import pytest


pytestmark = pytest.mark.unit


def test_tool_namespace_advertises_configured_prefix_without_mutating_upstream_tool() -> None:
    from mcp_broker.config import BrokerSettings, UpstreamConfig
    from mcp_broker.tool_namespace import ToolNamespaceRouter

    upstream = UpstreamConfig(
        name="read-store",
        command="node",
        args=[],
        mode="shared",
        enabled=True,
        tool_prefix="mem",
    )
    router = ToolNamespaceRouter(
        broker=BrokerSettings(tool_namespace_separator="."),
        upstreams={"read-store": upstream},
    )
    upstream_tool = {
        "name": "search",
        "description": "Search read-store",
        "inputSchema": {"type": "object"},
    }

    [advertised] = router.advertise_tools("read-store", [upstream_tool])

    assert advertised == {
        "name": "mem.search",
        "description": "Search read-store",
        "inputSchema": {"type": "object"},
    }
    assert upstream_tool["name"] == "search"


def test_tool_namespace_resolves_advertised_name_to_upstream_route() -> None:
    from mcp_broker.config import BrokerSettings, UpstreamConfig
    from mcp_broker.tool_namespace import ToolNamespaceRouter

    router = ToolNamespaceRouter(
        broker=BrokerSettings(tool_namespace_separator="__"),
        upstreams={
            "read-store": UpstreamConfig(
                name="read-store",
                command="node",
                args=[],
                mode="shared",
                enabled=True,
                tool_prefix="mem",
            ),
            "ms365": UpstreamConfig(
                name="ms365",
                command="npm",
                args=[],
                mode="per_session",
                enabled=False,
                tool_prefix="mail",
            ),
        },
    )

    route = router.resolve_tool_name("mem__search")

    assert route.upstream_name == "read-store"
    assert route.upstream_tool_name == "search"

    with pytest.raises(ValueError, match="unknown tool prefix: mail"):
        router.resolve_tool_name("mail__create_draft")

    with pytest.raises(ValueError, match="missing namespace separator"):
        router.resolve_tool_name("search")

    with pytest.raises(ValueError, match="missing upstream tool name"):
        router.resolve_tool_name("mem__")


def test_tool_namespace_resolves_upstream_tool_names_that_contain_separator() -> None:
    from mcp_broker.config import BrokerSettings, UpstreamConfig
    from mcp_broker.tool_namespace import ToolNamespaceRouter

    router = ToolNamespaceRouter(
        broker=BrokerSettings(tool_namespace_separator="__"),
        upstreams={
            "read-store": UpstreamConfig(
                name="read-store",
                command="node",
                args=[],
                mode="shared",
                enabled=True,
                tool_prefix="mem",
            ),
        },
    )

    route = router.resolve_tool_name("mem__search__deep")

    assert route.upstream_name == "read-store"
    assert route.upstream_tool_name == "search__deep"


def test_tool_namespace_rejects_invalid_namespace_config() -> None:
    from mcp_broker.config import BrokerSettings, UpstreamConfig
    from mcp_broker.tool_namespace import ToolNamespaceRouter

    upstream = UpstreamConfig(
        name="read-store",
        command="node",
        args=[],
        mode="shared",
        enabled=True,
        tool_prefix="mem",
    )

    with pytest.raises(ValueError) as exc_info:
        ToolNamespaceRouter(
            broker=BrokerSettings(tool_namespace_separator=""),
            upstreams={"read-store": upstream},
        )
    assert str(exc_info.value) == "broker.tool_namespace_separator cannot be empty"

    with pytest.raises(ValueError, match="duplicate tool prefix: mem"):
        ToolNamespaceRouter(
            broker=BrokerSettings(tool_namespace_separator="."),
            upstreams={
                "read-store": upstream,
                "memory_backup": UpstreamConfig(
                    name="memory_backup",
                    command="node",
                    args=[],
                    mode="shared",
                    enabled=True,
                    tool_prefix="mem",
                ),
            },
        )


def test_tool_namespace_rejects_unknown_disabled_and_malformed_upstream_tools() -> None:
    from mcp_broker.config import BrokerSettings, UpstreamConfig
    from mcp_broker.tool_namespace import ToolNamespaceRouter

    router = ToolNamespaceRouter(
        broker=BrokerSettings(tool_namespace_separator="."),
        upstreams={
            "read-store": UpstreamConfig(
                name="read-store",
                command="node",
                args=[],
                mode="shared",
                enabled=True,
                tool_prefix="read-store",
            ),
            "disabled": UpstreamConfig(
                name="disabled",
                command="node",
                args=[],
                mode="disabled",
                enabled=True,
                tool_prefix="disabled",
            ),
        },
    )

    with pytest.raises(ValueError, match="unknown upstream: missing"):
        router.advertise_tools("missing", [{"name": "search"}])

    with pytest.raises(ValueError, match="upstream disabled: disabled"):
        router.advertise_tools("disabled", [{"name": "search"}])

    with pytest.raises(ValueError, match="upstream tool missing name: read-store"):
        router.advertise_tools("read-store", [{"description": "missing name"}])


