from pathlib import Path
import pytest
from mcp_broker.broker import BrokerToolError
from mcp_broker.catalog import (
    BrokerCatalogFacade,
    _specific_query_can_select_upstream,
    catalog_entries_for_upstream,
    catalog_entry_matches,
    catalog_unavailable_entry_for_upstream,
    profile_allows_upstream,
    structured_tool_result,
    upstream_metadata_matches,
    upstream_owns_tool_name,
)
from mcp_broker.config import (
    BrokerConfig,
    BrokerSettings,
    RuntimeConfig,
    SmokeProbe,
    UpstreamConfig,
)
from mcp_broker.profiles import ToolExposureProfile
pytestmark = pytest.mark.unit
def _projection_error_message(projection: object) -> str:
    from mcp_broker.catalog import apply_projection

    with pytest.raises(ValueError) as exc:
        apply_projection({"content": []}, projection)  # type: ignore[arg-type]
    return str(exc.value)
def _catalog_config(tmp_path: Path) -> BrokerConfig:
    return BrokerConfig(
        runtime=_runtime(tmp_path),
        broker=BrokerSettings(),
        profiles={
            "default-llm": ToolExposureProfile(
                name="default-llm",
                max_tools=20,
                compact_tools_enabled=True,
                allow_mutating_upstreams=("write-store",),
            ),
            "other-llm": ToolExposureProfile(name="other-llm", max_tools=20),
        },
        upstreams={
            "read-store": UpstreamConfig(
                name="read-store",
                command="read-store",
                tool_prefix="read",
                profiles=("default-llm",),
                purpose="Read records",
                tags=("records",),
            ),
            "write-store": UpstreamConfig(
                name="write-store",
                command="write-store",
                tool_prefix="write",
                profiles=("default-llm",),
                mutating=True,
            ),
            "broken-store": UpstreamConfig(
                name="broken-store",
                command="broken-store",
                profiles=("default-llm",),
            ),
            "other-profile-store": UpstreamConfig(
                name="other-profile-store",
                command="other-profile-store",
                profiles=("other-llm",),
            ),
            "disabled-store": UpstreamConfig(
                name="disabled-store",
                command="disabled-store",
                enabled=False,
                profiles=("default-llm",),
            ),
        },
    )
def _runtime(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        root=tmp_path / "runtime",
        socket_path=tmp_path / "runtime" / "sockets" / "broker.sock",
        log_dir=tmp_path / "runtime" / "logs",
        state_dir=tmp_path / "runtime" / "state",
        secrets_dir=tmp_path / "runtime" / "secrets",
    )

def test_call_tool_unknown_broker_tool_raises_contract_error(tmp_path: Path) -> None:
    config = _catalog_config(tmp_path)
    facade = BrokerCatalogFacade(
        broker_config=config,
        profile=config.profiles["default-llm"],
        list_upstream=lambda _name, _timeout: [],
        call_upstream=lambda _name, _tool, _args, _timeout: {"content": []},
        call_locks={},
    )

    with pytest.raises(BrokerToolError) as exc:
        facade.call_tool("broker.missing", {})

    assert exc.value.code == "unknown_broker_tool"
    assert exc.value.message == "unknown broker tool: broker.missing"

def test_call_managed_tool_rejects_invalid_payload_before_upstream_call(tmp_path: Path) -> None:
    config = _catalog_config(tmp_path)
    calls: list[tuple[str, str, dict[str, object], int]] = []

    facade = BrokerCatalogFacade(
        broker_config=config,
        profile=config.profiles["default-llm"],
        list_upstream=lambda _name, _timeout: [],
        call_upstream=lambda name, tool, args, timeout: calls.append((name, tool, args, timeout)) or {},
        call_locks={},
    )

    with pytest.raises(ValueError) as invalid_arguments:
        facade.call_tool("broker.call_tool", {"name": "read.find_record", "arguments": []})
    assert str(invalid_arguments.value) == "broker.call_tool requires name and object arguments"
    with pytest.raises(ValueError) as invalid_name:
        facade.call_tool("broker.call_tool", {"name": None, "arguments": {}})
    assert str(invalid_name.value) == "broker.call_tool requires name and object arguments"
    assert calls == []

