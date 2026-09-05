import json
from argparse import Namespace
from pathlib import Path
import subprocess
import runpy
import sys
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

def test_facade_request_through_client_reports_subprocess_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="client failed")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(FacadeSmokeError, match="client failed"):
        _request_through_client(
            socket_path=Path("/tmp/broker.sock"),
            profile="llm",
            session_id="session",
            payload={"id": "one"},
        )

def test_facade_request_through_client_uses_default_failure_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(FacadeSmokeError) as exc_info:
        _request_through_client(
            socket_path=Path("/tmp/broker.sock"),
            profile="llm",
            session_id="session",
            payload={"id": "one"},
        )

    assert str(exc_info.value) == "client shim failed"

def test_facade_request_through_client_sends_exact_client_command_and_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed["args"] = args
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"result":{"ok":true}}',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    payload = {"jsonrpc": "2.0", "id": "request-id", "method": "tools/list"}
    response = _request_through_client(
        socket_path=tmp_path / "broker.sock",
        profile="llm-profile",
        session_id="session-1",
        payload=payload,
        timeout_seconds=11,
    )

    assert response == {"result": {"ok": True}}
    command = observed["args"][0]
    assert command == [
        subprocess.sys.executable,
        "-m",
        "mcp_broker.client",
        "--socket-path",
        str(tmp_path / "broker.sock"),
        "--profile",
        "llm-profile",
        "--session-id",
        "session-1",
    ]
    assert observed["kwargs"]["input"] == json.dumps(payload) + "\n"
    assert observed["kwargs"]["timeout"] == 11
    assert observed["kwargs"]["text"] is True
    assert observed["kwargs"]["check"] is False
    assert observed["kwargs"]["stdout"] == subprocess.PIPE
    assert observed["kwargs"]["stderr"] == subprocess.PIPE

def test_facade_request_through_client_reports_invalid_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="not-json", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(FacadeSmokeError, match="invalid client response"):
        _request_through_client(
            socket_path=Path("/tmp/broker.sock"),
            profile="llm",
            session_id="session",
            payload={"id": "one"},
        )

def test_facade_request_through_client_reports_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(FacadeSmokeError) as exc_info:
        _request_through_client(
            socket_path=Path("/tmp/broker.sock"),
            profile="llm",
            session_id="session",
            payload={"id": "one"},
            timeout_seconds=3,
        )
    assert str(exc_info.value) == "client shim timed out after 3s for one"

    with pytest.raises(FacadeSmokeError) as fallback_exc:
        _request_through_client(
            socket_path=Path("/tmp/broker.sock"),
            profile="llm",
            session_id="session",
            payload={},
            timeout_seconds=3,
        )
    assert str(fallback_exc.value) == "client shim timed out after 3s for request"

def test_facade_raise_on_error_maps_jsonrpc_error() -> None:
    with pytest.raises(FacadeSmokeError) as exc_info:
        _raise_on_error({"error": {"message": "bad upstream", "code": -32000}})
    assert str(exc_info.value) == '{"code": -32000, "message": "bad upstream"}'

def test_facade_smoke_request_passes_timeout_to_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import mcp_broker.facade_smoke as facade_smoke

    observed: dict[str, object] = {}

    def fake_request(**kwargs: object) -> dict[str, object]:
        observed.update(kwargs)
        return {"result": {}}

    monkeypatch.setattr(facade_smoke, "_request_through_client", fake_request)

    assert facade_smoke._smoke_request(
        tmp_path / "broker.sock",
        "llm-profile",
        "session-1",
        {"id": "request-id"},
        timeout_seconds=17,
    ) == {"result": {}}
    assert observed["timeout_seconds"] == 17
    assert observed["socket_path"] == tmp_path / "broker.sock"
    assert observed["profile"] == "llm-profile"
    assert observed["session_id"] == "session-1"
    assert observed["payload"] == {"id": "request-id"}