def test_tool_namespace_advertise_all_skips_enabled_false_upstreams() -> None:
    from mcp_broker.config import BrokerSettings, UpstreamConfig
    from mcp_broker.tool_namespace import ToolNamespaceRouter

    router = ToolNamespaceRouter(
        broker=BrokerSettings(tool_namespace_separator="."),
        upstreams={
            "read-store": UpstreamConfig(
                name="read-store",
                command="node",
                args=[],
                mode="shared",
                enabled=True,
                tool_prefix="read-store",
            ),
            "flag-disabled": UpstreamConfig(
                name="flag-disabled",
                command="node",
                args=[],
                mode="shared",
                enabled=False,
                tool_prefix="flag-disabled",
            ),
        },
    )

    assert router.advertise_all_tools(
        {
            "read-store": [{"name": "search"}],
            "flag-disabled": [{"name": "hidden"}],
        }
    ) == [{"name": "read-store.search"}]


def test_compact_broker_tools_rejects_unknown_name_style() -> None:
    from mcp_broker.tool_namespace import compact_broker_tool_definitions

    with pytest.raises(ValueError, match="broker_tool_name_style must be one of: dotted, snake"):
        compact_broker_tool_definitions(broker_tool_name_style="camel")


def test_compact_broker_tools_default_to_dotted_canonical_names() -> None:
    from mcp_broker.tool_namespace import compact_broker_tool_definitions

    definitions = compact_broker_tool_definitions()

    assert [definition["name"] for definition in definitions] == [
        "broker.search_tools",
        "broker.describe_tool",
        "broker.call_tool",
        "broker.status",
        "broker.close_session",
    ]
    assert definitions[0]["inputSchema"]["properties"]["query"]["minLength"] == 1
    assert definitions[2]["inputSchema"]["required"] == ["name", "arguments"]
    # broker.call_tool exposes an optional projection (not required) so callers can
    # trim verbose upstream responses server-side before they reach the context.
    call_properties = definitions[2]["inputSchema"]["properties"]
    projection_schema = call_properties["projection"]
    assert projection_schema["type"] == "object"
    assert len(projection_schema["description"]) >= 60
    assert projection_schema["properties"]["paths"]["type"] == "array"
    assert projection_schema["properties"]["paths"]["items"]["type"] == "string"
    assert projection_schema["properties"]["max_array_items"]["type"] == "integer"
    assert projection_schema["additionalProperties"] is False
    assert "projection" not in definitions[2]["inputSchema"]["required"]
    assert definitions[4]["inputSchema"]["additionalProperties"] is False


def test_compact_broker_tools_apply_snake_style_to_every_broker_tool() -> None:
    from mcp_broker.config import BrokerSettings
    from mcp_broker.profiles import ToolExposureProfile
    from mcp_broker.tool_namespace import ToolNamespaceRouter, compact_broker_tool_definitions

    expected_names = [
        "broker_search_tools",
        "broker_describe_tool",
        "broker_call_tool",
        "broker_status",
        "broker_close_session",
    ]

    assert [
        tool["name"]
        for tool in compact_broker_tool_definitions(broker_tool_name_style="snake")
    ] == expected_names

    router = ToolNamespaceRouter(
        broker=BrokerSettings(tool_namespace_separator="."),
        upstreams={},
        profile=ToolExposureProfile(
            name="safe-client",
            max_tools=10,
            compact_tools_enabled=True,
            broker_tool_name_style="snake",
        ),
    )
    assert [tool["name"] for tool in router.compact_broker_tools()] == expected_names


def test_advertise_all_tools_rejects_duplicate_names_from_one_upstream() -> None:
    from mcp_broker.config import BrokerSettings, UpstreamConfig
    from mcp_broker.tool_namespace import ToolNamespaceRouter

    router = ToolNamespaceRouter(
        broker=BrokerSettings(tool_namespace_separator="."),
        upstreams={
            "read-store": UpstreamConfig(
                name="read-store",
                command="node",
                args=[],
                mode="shared",
                enabled=True,
                tool_prefix="read-store",
            )
        },
    )

    with pytest.raises(ValueError) as exc_info:
        router.advertise_all_tools(
            {"read-store": [{"name": "search"}, {"name": "search"}]}
        )

    assert str(exc_info.value) == "duplicate advertised tool: read-store.search"


def test_advertise_all_tools_skips_mode_disabled_upstream() -> None:
    from mcp_broker.config import BrokerSettings, UpstreamConfig
    from mcp_broker.tool_namespace import ToolNamespaceRouter

    router = ToolNamespaceRouter(
        broker=BrokerSettings(tool_namespace_separator="."),
        upstreams={
            "disabled": UpstreamConfig(
                name="disabled",
                command="node",
                args=[],
                mode="disabled",
                enabled=True,
                tool_prefix="disabled",
            )
        },
    )

    assert router.advertise_all_tools(
        {"disabled": [{"name": "must-not-appear"}]}
    ) == []
