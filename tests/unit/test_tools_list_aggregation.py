import pytest


pytestmark = pytest.mark.unit


def test_broker_lists_namespaced_tools_for_profile_allowed_upstreams() -> None:
    from mcp_broker.broker import BrokerCore
    from mcp_broker.config import BrokerSettings
    from mcp_broker.profiles import ToolExposureProfile

    broker = BrokerCore(
        settings=BrokerSettings(tool_namespace_separator="."),
        upstreams=_upstreams(),
        profile=ToolExposureProfile(name="llm", max_tools=10),
    )

    result = broker.list_tools(
        {
            "read-store": [
                {
                    "name": "search",
                    "description": "Search read-store",
                    "inputSchema": {"type": "object"},
                }
            ],
            "diagram-renderer": [{"name": "render", "description": "Render diagram"}],
        }
    )

    assert result == {
        "tools": [
            {
                "name": "read-store.search",
                "description": "Search read-store",
                "inputSchema": {"type": "object"},
            },
            {
                "name": "diagram-renderer.render",
                "description": "Render diagram",
            },
        ]
    }


def test_broker_list_tools_skips_disabled_and_profile_denied_upstreams() -> None:
    from mcp_broker.broker import BrokerCore
    from mcp_broker.config import BrokerSettings
    from mcp_broker.profiles import ToolExposureProfile

    broker = BrokerCore(
        settings=BrokerSettings(tool_namespace_separator="."),
        upstreams=_upstreams(),
        profile=ToolExposureProfile(name="llm", max_tools=10),
    )

    result = broker.list_tools(
        {
            "read-store": [{"name": "search"}],
            "disabled": [{"name": "hidden"}],
            "mail-writer": [{"name": "create_draft"}],
        }
    )

    assert result == {"tools": [{"name": "read-store.search"}]}


def test_broker_list_tools_rejects_duplicate_advertised_tool_names() -> None:
    from mcp_broker.broker import BrokerCore
    from mcp_broker.config import BrokerSettings
    from mcp_broker.profiles import ToolExposureProfile

    broker = BrokerCore(
        settings=BrokerSettings(tool_namespace_separator="."),
        upstreams=_upstreams(),
        profile=ToolExposureProfile(name="llm", max_tools=10),
    )

    with pytest.raises(ValueError, match="duplicate advertised tool: read-store.search"):
        broker.list_tools(
            {
                "read-store": [
                    {"name": "search"},
                    {"name": "search"},
                ]
            }
        )


def test_broker_list_tools_uses_compact_mode_when_budget_would_be_exceeded() -> None:
    from mcp_broker.broker import BrokerCore
    from mcp_broker.config import BrokerSettings
    from mcp_broker.profiles import ToolExposureProfile

    broker = BrokerCore(
        settings=BrokerSettings(tool_namespace_separator="."),
        upstreams=_upstreams(),
        profile=ToolExposureProfile(name="llm", max_tools=1, compact_tools_enabled=True),
    )

    result = broker.list_tools(
        {
            "read-store": [{"name": "search"}],
            "diagram-renderer": [{"name": "render"}],
        }
    )

    assert [tool["name"] for tool in result["tools"]] == [
        "broker.search_tools",
        "broker.describe_tool",
        "broker.call_tool",
        "broker.status",
        "broker.close_session",
    ]
    assert all(len(tool["description"]) >= 160 for tool in result["tools"])
    assert result["tools"][0]["inputSchema"]["properties"]["query"]["description"]
    assert result["tools"][2]["inputSchema"]["properties"]["arguments"]["additionalProperties"] is True


def test_broker_list_tools_can_render_compact_tools_with_profile_safe_names() -> None:
    from mcp_broker.broker import BrokerCore
    from mcp_broker.config import BrokerSettings
    from mcp_broker.profiles import ToolExposureProfile

    broker = BrokerCore(
        settings=BrokerSettings(tool_namespace_separator="."),
        upstreams=_upstreams(),
        profile=ToolExposureProfile(
            name="llm",
            max_tools=1,
            compact_tools_enabled=True,
            broker_tool_name_style="snake",
        ),
    )

    result = broker.compact_tools()

    assert [tool["name"] for tool in result["tools"]] == [
        "broker_search_tools",
        "broker_describe_tool",
        "broker_call_tool",
        "broker_status",
        "broker_close_session",
    ]


def test_broker_list_tools_enforces_budget_when_compact_mode_is_disabled() -> None:
    from mcp_broker.broker import BrokerCore
    from mcp_broker.config import BrokerSettings
    from mcp_broker.profiles import ToolExposureProfile

    broker = BrokerCore(
        settings=BrokerSettings(tool_namespace_separator="."),
        upstreams=_upstreams(),
        profile=ToolExposureProfile(name="llm", max_tools=1),
    )

    with pytest.raises(ValueError, match="profile llm exceeds tool budget: 2 > 1"):
        broker.list_tools(
            {
                "read-store": [{"name": "search"}],
                "diagram-renderer": [{"name": "render"}],
            }
        )


def _upstreams():
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
        "disabled": UpstreamConfig(
            name="disabled",
            command="disabled",
            args=[],
            mode="disabled",
            enabled=True,
            tool_prefix="disabled",
            profiles=("llm",),
        ),
    }