def test_call_managed_tool_defaults_missing_arguments_to_empty_object(tmp_path: Path) -> None:
    config = _catalog_config(tmp_path)
    calls: list[tuple[str, str, dict[str, object], int]] = []

    result = BrokerCatalogFacade(
        broker_config=config,
        profile=config.profiles["default-llm"],
        list_upstream=lambda _name, _timeout: [],
        call_upstream=lambda name, tool, args, timeout: calls.append((name, tool, args, timeout))
        or {"content": []},
        call_locks={},
    ).call_tool("broker.call_tool", {"name": "read.find_record"})

    assert result == {"content": []}
    assert calls == [("read-store", "find_record", {}, 60)]

def test_call_managed_tool_injects_cwd_project_for_configured_upstream(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "Projects" / "apps" / "mcp-broker"
    (repo / ".git").mkdir(parents=True)
    config = BrokerConfig(
        runtime=_runtime(tmp_path),
        broker=BrokerSettings(),
        profiles={"codex": ToolExposureProfile(name="codex", max_tools=20)},
        upstreams={
            "memory-index": UpstreamConfig(
                name="memory-index",
                command="codebase-memory",
                tool_prefix="codebase-memory",
                profiles=("codex",),
                inject_cwd_project=True,
                inject_cwd_project_exclude=("list_projects",),
            ),
        },
    )
    calls: list[tuple[str, str, dict[str, object], int]] = []

    result = BrokerCatalogFacade(
        broker_config=config,
        profile=config.profiles["codex"],
        list_upstream=lambda _name, _timeout: [],
        call_upstream=lambda name, tool, args, timeout: calls.append((name, tool, args, timeout))
        or {"content": []},
        call_locks={},
        client_cwd=str(repo / "src" / "mcp_broker"),
    ).call_tool(
        "broker.call_tool",
        {"name": "codebase-memory.search_graph", "arguments": {"query": "BrokerCore"}},
    )

    assert result == {"content": []}
    assert calls == [
        (
            "memory-index",
            "search_graph",
            {
                "query": "BrokerCore",
                "project": str(repo).lstrip("/").replace("/", "-"),
            },
            60,
        )
    ]

def test_call_managed_tool_does_not_inject_cwd_project_for_excluded_tool(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "Projects" / "apps" / "mcp-broker"
    (repo / ".git").mkdir(parents=True)
    config = BrokerConfig(
        runtime=_runtime(tmp_path),
        broker=BrokerSettings(),
        profiles={"codex": ToolExposureProfile(name="codex", max_tools=20)},
        upstreams={
            "memory-index": UpstreamConfig(
                name="memory-index",
                command="codebase-memory",
                tool_prefix="codebase-memory",
                profiles=("codex",),
                inject_cwd_project=True,
                inject_cwd_project_exclude=("list_projects",),
            ),
        },
    )
    calls: list[tuple[str, str, dict[str, object], int]] = []

    BrokerCatalogFacade(
        broker_config=config,
        profile=config.profiles["codex"],
        list_upstream=lambda _name, _timeout: [],
        call_upstream=lambda name, tool, args, timeout: calls.append((name, tool, args, timeout))
        or {"content": []},
        call_locks={},
        client_cwd=str(repo),
    ).call_tool(
        "broker.call_tool",
        {"name": "codebase-memory.list_projects", "arguments": {}},
    )

    assert calls == [("memory-index", "list_projects", {}, 60)]

def test_call_managed_tool_uses_configured_namespace_separator_for_project_injection(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "Projects" / "apps" / "mcp-broker"
    (repo / ".git").mkdir(parents=True)
    config = BrokerConfig(
        runtime=_runtime(tmp_path),
        broker=BrokerSettings(tool_namespace_separator="::"),
        profiles={"codex": ToolExposureProfile(name="codex", max_tools=20)},
        upstreams={
            "memory-index": UpstreamConfig(
                name="memory-index",
                command="codebase-memory",
                tool_prefix="codebase-memory",
                profiles=("codex",),
                inject_cwd_project=True,
            ),
        },
    )
    calls: list[tuple[str, str, dict[str, object], int]] = []

    BrokerCatalogFacade(
        broker_config=config,
        profile=config.profiles["codex"],
        list_upstream=lambda _name, _timeout: [],
        call_upstream=lambda name, tool, args, timeout: calls.append((name, tool, args, timeout))
        or {"content": []},
        call_locks={},
        client_cwd=str(repo),
    ).call_tool(
        "broker.call_tool",
        {"name": "codebase-memory::search_graph", "arguments": {"query": "BrokerCore"}},
    )

    assert calls == [
        (
            "memory-index",
            "search_graph",
            {
                "query": "BrokerCore",
                "project": str(repo).lstrip("/").replace("/", "-"),
            },
            60,
        )
    ]

def test_project_injection_defaults_empty_namespace_separator_to_dot(tmp_path: Path) -> None:
    repo = tmp_path / "Projects" / "apps" / "mcp-broker"
    (repo / ".git").mkdir(parents=True)
    config = BrokerConfig(
        runtime=_runtime(tmp_path),
        broker=BrokerSettings(tool_namespace_separator=""),
        profiles={"codex": ToolExposureProfile(name="codex", max_tools=20)},
        upstreams={
            "memory-index": UpstreamConfig(
                name="memory-index",
                command="codebase-memory",
                tool_prefix="codebase-memory",
                profiles=("codex",),
                inject_cwd_project=True,
            ),
        },
    )
    facade = BrokerCatalogFacade(
        broker_config=config,
        profile=config.profiles["codex"],
        list_upstream=lambda _name, _timeout: [],
        call_upstream=lambda _name, _tool, _args, _timeout: {"content": []},
        call_locks={},
        client_cwd=str(repo),
    )

    result = facade._inject_cwd_project_arg(
        "codebase-memory.search_graph",
        {"query": "BrokerCore"},
    )

    assert result == {
        "query": "BrokerCore",
        "project": str(repo).lstrip("/").replace("/", "-"),
    }

def test_call_managed_tool_enforces_profile_and_uses_shared_call_locks(tmp_path: Path) -> None:
    profile = ToolExposureProfile(name="default-llm", max_tools=20)
    config = BrokerConfig(
        runtime=_runtime(tmp_path),
        broker=BrokerSettings(),
        profiles={},
        upstreams={
            "write-store": UpstreamConfig(
                name="write-store",
                command="write-store",
                tool_prefix="write",
                profiles=("default-llm",),
                mutating=True,
                serialize_calls=True,
            ),
            "read-store": UpstreamConfig(
                name="read-store",
                command="read-store",
                tool_prefix="read",
                profiles=("default-llm",),
                serialize_calls=True,
            ),
        },
    )
    call_locks: dict[str, object] = {}
    calls: list[tuple[str, str, dict[str, object], int]] = []
    facade = BrokerCatalogFacade(
        broker_config=config,
        profile=profile,
        list_upstream=lambda _name, _timeout: [],
        call_upstream=lambda name, tool, args, timeout: calls.append((name, tool, args, timeout))
        or {"content": []},
        call_locks=call_locks,  # type: ignore[arg-type]
    )

    with pytest.raises(BrokerToolError) as exc:
        facade.call_tool("broker.call_tool", {"name": "write.create", "arguments": {}})

    assert exc.value.code == "mutating_not_allowed"
    assert calls == []
    assert facade.call_tool("broker.call_tool", {"name": "read.find", "arguments": {}}) == {
        "content": []
    }
    assert calls == [("read-store", "find", {}, 60)]
    assert set(call_locks) == {"read-store"}

def test_status_reports_visible_disabled_and_allowed_mutating_upstreams(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerIdentityConfig

    config = BrokerConfig(
        runtime=_runtime(tmp_path),
        broker=BrokerSettings(
            identity=BrokerIdentityConfig(
                broker_id="engineer-laptop",
                environment="local",
                bundle_version="unbundled",
            )
        ),
        profiles={
            "default-llm": ToolExposureProfile(
                name="default-llm",
                max_tools=20,
                allow_mutating_upstreams=("write-store",),
            ),
            "maintenance": ToolExposureProfile(name="maintenance", max_tools=500),
            "other-llm": ToolExposureProfile(name="other-llm", max_tools=20),
        },
        upstreams=_catalog_config(tmp_path).upstreams,
    )
    profile = ToolExposureProfile(
        name="default-llm",
        max_tools=20,
        allow_mutating_upstreams=("write-store",),
    )
    visible_sets: list[set[str] | None] = []

    def status_provider(visible: set[str] | None) -> dict[str, dict[str, object]]:
        visible_sets.append(visible)
        return {
            "read-store": {
                "state": "running",
                "pid": 456,
                "restarts": 2,
                "sessions": 3,
                "auth_probe": "tool-call",
                "auth_state": "authenticated",
                "auth_repair_attempts": 4,
                "auth_repair_successes": 3,
                "auth_repair_failures": 1,
            },
            "write-store": {
                "state": "running",
                "auth_state": "unauthenticated",
                "last_error": "token expired",
            },
            "broken-store": {
                "state": "configured",
                "auth_state": "invalid-value",
                "last_error": "HTTP 403 forbidden",
            },
            "disabled-store": {"state": "disabled"},
        }

    result = BrokerCatalogFacade(
        broker_config=config,
        profile=profile,
        list_upstream=lambda _name, _timeout: [],
        call_upstream=lambda _name, _tool, _args, _timeout: {"content": []},
        call_locks={},
        status_provider=status_provider,
    ).call_tool("broker.status", {})

    payload = result["structuredContent"]
    assert visible_sets == [{"read-store", "write-store", "broken-store"}]
    assert payload["identity"] == {
        "active_profile": "default-llm",
        "active_profiles": ["default-llm", "maintenance", "other-llm"],
        "broker_id": "engineer-laptop",
        "bundle_version": "unbundled",
        "environment": "local",
        "schema_version": 1,
    }
    assert payload["profile"] == "default-llm"
    assert payload["socket_path"] == str(config.runtime.socket_path)
    assert payload["status"] == "degraded"
    assert set(payload["upstreams"]) == {
        "read-store",
        "write-store",
        "broken-store",
        "disabled-store",
    }
    assert payload["upstreams"]["read-store"] == {
        "enabled": True,
        "auth_repair_attempts": 4,
        "auth_repair_failures": 1,
        "auth_repair_successes": 3,
        "auth_probe": "tool-call",
        "auth_state": "authenticated",
        "exposed": True,
        "last_error": None,
        "mode": "shared",
        "mutating": False,
        "pid": 456,
        "restarts": 2,
        "session_count": 3,
        "state": "running",
        "transport": "stdio",
    }
    assert payload["upstreams"]["write-store"]["auth_state"] == "unauthenticated"
    assert payload["upstreams"]["write-store"]["mutating"] is True
    assert payload["upstreams"]["broken-store"]["auth_state"] == "unauthenticated"
    assert payload["upstreams"]["disabled-store"]["enabled"] is False
    assert payload["upstreams"]["disabled-store"]["exposed"] is False
    assert payload["upstreams"]["disabled-store"]["state"] == "disabled"

def test_status_reports_session_count_key_and_default_configured_states(tmp_path: Path) -> None:
    config = BrokerConfig(
        runtime=_runtime(tmp_path),
        broker=BrokerSettings(),
        profiles={"default-llm": ToolExposureProfile(name="default-llm", max_tools=20)},
        upstreams={
            "read-store": UpstreamConfig(
                name="read-store",
                command="read-store",
                profiles=("default-llm",),
            ),
            "mode-disabled-store": UpstreamConfig(
                name="mode-disabled-store",
                command="mode-disabled-store",
                mode="disabled",
                profiles=("default-llm",),
            ),
        },
    )

    result = BrokerCatalogFacade(
        broker_config=config,
        profile=config.profiles["default-llm"],
        list_upstream=lambda _name, _timeout: [],
        call_upstream=lambda _name, _tool, _args, _timeout: {"content": []},
        call_locks={},
        status_provider=lambda _visible: {"read-store": {"session_count": 7}},
    ).call_tool("broker.status", {})

    payload = result["structuredContent"]
    assert payload["socket_path"] == str(config.runtime.socket_path)
    assert payload["status"] == "ok"
    assert payload["upstreams"]["read-store"]["session_count"] == 7
    assert payload["upstreams"]["read-store"]["state"] == "configured"
    assert payload["upstreams"]["mode-disabled-store"]["enabled"] is True
    assert payload["upstreams"]["mode-disabled-store"]["exposed"] is False
    assert payload["upstreams"]["mode-disabled-store"]["state"] == "disabled"

@pytest.mark.parametrize("state", ["exited", "failed", "backoff"])
def test_status_degrades_for_stopped_runtime_states(tmp_path: Path, state: str) -> None:
    config = BrokerConfig(
        runtime=_runtime(tmp_path),
        broker=BrokerSettings(),
        profiles={"default-llm": ToolExposureProfile(name="default-llm", max_tools=20)},
        upstreams={
            "read-store": UpstreamConfig(
                name="read-store",
                command="read-store",
                profiles=("default-llm",),
            )
        },
    )

    result = BrokerCatalogFacade(
        broker_config=config,
        profile=config.profiles["default-llm"],
        list_upstream=lambda _name, _timeout: [],
        call_upstream=lambda _name, _tool, _args, _timeout: {"content": []},
        call_locks={},
        status_provider=lambda _visible: {"read-store": {"state": state}},
    ).call_tool("broker.status", {})

    assert result["structuredContent"]["status"] == "degraded"
    assert result["structuredContent"]["upstreams"]["read-store"]["last_error"] is None

def test_status_filters_enabled_upstreams_hidden_by_profile_or_mutating_policy(tmp_path: Path) -> None:
    config = _catalog_config(tmp_path)

    result = BrokerCatalogFacade(
        broker_config=config,
        profile=ToolExposureProfile(name="default-llm", max_tools=20),
        list_upstream=lambda _name, _timeout: [],
        call_upstream=lambda _name, _tool, _args, _timeout: {"content": []},
        call_locks={},
        status_provider=lambda visible: {name: {"state": "running"} for name in visible or set()},
    ).call_tool("broker.status", {})

    payload = result["structuredContent"]
    assert set(payload["upstreams"]) == {"read-store", "broken-store", "disabled-store"}
    assert "write-store" not in payload["upstreams"]
    assert "other-profile-store" not in payload["upstreams"]

def test_status_rejects_arguments_except_client_control(tmp_path: Path) -> None:
    config = _catalog_config(tmp_path)
    facade = BrokerCatalogFacade(
        broker_config=config,
        profile=config.profiles["default-llm"],
        list_upstream=lambda _name, _timeout: [],
        call_upstream=lambda _name, _tool, _args, _timeout: {"content": []},
        call_locks={},
    )

    assert facade.call_tool("broker.status", {"wait_for_previous": True})["structuredContent"][
        "profile"
    ] == "default-llm"
    with pytest.raises(ValueError) as exc:
        facade.call_tool("broker.status", {"verbose": True})

    assert str(exc.value) == "broker.status does not accept arguments"

def test_close_session_uses_only_bound_session_metadata(tmp_path: Path) -> None:
    config = _catalog_config(tmp_path)
    stopped_sessions: list[str] = []
    facade = BrokerCatalogFacade(
        broker_config=config,
        profile=config.profiles["default-llm"],
        list_upstream=lambda _name, _timeout: [],
        call_upstream=lambda _name, _tool, _args, _timeout: {"content": []},
        call_locks={},
        session_id="calling-session",
        session_stopper=lambda session_id: stopped_sessions.append(session_id) or {"stopped": True},
    )

    assert facade.call_tool("broker.close_session", {"wait_for_previous": True})[
        "structuredContent"
    ] == {"stopped": True}
    assert stopped_sessions == ["calling-session"]
    with pytest.raises(ValueError) as invalid_arguments:
        facade.call_tool("broker.close_session", {"broker_session_id": "another-session"})
    assert str(invalid_arguments.value) == "broker.close_session does not accept arguments"

@pytest.mark.parametrize(
    ("session_id", "session_stopper"),
    [
        (None, lambda _session_id: {"stopped": True}),
        ("calling-session", None),
    ],
)
def test_close_session_requires_each_bound_session_metadata_field(
    tmp_path: Path,
    session_id: str | None,
    session_stopper,
) -> None:
    config = _catalog_config(tmp_path)
    facade = BrokerCatalogFacade(
        broker_config=config,
        profile=config.profiles["default-llm"],
        list_upstream=lambda _name, _timeout: [],
        call_upstream=lambda _name, _tool, _args, _timeout: {"content": []},
        call_locks={},
        session_id=session_id,
        session_stopper=session_stopper,
    )

    with pytest.raises(ValueError) as missing_metadata:
        facade.call_tool("broker.close_session", {})
    assert str(missing_metadata.value) == "broker.close_session requires broker session metadata"

def test_catalog_listing_continues_after_unavailable_and_disabled_upstreams(
    tmp_path: Path,
) -> None:
    config = BrokerConfig(
        runtime=_runtime(tmp_path),
        broker=BrokerSettings(),
        profiles={"default-llm": ToolExposureProfile(name="default-llm", max_tools=20)},
        upstreams={
            "mode-disabled-store": UpstreamConfig(
                name="mode-disabled-store",
                command="mode-disabled-store",
                mode="disabled",
                profiles=("default-llm",),
            ),
            "broken-store": UpstreamConfig(
                name="broken-store",
                command="broken-store",
                profiles=("default-llm",),
            ),
            "read-store": UpstreamConfig(
                name="read-store",
                command="read-store",
                tool_prefix="read",
                profiles=("default-llm",),
            ),
        },
    )
    calls: list[str] = []

    def list_upstream(upstream_name: str, _timeout: int) -> list[dict[str, object]]:
        calls.append(upstream_name)
        if upstream_name == "broken-store":
            raise RuntimeError("missing token")
        return [{"name": "find"}]

    result = BrokerCatalogFacade(
        broker_config=config,
        profile=config.profiles["default-llm"],
        list_upstream=list_upstream,
        call_upstream=lambda _name, _tool, _args, _timeout: {"content": []},
        call_locks={},
    ).call_tool("broker.search_tools", {"query": ""})

    assert calls == ["broken-store", "read-store"]
    assert [match["name"] for match in result["structuredContent"]["matches"]] == [
        "broken-store",
        "read.find",
    ]
    assert result["structuredContent"]["skipped_upstreams"] == {
        "broken-store": "missing token"
    }

def test_catalog_listing_continues_after_profile_hidden_upstream(tmp_path: Path) -> None:
    config = BrokerConfig(
        runtime=_runtime(tmp_path),
        broker=BrokerSettings(),
        profiles={
            "default-llm": ToolExposureProfile(
                name="default-llm",
                max_tools=20,
                allow_mutating_upstreams=("write-store",),
            ),
            "other-llm": ToolExposureProfile(name="other-llm", max_tools=20),
        },
        upstreams={
            "other-profile-store": UpstreamConfig(
                name="other-profile-store",
                command="other-profile-store",
                profiles=("other-llm",),
            ),
            "write-store": UpstreamConfig(
                name="write-store",
                command="write-store",
                tool_prefix="write",
                profiles=("default-llm",),
                mutating=True,
            ),
            "read-store": UpstreamConfig(
                name="read-store",
                command="read-store",
                tool_prefix="read",
                profiles=("default-llm",),
            ),
        },
    )
    calls: list[str] = []

    result = BrokerCatalogFacade(
        broker_config=config,
        profile=config.profiles["default-llm"],
        list_upstream=lambda name, _timeout: calls.append(name) or [{"name": "tool"}],
        call_upstream=lambda _name, _tool, _args, _timeout: {"content": []},
        call_locks={},
    ).call_tool("broker.search_tools", {"query": ""})

    assert calls == ["write-store", "read-store"]
    # Empty query scores all entries equally, so matches are ordered by name (asc).
    assert [match["name"] for match in result["structuredContent"]["matches"]] == [
        "read.tool",
        "write.tool",
    ]
