import json
from argparse import Namespace
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
                "state": "running",
                "transport": "stdio",
            },
        },
    }

def test_parse_call_args_requires_json_object() -> None:
    assert parse_call_args('{"message":"hello"}') == {"message": "hello"}
    assert parse_call_args('{"nested":{"enabled":true},"items":[1,2]}') == {
        "nested": {"enabled": True},
        "items": [1, 2],
    }

    with pytest.raises(ValueError, match="call args must be a JSON object") as exc_info:
        parse_call_args('["bad"]')
    assert str(exc_info.value) == "call args must be a JSON object"

def test_parse_args_keeps_smoke_probe_optional_and_configures_timeout() -> None:
    args = _parse_args(
        [
            "--config",
            "config/broker.example.yaml",
            "--profile",
            "llm-profile",
            "--request-timeout-seconds",
            "9",
        ]
    )

    assert args.config == "config/broker.example.yaml"
    assert args.profile == "llm-profile"
    assert args.query is None
    assert args.call_tool is None
    assert args.call_args is None
    assert args.request_timeout_seconds == 9

def test_parse_args_requires_config_and_preserves_defaults() -> None:
    args = _parse_args(["--config", "config/broker.example.yaml"])

    assert args.config == "config/broker.example.yaml"
    assert args.profile == "codex"
    assert args.query is None
    assert args.call_tool is None
    assert args.call_args is None
    assert args.request_timeout_seconds == DEFAULT_REQUEST_TIMEOUT_SECONDS

    with pytest.raises(SystemExit) as exc_info:
        _parse_args([])
    assert exc_info.value.code == 2

