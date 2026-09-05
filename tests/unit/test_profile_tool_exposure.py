import pytest


pytestmark = pytest.mark.unit


def test_profile_tool_exposure_filters_upstreams_and_hides_protected_tools() -> None:
    from mcp_broker.config import BrokerSettings
    from mcp_broker.profiles import ToolExposureProfile
    from mcp_broker.tool_namespace import ToolNamespaceRouter

    router = ToolNamespaceRouter(
        broker=BrokerSettings(tool_namespace_separator="."),
        upstreams=_profiled_upstreams(),
        profile=ToolExposureProfile(name="llm", max_tools=10),
    )

    advertised = router.advertise_all_tools(
        {
            "read-store": [{"name": "search"}],
            "diagram-renderer": [{"name": "render"}],
            "mail-writer": [{"name": "create_draft"}],
            "diagram-editor": [{"name": "create_diagram"}],
        }
    )

    assert [tool["name"] for tool in advertised] == ["read-store.search", "diagram-renderer.render"]
    assert router.resolve_tool_name("read-store.search").upstream_name == "read-store"

    with pytest.raises(ValueError, match="unknown tool prefix: mail-writer"):
        router.resolve_tool_name("mail-writer.create_draft")


def test_profile_tool_exposure_supports_claude_and_maintenance_profiles() -> None:
    from mcp_broker.config import BrokerSettings
    from mcp_broker.profiles import ToolExposureProfile
    from mcp_broker.tool_namespace import ToolNamespaceRouter

    claude_router = ToolNamespaceRouter(
        broker=BrokerSettings(tool_namespace_separator="."),
        upstreams=_profiled_upstreams(),
        profile=ToolExposureProfile(name="claude", max_tools=10),
    )
    maintenance_router = ToolNamespaceRouter(
        broker=BrokerSettings(tool_namespace_separator="."),
        upstreams=_profiled_upstreams(),
        profile=ToolExposureProfile(name="maintenance", max_tools=10),
    )

    assert [tool["name"] for tool in claude_router.advertise_all_tools({"diagram-editor": [{"name": "open"}]})] == [
        "diagram-editor.open"
    ]
    assert [
        tool["name"]
        for tool in maintenance_router.advertise_all_tools(
            {"mail-writer": [{"name": "create_draft"}]}
        )
    ] == ["mail-writer.create_draft"]


def test_profile_tool_exposure_enforces_budget_and_can_return_compact_broker_tools() -> None:
    from mcp_broker.config import BrokerSettings
    from mcp_broker.profiles import ToolExposureProfile
    from mcp_broker.tool_namespace import ToolNamespaceRouter

    router = ToolNamespaceRouter(
        broker=BrokerSettings(tool_namespace_separator="."),
        upstreams=_profiled_upstreams(),
        profile=ToolExposureProfile(name="llm", max_tools=1, compact_tools_enabled=True),
    )

    with pytest.raises(ValueError, match="profile llm exceeds tool budget: 2 > 1"):
        router.advertise_all_tools(
            {
                "read-store": [{"name": "search"}],
                "diagram-renderer": [{"name": "render"}],
            }
        )

    assert [tool["name"] for tool in router.compact_broker_tools()] == [
        "broker.search_tools",
        "broker.describe_tool",
        "broker.call_tool",
        "broker.status",
        "broker.close_session",
    ]


def test_snake_profile_exposes_broker_tools_with_client_safe_names() -> None:
    from mcp_broker.profiles import ToolExposureProfile

    profile = ToolExposureProfile(
        name="safe-client",
        max_tools=10,
        broker_tool_name_style="snake",
    )

    assert profile.exposed_broker_tool_name("broker.status") == "broker_status"
    assert profile.canonical_broker_tool_name("broker_status") == "broker.status"


def test_compact_broker_tools_include_directory_quality_metadata() -> None:
    from mcp_broker.config import BrokerSettings
    from mcp_broker.profiles import ToolExposureProfile
    from mcp_broker.tool_namespace import ToolNamespaceRouter

    router = ToolNamespaceRouter(
        broker=BrokerSettings(tool_namespace_separator="."),
        upstreams=_profiled_upstreams(),
        profile=ToolExposureProfile(name="llm", max_tools=1, compact_tools_enabled=True),
    )

    tools = {tool["name"]: tool for tool in router.compact_broker_tools()}

    for tool in tools.values():
        assert len(tool["description"]) >= 160
        assert tool["inputSchema"]["type"] == "object"
        assert len(tool["inputSchema"]["description"]) >= 80
        assert tool["inputSchema"]["additionalProperties"] is False
        for property_schema in tool["inputSchema"]["properties"].values():
            assert len(property_schema["description"]) >= 60

    search_schema = tools["broker.search_tools"]["inputSchema"]
    assert search_schema["properties"]["query"]["minLength"] == 1
    assert search_schema["properties"]["limit"]["default"] == 20
    assert "examples" in search_schema["properties"]["query"]

    call_schema = tools["broker.call_tool"]["inputSchema"]
    assert "mutating" in tools["broker.call_tool"]["description"]
    assert call_schema["properties"]["arguments"]["additionalProperties"] is True
    assert "examples" in call_schema["properties"]["name"]


