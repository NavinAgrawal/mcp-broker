import json
from argparse import Namespace
from pathlib import Path
import pytest
from mcp_broker.facade_smoke import (
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    FacadeSmokeError,
    _ConfiguredFacadeProbe,
    _call_payload,
    _cleanup_smoke_daemon,
    _describe_payload,
    _empty_to_none,
    _exercise_client_shim,
    _request_through_client,
    _parse_args,
    _resolve_facade_probe,
    _run_smoke,
    _initialize_payload,
    _search_payload,
    _smoke_request,
    _start_daemon_if_needed,
    _stop_smoke_session,
    _raise_on_error,
    _tools_list_payload,
    build_facade_smoke_report,
    parse_call_args,
)
pytestmark = [pytest.mark.unit, pytest.mark.error_simulation]
def _status_broker_config():
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig

    return BrokerConfig(
        runtime=RuntimeConfig.from_mapping({"root": "/tmp/mcp-broker-test"}),
        broker=BrokerSettings(),
        upstreams={
            "read-store": UpstreamConfig(
                name="read-store",
                command="read-store",
                profiles=("llm-profile",),
                purpose="Memory upstream.",
            ),
            "browser-session": UpstreamConfig(
                name="browser-session",
                command="browser-session",
                mode="per_session",
                profiles=("llm-profile",),
            ),
            "missing-auth": UpstreamConfig(
                name="missing-auth",
                command="missing-auth",
                profiles=("llm-profile",),
            ),
            "disabled": UpstreamConfig(
                name="disabled",
                command="disabled",
                enabled=False,
                profiles=("llm-profile",),
            ),
            "other-profile-only": UpstreamConfig(
                name="other-profile-only",
                command="other-profile-only",
                profiles=("other-profile",),
            ),
        },
    )
def _status_health_snapshot(
    _visible_upstreams: set[str] | None = None,
) -> dict[str, dict[str, object]]:
    return {
        "read-store": {
            "state": "running",
            "pid": 123,
            "restarts": 1,
            "last_error": None,
            "auth_state": "authenticated",
            "auth_repair_attempts": 2,
            "auth_repair_successes": 1,
            "auth_repair_failures": 1,
        },
        "browser-session": {
            "state": "running",
            "pid": None,
            "restarts": 0,
            "last_error": None,
            "sessions": 2,
        },
        "missing-auth": {
            "state": "configured",
            "pid": None,
            "restarts": 0,
            "last_error": "missing environment variable: API_TOKEN required for auth",
        },
        "disabled": {"state": "disabled", "last_error": None},
        "other-profile-only": {"state": "configured", "last_error": None},
    }
def _expected_status_payload() -> dict[str, object]:
    from mcp_broker.config_identity import (
        CONFIG_SCHEMA_VERSION,
        DEFAULT_BROKER_ENVIRONMENT,
        DEFAULT_BROKER_ID,
        DEFAULT_BUNDLE_VERSION,
    )

    return {
        "identity": {
            "active_profile": "llm-profile",
            "active_profiles": [],
            "broker_id": DEFAULT_BROKER_ID,
            "bundle_version": DEFAULT_BUNDLE_VERSION,
            "environment": DEFAULT_BROKER_ENVIRONMENT,
            "schema_version": CONFIG_SCHEMA_VERSION,
        },
        "profile": "llm-profile",
        "socket_path": "/tmp/mcp-broker-test/sockets/broker.sock",
        "status": "degraded",
        "upstreams": {
            "disabled": {
                "enabled": False,
                "auth_probe": "none",
                "auth_repair_attempts": 0,
                "auth_repair_failures": 0,
                "auth_repair_successes": 0,
                "auth_state": "unknown",
                "exposed": False,
                "last_error": None,
                "mode": "shared",
                "mutating": False,
                "pid": None,
                "restarts": None,
                "session_count": 0,
                "active_call_count": 0,
                "state": "disabled",
                "transport": "stdio",
            },
            "missing-auth": {
                "enabled": True,
                "auth_probe": "none",
                "auth_repair_attempts": 0,
                "auth_repair_failures": 0,
                "auth_repair_successes": 0,
                "auth_state": "unauthenticated",
                "exposed": True,
                "last_error": "missing environment variable: API_TOKEN required for auth",
                "mode": "shared",
                "mutating": False,
                "pid": None,
                "restarts": 0,
                "session_count": 0,
                "active_call_count": 0,
                "state": "configured",
                "transport": "stdio",
            },
            "read-store": {
                "enabled": True,
                "auth_probe": "none",
                "auth_repair_attempts": 2,
                "auth_repair_failures": 1,
                "auth_repair_successes": 1,
                "auth_state": "authenticated",
                "exposed": True,
                "last_error": None,
                "mode": "shared",
                "mutating": False,
                "pid": 123,
                "restarts": 1,
                "session_count": 0,
                "active_call_count": 0,
                "state": "running",
                "transport": "stdio",
            },
            "browser-session": {
                "enabled": True,
                "auth_probe": "none",
                "auth_repair_attempts": 0,
                "auth_repair_failures": 0,
                "auth_repair_successes": 0,
                "auth_state": "unknown",
                "exposed": True,
                "last_error": None,
                "mode": "per_session",
                "mutating": False,
                "pid": None,
                "restarts": 0,
                "session_count": 2,
                "active_call_count": 0,
                "state": "running",
                "transport": "stdio",
            },
        },
    }