def test_parse_args_help_names_facade_smoke_purpose(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        _parse_args(["--help"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 0
    assert "Exercise compact Codex broker facade" in captured.out
    assert "XX" not in captured.out

def test_resolve_facade_probe_uses_configured_profile_smoke_probe() -> None:
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig
    from mcp_broker.schema import SmokeProbe

    config = BrokerConfig(
        runtime=RuntimeConfig.from_mapping({"root": "/tmp/mcp-broker-test"}),
        broker=BrokerSettings(),
        upstreams={
            "later-store": UpstreamConfig(
                name="later-store",
                command="later-store",
                profiles=("llm-profile",),
                smoke=SmokeProbe(
                    query="later query",
                    tool="later-store.read",
                    arguments={"later": True},
                ),
            ),
            "first-store": UpstreamConfig(
                name="first-store",
                command="first-store",
                profiles=("llm-profile",),
                smoke=SmokeProbe(
                    query="first query",
                    tool="first-store.read",
                    arguments={"first": True},
                ),
            ),
            "other-profile": UpstreamConfig(
                name="other-profile",
                command="other-profile",
                profiles=("other-profile",),
                smoke=SmokeProbe(query="other query", tool="other.read", arguments={}),
            ),
            "disabled-store": UpstreamConfig(
                name="disabled-store",
                command="disabled-store",
                enabled=False,
                profiles=("llm-profile",),
                smoke=SmokeProbe(query="disabled query", tool="disabled.read", arguments={}),
            ),
            "describe-only": UpstreamConfig(
                name="describe-only",
                command="describe-only",
                profiles=("llm-profile",),
                smoke=SmokeProbe(
                    query="describe query",
                    tool="describe-only.read",
                    arguments={},
                    call=False,
                ),
            ),
        },
    )

    assert _resolve_facade_probe(
        config=config,
        profile="llm-profile",
        query=None,
        call_tool=None,
        call_args=None,
    ) == _ConfiguredFacadeProbe(
        query="first query",
        call_tool="first-store.read",
        call_args={"first": True},
    )

def test_resolve_facade_probe_rejects_partial_explicit_probe() -> None:
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig

    config = BrokerConfig(
        runtime=RuntimeConfig.from_mapping({"root": "/tmp/mcp-broker-test"}),
        broker=BrokerSettings(),
        upstreams={},
    )

    with pytest.raises(FacadeSmokeError, match="provide query, call-tool, and call-args") as exc_info:
        _resolve_facade_probe(
            config=config,
            profile="llm-profile",
            query="repo",
            call_tool="fake.echo",
            call_args=None,
        )
    assert str(exc_info.value) == (
        "provide query, call-tool, and call-args together or omit all to use YAML smoke"
    )

def test_resolve_facade_probe_uses_explicit_probe_without_config_inventory() -> None:
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig

    config = BrokerConfig(
        runtime=RuntimeConfig.from_mapping({"root": "/tmp/mcp-broker-test"}),
        broker=BrokerSettings(),
        upstreams={},
    )

    assert _resolve_facade_probe(
        config=config,
        profile="llm-profile",
        query="explicit query",
        call_tool="generic.echo",
        call_args='{"value":"ok"}',
    ) == _ConfiguredFacadeProbe(
        query="explicit query",
        call_tool="generic.echo",
        call_args={"value": "ok"},
    )

def test_resolve_facade_probe_rejects_profiles_without_callable_smoke_probe() -> None:
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig
    from mcp_broker.schema import SmokeProbe

    config = BrokerConfig(
        runtime=RuntimeConfig.from_mapping({"root": "/tmp/mcp-broker-test"}),
        broker=BrokerSettings(),
        upstreams={
            "describe-only": UpstreamConfig(
                name="describe-only",
                command="describe-only",
                profiles=("llm-profile",),
                smoke=SmokeProbe(
                    query="describe query",
                    tool="describe-only.read",
                    arguments={},
                    call=False,
                ),
            ),
            "disabled-mode": UpstreamConfig(
                name="disabled-mode",
                command="disabled-mode",
                mode="disabled",
                profiles=("llm-profile",),
                smoke=SmokeProbe(query="disabled query", tool="disabled.read", arguments={}),
            )
        },
    )

    with pytest.raises(FacadeSmokeError, match="llm-profile has no callable smoke probe"):
        _resolve_facade_probe(
            config=config,
            profile="llm-profile",
            query=None,
            call_tool=None,
            call_args=None,
        )

def test_resolve_facade_probe_rejects_callable_smoke_for_other_profile() -> None:
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig
    from mcp_broker.schema import SmokeProbe

    config = BrokerConfig(
        runtime=RuntimeConfig.from_mapping({"root": "/tmp/mcp-broker-test"}),
        broker=BrokerSettings(),
        upstreams={
            "other-profile": UpstreamConfig(
                name="other-profile",
                command="other-profile",
                profiles=("other-profile",),
                smoke=SmokeProbe(query="other query", tool="other.read", arguments={}),
            ),
        },
    )

    with pytest.raises(FacadeSmokeError) as exc_info:
        _resolve_facade_probe(
            config=config,
            profile="llm-profile",
            query=None,
            call_tool=None,
            call_args=None,
        )

    assert str(exc_info.value) == "llm-profile has no callable smoke probe"

def test_empty_to_none_only_normalizes_missing_explicit_values() -> None:
    assert _empty_to_none(None) is None
    assert _empty_to_none("") is None
    assert _empty_to_none(" ") == " "
    assert _empty_to_none("query") == "query"

def test_facade_payload_helpers_build_expected_jsonrpc_messages() -> None:
    assert _initialize_payload() == {
        "jsonrpc": "2.0",
        "id": "initialize",
        "method": "initialize",
        "params": {"protocolVersion": "2025-11-25"},
    }
    assert _tools_list_payload() == {
        "jsonrpc": "2.0",
        "id": "tools/list",
        "method": "tools/list",
    }
    assert _search_payload("read-store") == {
        "jsonrpc": "2.0",
        "id": "broker.search_tools",
        "method": "tools/call",
        "params": {
            "name": "broker.search_tools",
            "arguments": {"query": "read-store", "limit": 10},
        },
    }
    assert _describe_payload("read-store.get_project_scope") == {
        "jsonrpc": "2.0",
        "id": "broker.describe_tool",
        "method": "tools/call",
        "params": {
            "name": "broker.describe_tool",
            "arguments": {"name": "read-store.get_project_scope"},
        },
    }
    assert _call_payload("read-store.get_project_scope", {"scope": "project"}) == {
        "jsonrpc": "2.0",
        "id": "read-store.get_project_scope",
        "method": "tools/call",
        "params": {
            "name": "broker.call_tool",
            "arguments": {
                "name": "read-store.get_project_scope",
                "arguments": {"scope": "project"},
            },
        },
    }

def test_build_facade_smoke_report_summarizes_compact_facade_path() -> None:
    report = build_facade_smoke_report(
        profile="llm-profile",
        list_response={
            "result": {
                "tools": [
                    {"name": "broker.search_tools"},
                    {"name": "broker.call_tool"},
                    {"name": "broker.describe_tool"},
                ]
            }
        },
        search_response={
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"matches": [{"name": "fake.echo"}]}),
                    }
                ]
            }
        },
        describe_response={
            "result": {
                "content": [
                    {"type": "text", "text": json.dumps({"tool": {"name": "fake.echo"}})}
                ]
            }
        },
        call_response={
            "id": "fake.echo",
            "result": {"content": [{"type": "text", "text": "hello"}]},
        },
        started_daemon=True,
    )

    assert report == {
        "profile": "llm-profile",
        "advertised_tools": [
            "broker.call_tool",
            "broker.describe_tool",
            "broker.search_tools",
        ],
        "search_hit_count": 1,
        "described_tool": "fake.echo",
        "called_tool": "fake.echo",
        "call_text": "hello",
        "started_daemon": True,
    }