def test_facade_exercise_client_shim_sends_requests_in_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import mcp_broker.facade_smoke as facade_smoke

    calls: list[dict[str, object]] = []

    def fake_smoke_request(
        socket_path: Path,
        profile: str,
        session_id: str,
        payload: dict[str, object],
        *,
        timeout_seconds: int,
    ) -> dict[str, object]:
        calls.append(
            {
                "socket_path": socket_path,
                "profile": profile,
                "session_id": session_id,
                "payload": payload,
                "timeout_seconds": timeout_seconds,
            }
        )
        return {"id": payload["id"], "result": {"content": []}}

    monkeypatch.setattr(facade_smoke, "_smoke_request", fake_smoke_request)

    responses = facade_smoke._exercise_client_shim(
        socket_path=tmp_path / "broker.sock",
        profile="llm-profile",
        query="read-store",
        call_tool="read-store.fetch",
        call_args={"id": "abc"},
        session_id="session-1",
        timeout_seconds=23,
    )

    assert [call["payload"]["id"] for call in calls] == [
        "initialize",
        "tools/list",
        "broker.search_tools",
        "broker.describe_tool",
        "read-store.fetch",
    ]
    assert [call["payload"] for call in calls] == [
        facade_smoke._initialize_payload(),
        facade_smoke._tools_list_payload(),
        facade_smoke._search_payload("read-store"),
        facade_smoke._describe_payload("read-store.fetch"),
        facade_smoke._call_payload("read-store.fetch", {"id": "abc"}),
    ]
    assert {call["socket_path"] for call in calls} == {tmp_path / "broker.sock"}
    assert {call["profile"] for call in calls} == {"llm-profile"}
    assert {call["session_id"] for call in calls} == {"session-1"}
    assert {call["timeout_seconds"] for call in calls} == {23}
    assert responses == {
        "tools/list": {"id": "tools/list", "result": {"content": []}},
        "broker.search_tools": {"id": "broker.search_tools", "result": {"content": []}},
        "broker.describe_tool": {"id": "broker.describe_tool", "result": {"content": []}},
        "read-store.fetch": {"id": "read-store.fetch", "result": {"content": []}},
    }

def test_facade_exercise_client_shim_stops_on_first_jsonrpc_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import mcp_broker.facade_smoke as facade_smoke

    calls: list[str] = []

    def fake_smoke_request(
        _socket_path: Path,
        _profile: str,
        _session_id: str,
        payload: dict[str, object],
        *,
        timeout_seconds: int,
    ) -> dict[str, object]:
        calls.append(str(payload["id"]))
        if payload["id"] == "tools/list":
            return {"error": {"code": -32001, "message": "list failed"}}
        return {"id": payload["id"], "result": {"content": []}}

    monkeypatch.setattr(facade_smoke, "_smoke_request", fake_smoke_request)

    with pytest.raises(FacadeSmokeError, match="list failed"):
        facade_smoke._exercise_client_shim(
            socket_path=tmp_path / "broker.sock",
            profile="llm-profile",
            query="read-store",
            call_tool="read-store.fetch",
            call_args={},
            session_id="session-1",
            timeout_seconds=23,
        )

    assert calls == ["initialize", "tools/list"]