def test_facade_run_smoke_wires_config_daemon_probe_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import mcp_broker.facade_smoke as facade_smoke
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig

    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={},
    )
    events: list[tuple[str, object]] = []

    class NewDaemon:
        def __init__(self, **kwargs: object) -> None:
            events.append(("daemon-init", kwargs))

        def start(self) -> None:
            events.append(("daemon-start", None))

    class FixedUuid:
        hex = "abc123"

    def fake_exercise(**kwargs: object) -> dict[str, dict[str, object]]:
        events.append(("exercise", kwargs))
        return {
            "tools/list": {"result": {"tools": []}},
            "broker.search_tools": {"result": {"content": [{"type": "text", "text": '{"matches": []}'}]}},
            "broker.describe_tool": {
                "result": {"content": [{"type": "text", "text": '{"tool": {"name": "fake.echo"}}'}]}
            },
            "fake.echo": {"id": "fake.echo", "result": {"content": []}},
        }

    def fake_cleanup(*args: object, **kwargs: object) -> None:
        events.append(("cleanup", (args, kwargs)))

    monkeypatch.setattr(facade_smoke.BrokerConfig, "from_file", lambda path: events.append(("config", path)) or config)
    monkeypatch.setattr(facade_smoke, "BrokerDaemon", NewDaemon)
    monkeypatch.setattr(facade_smoke, "uuid4", lambda: FixedUuid())
    monkeypatch.setattr(facade_smoke, "_exercise_client_shim", fake_exercise)
    monkeypatch.setattr(facade_smoke, "_cleanup_smoke_daemon", fake_cleanup)

    report = _run_smoke(
        Namespace(
            config=str(tmp_path / "broker.yaml"),
            profile="llm",
            query="echo",
            call_tool="fake.echo",
            call_args='{"value": true}',
            request_timeout_seconds=31,
        )
    )

    assert report["started_daemon"] is True
    assert report["profile"] == "llm"
    assert events[0] == ("config", Path(tmp_path / "broker.yaml"))
    assert events[1] == (
        "daemon-init",
        {
            "runtime_root": tmp_path / "runtime",
            "socket_path": tmp_path / "broker.sock",
            "broker_config": config,
        },
    )
    assert events[3] == (
        "exercise",
        {
            "socket_path": tmp_path / "broker.sock",
            "profile": "llm",
            "query": "echo",
            "call_tool": "fake.echo",
            "call_args": {"value": True},
            "session_id": "facade-smoke-abc123",
            "timeout_seconds": 31,
        },
    )
    cleanup_args, cleanup_kwargs = events[4][1]
    assert cleanup_args[:3] == (
        tmp_path / "broker.sock",
        "llm",
        "facade-smoke-abc123",
    )
    assert cleanup_args[3] is not None
    assert cleanup_kwargs == {"started_daemon": True}