def test_profile_tool_exposure_allows_tool_count_equal_to_budget() -> None:
    from mcp_broker.config import BrokerSettings
    from mcp_broker.profiles import ToolExposureProfile
    from mcp_broker.tool_namespace import ToolNamespaceRouter

    router = ToolNamespaceRouter(
        broker=BrokerSettings(tool_namespace_separator="."),
        upstreams=_profiled_upstreams(),
        profile=ToolExposureProfile(name="llm", max_tools=2),
    )

    assert router.advertise_all_tools(
        {
            "read-store": [{"name": "search"}],
            "diagram-renderer": [{"name": "render"}],
        }
    ) == [{"name": "read-store.search"}, {"name": "diagram-renderer.render"}]


def test_profile_tool_exposure_rejects_invalid_profile_settings() -> None:
    from mcp_broker.profiles import ToolExposureProfile

    with pytest.raises(ValueError, match="profile name cannot be empty"):
        ToolExposureProfile(name="", max_tools=1)

    with pytest.raises(ValueError, match="profile max_tools must be greater than 0"):
        ToolExposureProfile(name="llm", max_tools=0)

    with pytest.raises(ValueError, match="profile allow_mutating_upstreams cannot include empty values"):
        ToolExposureProfile(name="llm", max_tools=1, allow_mutating_upstreams=("",))


def test_profile_tool_exposure_denies_mutating_upstreams_without_allowlist() -> None:
    from mcp_broker.config import BrokerSettings, UpstreamConfig
    from mcp_broker.profiles import ToolExposureProfile
    from mcp_broker.tool_namespace import ToolNamespaceRouter

    upstreams = {
        "notes-writer": UpstreamConfig(
            name="notes-writer",
            command="notes-writer",
            mode="per_session",
            enabled=True,
            tool_prefix="notes-writer",
            profiles=("maintenance",),
            mutating=True,
        )
    }
    denied_router = ToolNamespaceRouter(
        broker=BrokerSettings(tool_namespace_separator="."),
        upstreams=upstreams,
        profile=ToolExposureProfile(name="maintenance", max_tools=10),
    )
    allowed_router = ToolNamespaceRouter(
        broker=BrokerSettings(tool_namespace_separator="."),
        upstreams=upstreams,
        profile=ToolExposureProfile(
            name="maintenance",
            max_tools=10,
            allow_mutating_upstreams=("notes-writer",),
        ),
    )

    with pytest.raises(ValueError, match="mutating upstream not allowed for profile: notes-writer"):
        denied_router.advertise_tools("notes-writer", [{"name": "write_note"}])

    assert allowed_router.advertise_tools("notes-writer", [{"name": "write_note"}]) == [
        {"name": "notes-writer.write_note"}
    ]


def test_profile_tool_exposure_handles_disabled_compact_mode_and_direct_denial() -> None:
    from mcp_broker.config import BrokerSettings
    from mcp_broker.profiles import ToolExposureProfile
    from mcp_broker.tool_namespace import ToolNamespaceRouter

    router = ToolNamespaceRouter(
        broker=BrokerSettings(tool_namespace_separator="."),
        upstreams=_profiled_upstreams(),
        profile=ToolExposureProfile(name="llm", max_tools=10),
    )

    assert router.compact_broker_tools() == []

    with pytest.raises(ValueError, match="upstream not exposed to profile: mail-writer"):
        router.advertise_tools("mail-writer", [{"name": "create_draft"}])


def test_profile_tool_exposure_without_profile_does_not_enforce_budget() -> None:
    from mcp_broker.config import BrokerSettings
    from mcp_broker.tool_namespace import ToolNamespaceRouter

    router = ToolNamespaceRouter(
        broker=BrokerSettings(tool_namespace_separator="."),
        upstreams=_profiled_upstreams(),
    )

    advertised = router.advertise_all_tools(
        {
            "read-store": [{"name": "search"}],
            "diagram-renderer": [{"name": "render"}],
            "diagram-editor": [{"name": "open"}],
        }
    )

    assert [tool["name"] for tool in advertised] == [
        "read-store.search",
        "diagram-renderer.render",
        "diagram-editor.open",
    ]


def _profiled_upstreams():
    from mcp_broker.config import UpstreamConfig

    return {
        "read-store": UpstreamConfig(
            name="read-store",
            command="read-store",
            args=[],
            mode="shared",
            enabled=True,
            tool_prefix="read-store",
            profiles=("llm", "maintenance"),
        ),
        "diagram-renderer": UpstreamConfig(
            name="diagram-renderer",
            command="diagram-renderer",
            args=[],
            mode="shared",
            enabled=True,
            tool_prefix="diagram-renderer",
            profiles=("llm", "maintenance"),
        ),
        "mail-writer": UpstreamConfig(
            name="mail-writer",
            command="ms365",
            args=[],
            mode="per_session",
            enabled=True,
            tool_prefix="mail-writer",
            profiles=("protected", "maintenance"),
        ),
        "diagram-editor": UpstreamConfig(
            name="diagram-editor",
            command="diagram-editor",
            args=[],
            mode="shared",
            enabled=True,
            tool_prefix="diagram-editor",
            profiles=("claude", "maintenance"),
        ),
    }