def test_facade_stop_smoke_session_swallows_client_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_broker.facade_smoke as facade_smoke

    calls: list[dict[str, object]] = []

    def successful_request(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {"result": {}}

    monkeypatch.setattr(facade_smoke, "_request_through_client", successful_request)
    facade_smoke._stop_smoke_session(Path("/tmp/broker.sock"), "llm", "session-a")
    assert calls[0]["socket_path"] == Path("/tmp/broker.sock")
    assert calls[0]["profile"] == "llm"
    assert calls[0]["session_id"] == "session-a"
    assert calls[0]["payload"] == {
        "id": "broker/session/stop",
        "method": "broker/session/stop",
        "params": {"broker_session_id": "session-a"},
    }

    def failed_request(**_kwargs: object) -> dict[str, object]:
        raise facade_smoke.FacadeSmokeError("ignored")

    monkeypatch.setattr(facade_smoke, "_request_through_client", failed_request)
    facade_smoke._stop_smoke_session(Path("/tmp/broker.sock"), "llm", "session-b")

def test_facade_cleanup_smoke_daemon_stops_session_when_reusing_daemon(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import mcp_broker.facade_smoke as facade_smoke

    stopped_sessions: list[tuple[Path, str, str]] = []

    class Daemon:
        def join(self, timeout: int) -> None:
            raise AssertionError("join should not run for reused daemon")

        def stop(self) -> None:
            raise AssertionError("stop should not run for reused daemon")

    monkeypatch.setattr(
        facade_smoke,
        "_stop_smoke_session",
        lambda socket_path, profile, session_id: stopped_sessions.append(
            (socket_path, profile, session_id)
        ),
    )

    facade_smoke._cleanup_smoke_daemon(
        tmp_path / "broker.sock",
        "llm-profile",
        "session-1",
        Daemon(),
        started_daemon=False,
    )

    assert stopped_sessions == [(tmp_path / "broker.sock", "llm-profile", "session-1")]

def test_facade_cleanup_smoke_daemon_requests_stop_and_always_stops_daemon(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import mcp_broker.facade_smoke as facade_smoke

    events: list[object] = []

    class Daemon:
        def join(self, timeout: int) -> None:
            events.append(("join", timeout))

        def stop(self) -> None:
            events.append("stop")

    def fake_request(**kwargs: object) -> dict[str, object]:
        events.append(kwargs)
        return {"result": {}}

    monkeypatch.setattr(facade_smoke, "_request_through_client", fake_request)

    facade_smoke._cleanup_smoke_daemon(
        tmp_path / "broker.sock",
        "llm-profile",
        "session-1",
        Daemon(),
        started_daemon=True,
    )

    request = events[0]
    assert request["socket_path"] == tmp_path / "broker.sock"
    assert request["profile"] == "llm-profile"
    assert request["session_id"] == "facade-smoke-stop"
    assert request["payload"] == {"jsonrpc": "2.0", "id": "stop", "method": "broker/stop"}
    assert events[1:] == [("join", 5), "stop"]

def test_facade_cleanup_smoke_daemon_stops_daemon_after_stop_request_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import mcp_broker.facade_smoke as facade_smoke

    events: list[str] = []

    class Daemon:
        def join(self, timeout: int) -> None:
            events.append("join")

        def stop(self) -> None:
            events.append("stop")

    def failed_request(**_kwargs: object) -> dict[str, object]:
        raise FacadeSmokeError("stop failed")

    monkeypatch.setattr(facade_smoke, "_request_through_client", failed_request)

    with pytest.raises(FacadeSmokeError, match="stop failed"):
        facade_smoke._cleanup_smoke_daemon(
            tmp_path / "broker.sock",
            "llm-profile",
            "session-1",
            Daemon(),
            started_daemon=True,
        )

    assert events == ["stop"]

def test_facade_module_entrypoint_runs_arg_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp_broker.facade_smoke as facade_smoke

    monkeypatch.setattr(sys, "argv", ["facade_smoke.py"])

    with pytest.raises(SystemExit) as exc_info:
        runpy.run_path(str(Path(facade_smoke.__file__)), run_name="__main__")

    assert exc_info.value.code == 2

def test_facade_run_smoke_stops_session_when_daemon_already_running(
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
    stopped: list[tuple[Path, str, str]] = []

    class AlreadyRunningDaemon:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            raise facade_smoke.BrokerDaemonError("broker daemon already running")

    monkeypatch.setattr(facade_smoke.BrokerConfig, "from_file", lambda _path: config)
    monkeypatch.setattr(facade_smoke, "BrokerDaemon", AlreadyRunningDaemon)
    monkeypatch.setattr(
        facade_smoke,
        "_exercise_client_shim",
        lambda **_kwargs: {
            "tools/list": {"result": {"tools": []}},
            "broker.search_tools": {"result": {"content": [{"type": "text", "text": '{"matches": []}'}]}},
            "broker.describe_tool": {
                "result": {"content": [{"type": "text", "text": '{"tool": {"name": "fake.echo"}}'}]}
            },
            "fake.echo": {"id": "fake.echo", "result": {"content": []}},
        },
    )
    monkeypatch.setattr(
        facade_smoke,
        "_stop_smoke_session",
        lambda socket_path, profile, session_id: stopped.append((socket_path, profile, session_id)),
    )

    report = facade_smoke._run_smoke(
        Namespace(
            config=str(tmp_path / "broker.yaml"),
            profile="llm",
            query="echo",
            call_tool="fake.echo",
            call_args="{}",
            request_timeout_seconds=70,
        )
    )

    assert report["started_daemon"] is False
    assert report["profile"] == "llm"
    assert stopped[0][0] == tmp_path / "broker.sock"
    assert stopped[0][1] == "llm"