def test_facade_run_smoke_passes_config_and_profile_to_probe_resolver(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import mcp_broker.facade_smoke as facade_smoke
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig

    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={},
    )
    observed: dict[str, object] = {}

    class Daemon:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            pass

    def fake_resolve(**kwargs: object) -> _ConfiguredFacadeProbe:
        observed["resolve"] = kwargs
        return _ConfiguredFacadeProbe(
            query="resolved query",
            call_tool="resolved.echo",
            call_args={"value": True},
        )

    def fake_exercise(**kwargs: object) -> dict[str, dict[str, object]]:
        observed["exercise"] = kwargs
        return {
            "tools/list": {"result": {"tools": []}},
            "broker.search_tools": {"result": {"content": [{"type": "text", "text": '{"matches": []}'}]}},
            "broker.describe_tool": {
                "result": {"content": [{"type": "text", "text": '{"tool": {"name": "resolved.echo"}}'}]}
            },
            "resolved.echo": {"id": "resolved.echo", "result": {"content": []}},
        }

    monkeypatch.setattr(facade_smoke.BrokerConfig, "from_file", lambda _path: config)
    monkeypatch.setattr(facade_smoke, "BrokerDaemon", Daemon)
    monkeypatch.setattr(facade_smoke, "_resolve_facade_probe", fake_resolve)
    monkeypatch.setattr(facade_smoke, "_exercise_client_shim", fake_exercise)
    monkeypatch.setattr(facade_smoke, "_cleanup_smoke_daemon", lambda *_args, **_kwargs: None)

    report = _run_smoke(
        Namespace(
            config=str(tmp_path / "broker.yaml"),
            profile="llm",
            query="cli query",
            call_tool="cli.echo",
            call_args='{"cli": true}',
            request_timeout_seconds=44,
        )
    )

    assert observed["resolve"] == {
        "config": config,
        "profile": "llm",
        "query": "cli query",
        "call_tool": "cli.echo",
        "call_args": '{"cli": true}',
    }
    assert observed["exercise"]["query"] == "resolved query"
    assert observed["exercise"]["call_tool"] == "resolved.echo"
    assert observed["exercise"]["call_args"] == {"value": True}
    assert report["called_tool"] == "resolved.echo"

def test_facade_run_smoke_cleans_up_as_reused_daemon_when_start_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import mcp_broker.facade_smoke as facade_smoke
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig

    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={},
    )
    cleanup: dict[str, object] = {}

    class Daemon:
        def __init__(self, **_kwargs: object) -> None:
            pass

    def fail_start(_daemon: object) -> bool:
        raise FacadeSmokeError("start failed")

    def fake_cleanup(*args: object, **kwargs: object) -> None:
        cleanup["args"] = args
        cleanup["kwargs"] = kwargs

    monkeypatch.setattr(facade_smoke.BrokerConfig, "from_file", lambda _path: config)
    monkeypatch.setattr(facade_smoke, "BrokerDaemon", Daemon)
    monkeypatch.setattr(
        facade_smoke,
        "_resolve_facade_probe",
        lambda **_kwargs: _ConfiguredFacadeProbe("query", "fake.echo", {}),
    )
    monkeypatch.setattr(facade_smoke, "_start_daemon_if_needed", fail_start)
    monkeypatch.setattr(facade_smoke, "_cleanup_smoke_daemon", fake_cleanup)

    with pytest.raises(FacadeSmokeError, match="start failed"):
        _run_smoke(
            Namespace(
                config=str(tmp_path / "broker.yaml"),
                profile="llm",
                query="echo",
                call_tool="fake.echo",
                call_args="{}",
                request_timeout_seconds=31,
            )
        )

    cleanup_args = cleanup["args"]
    assert cleanup_args[:3] == (tmp_path / "broker.sock", "llm", cleanup_args[2])
    assert cleanup["kwargs"] == {"started_daemon": False}

def test_broker_status_reports_profile_upstream_visibility_without_listing_tools() -> None:
    from mcp_broker.catalog import BrokerCatalogFacade
    from mcp_broker.profiles import ToolExposureProfile

    listed_upstreams: list[str] = []
    visible_sets: list[set[str] | None] = []

    def status_health_snapshot(visible_upstreams: set[str] | None) -> dict[str, dict[str, object]]:
        visible_sets.append(visible_upstreams)
        return _status_health_snapshot(visible_upstreams)

    result = BrokerCatalogFacade(
        broker_config=_status_broker_config(),
        profile=ToolExposureProfile(name="llm-profile", max_tools=80, compact_tools_enabled=True),
        list_upstream=lambda name, timeout: listed_upstreams.append(name) or [],
        call_upstream=lambda name, tool, args, timeout: {"content": []},
        call_locks={},
        status_provider=status_health_snapshot,
    ).call_tool("broker.status", {})

    payload = json.loads(result["content"][0]["text"])

    assert listed_upstreams == []
    assert visible_sets == [{"browser-session", "missing-auth", "read-store"}]
    assert payload == _expected_status_payload()

def test_broker_facade_accepts_canonical_names_without_profile() -> None:
    from mcp_broker.broker import BrokerToolError
    from mcp_broker.catalog import BrokerCatalogFacade

    facade = BrokerCatalogFacade(
        broker_config=_status_broker_config(),
        profile=None,
        list_upstream=lambda _name, _timeout: [],
        call_upstream=lambda _name, _tool, _args, _timeout: {"content": []},
        call_locks={},
    )

    with pytest.raises(BrokerToolError, match="unknown broker tool: broker.unknown"):
        facade.call_tool("broker.unknown", {})