def test_build_facade_smoke_report_rejects_upstream_error_content() -> None:
    with pytest.raises(FacadeSmokeError, match="fake.echo returned upstream error"):
        build_facade_smoke_report(
            profile="llm-profile",
            list_response={"result": {"tools": [{"name": "broker.call_tool"}]}},
            search_response={
                "result": {
                    "content": [
                        {"type": "text", "text": json.dumps({"matches": [{"name": "fake.echo"}]})}
                    ]
                }
            },
            describe_response={
                "result": {
                    "content": [
                        {"type": "text", "text": json.dumps({"tool": {"name": "fake.echo"}})}
                    ]
                }
            },
            call_response={
                "id": "fake.echo",
                "result": {"content": [{"type": "text", "text": "Error: missing value"}]},
            },
            started_daemon=False,
        )

def test_build_facade_smoke_report_rejects_jsonrpc_tool_errors() -> None:
    with pytest.raises(FacadeSmokeError, match="generic.echo returned upstream error"):
        build_facade_smoke_report(
            profile="llm-profile",
            list_response={"result": {"tools": [{"name": "broker.call_tool"}]}},
            search_response={
                "result": {
                    "content": [
                        {"type": "text", "text": json.dumps({"matches": [{"name": "generic.echo"}]})}
                    ]
                }
            },
            describe_response={
                "result": {
                    "content": [
                        {"type": "text", "text": json.dumps({"tool": {"name": "generic.echo"}})}
                    ]
                }
            },
            call_response={
                "id": "generic.echo",
                "result": {"content": [], "isError": True},
            },
            started_daemon=False,
        )

def test_build_facade_smoke_report_preserves_empty_call_text() -> None:
    report = build_facade_smoke_report(
        profile="llm-profile",
        list_response={"result": {"tools": [{"name": "broker.call_tool"}]}},
        search_response={
            "result": {"content": [{"type": "text", "text": json.dumps({"matches": []})}]}
        },
        describe_response={
            "result": {"content": [{"type": "text", "text": json.dumps({"tool": {"name": "fake.echo"}})}]}
        },
        call_response={"id": "fake.echo", "result": {"content": []}},
        started_daemon=False,
    )

    assert report["call_text"] == ""

def test_facade_smoke_main_reports_invalid_call_args(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import mcp_broker.facade_smoke as facade_smoke

    def raise_value_error(_args: object) -> dict:
        raise ValueError("call args must be a JSON object")

    monkeypatch.setattr(facade_smoke, "_run_smoke", raise_value_error)

    result = facade_smoke.main(
        [
            "--config",
            "/tmp/broker.yaml",
            "--query",
            "echo",
            "--call-tool",
            "fake.echo",
            "--call-args",
            "[]",
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert captured.err == "call args must be a JSON object\n"

def test_facade_smoke_main_passes_parsed_args_to_runner(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import mcp_broker.facade_smoke as facade_smoke

    observed: dict[str, object] = {}

    def fake_run_smoke(args: Namespace) -> dict[str, object]:
        observed["args"] = args
        return {"ok": True}

    monkeypatch.setattr(facade_smoke, "_run_smoke", fake_run_smoke)

    result = facade_smoke.main(["--config", "broker.yaml", "--profile", "llm"])

    captured = capsys.readouterr()
    args = observed["args"]
    assert result == 0
    assert args.config == "broker.yaml"
    assert args.profile == "llm"
    assert captured.out == '{"ok": true}\n'

def test_facade_smoke_main_writes_sorted_json_on_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import mcp_broker.facade_smoke as facade_smoke

    monkeypatch.setattr(
        facade_smoke,
        "_run_smoke",
        lambda _args: {"zeta": 1, "alpha": 2},
    )

    result = facade_smoke.main(["--config", "config/broker.example.yaml"])

    captured = capsys.readouterr()
    assert result == 0
    assert captured.err == ""
    assert captured.out == '{"alpha": 2, "zeta": 1}\n'

def test_facade_start_daemon_accepts_existing_daemon() -> None:
    import mcp_broker.facade_smoke as facade_smoke

    class AlreadyRunningDaemon:
        def start(self) -> None:
            raise facade_smoke.BrokerDaemonError("broker daemon already running")

    assert facade_smoke._start_daemon_if_needed(AlreadyRunningDaemon()) is False

def test_facade_start_daemon_returns_true_after_starting_new_daemon() -> None:
    import mcp_broker.facade_smoke as facade_smoke

    class NewDaemon:
        def __init__(self) -> None:
            self.started = False

        def start(self) -> None:
            self.started = True

    daemon = NewDaemon()

    assert facade_smoke._start_daemon_if_needed(daemon) is True
    assert daemon.started is True

def test_facade_start_daemon_rethrows_unexpected_errors() -> None:
    import mcp_broker.facade_smoke as facade_smoke

    class BrokenDaemon:
        def start(self) -> None:
            raise facade_smoke.BrokerDaemonError("bind failed")

    with pytest.raises(facade_smoke.BrokerDaemonError, match="bind failed"):
        facade_smoke._start_daemon_if_needed(BrokenDaemon())