def test_broker_status_ignores_client_control_arguments() -> None:
    from mcp_broker.catalog import BrokerCatalogFacade
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig
    from mcp_broker.profiles import ToolExposureProfile

    facade = BrokerCatalogFacade(
        broker_config=BrokerConfig(
            runtime=RuntimeConfig.from_mapping({"root": "/tmp/mcp-broker-test"}),
            broker=BrokerSettings(),
            upstreams={},
        ),
        profile=ToolExposureProfile(name="llm-profile", max_tools=80, compact_tools_enabled=True),
        list_upstream=lambda name, timeout: [],
        call_upstream=lambda name, tool, args, timeout: {"content": []},
        call_locks={},
    )

    result = facade.call_tool("broker.status", {"wait_for_previous": True})

    payload = json.loads(result["content"][0]["text"])
    assert payload["profile"] == "llm-profile"

def test_broker_status_rejects_status_arguments() -> None:
    from mcp_broker.catalog import BrokerCatalogFacade
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig
    from mcp_broker.profiles import ToolExposureProfile

    facade = BrokerCatalogFacade(
        broker_config=BrokerConfig(
            runtime=RuntimeConfig.from_mapping({"root": "/tmp/mcp-broker-test"}),
            broker=BrokerSettings(),
            upstreams={},
        ),
        profile=ToolExposureProfile(name="llm-profile", max_tools=80, compact_tools_enabled=True),
        list_upstream=lambda name, timeout: [],
        call_upstream=lambda name, tool, args, timeout: {"content": []},
        call_locks={},
    )

    with pytest.raises(ValueError, match="broker.status does not accept arguments"):
        facade.call_tool("broker.status", {"verbose": True})

def test_broker_status_keeps_non_auth_errors_unknown() -> None:
    from mcp_broker.catalog import BrokerCatalogFacade
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig
    from mcp_broker.profiles import ToolExposureProfile

    result = BrokerCatalogFacade(
        broker_config=BrokerConfig(
            runtime=RuntimeConfig.from_mapping({"root": "/tmp/mcp-broker-test"}),
            broker=BrokerSettings(),
            upstreams={
                "display": UpstreamConfig(
                    name="display",
                    command="display",
                    profiles=("llm-profile",),
                )
            },
        ),
        profile=ToolExposureProfile(name="llm-profile", max_tools=80, compact_tools_enabled=True),
        list_upstream=lambda name, timeout: [],
        call_upstream=lambda name, tool, args, timeout: {"content": []},
        call_locks={},
        status_provider=lambda _visible_upstreams: {
            "display": {"last_error": "missing environment variable: DISPLAY"},
        },
    ).call_tool("broker.status", {})

    payload = json.loads(result["content"][0]["text"])

    assert payload["upstreams"]["display"]["auth_state"] == "unknown"

def test_broker_status_reports_degraded_when_upstream_state_failed_without_error() -> None:
    from mcp_broker.catalog import BrokerCatalogFacade
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig
    from mcp_broker.profiles import ToolExposureProfile

    result = BrokerCatalogFacade(
        broker_config=BrokerConfig(
            runtime=RuntimeConfig.from_mapping({"root": "/tmp/mcp-broker-test"}),
            broker=BrokerSettings(),
            upstreams={
                "worker": UpstreamConfig(
                    name="worker",
                    command="worker",
                    profiles=("llm-profile",),
                )
            },
        ),
        profile=ToolExposureProfile(name="llm-profile", max_tools=80, compact_tools_enabled=True),
        list_upstream=lambda name, timeout: [],
        call_upstream=lambda name, tool, args, timeout: {"content": []},
        call_locks={},
        status_provider=lambda _visible_upstreams: {
            "worker": {"state": "failed", "last_error": None},
        },
    ).call_tool("broker.status", {})

    payload = json.loads(result["content"][0]["text"])

    assert payload["status"] == "degraded"
    assert payload["upstreams"]["worker"]["state"] == "failed"

def test_dotted_profile_keeps_broker_tool_names_canonical() -> None:
    from mcp_broker.profiles import ToolExposureProfile

    profile = ToolExposureProfile(
        name="llm-profile",
        max_tools=80,
        compact_tools_enabled=True,
        broker_tool_name_style="dotted",
    )

    assert profile.exposed_broker_tool_name("broker.status") == "broker.status"
