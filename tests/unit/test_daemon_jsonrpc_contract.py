from pathlib import Path
import json
import os
import socket
import tempfile
import threading

import pytest

from mcp_broker.config import BrokerConfig
from mcp_broker.schema import DEFAULT_CALL_TIMEOUT_SECONDS


pytestmark = pytest.mark.unit


def test_daemon_jsonrpc_reports_invalid_request_and_notifications(tmp_path: Path) -> None:
    from mcp_broker.daemon import BrokerDaemon

    daemon = BrokerDaemon(runtime_root=tmp_path / "runtime", socket_path=tmp_path / "broker.sock")

    invalid = daemon._handle_jsonrpc_request({"jsonrpc": "2.0"})
    daemon._handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": "init",
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25"},
        }
    )
    initialized = daemon._handle_jsonrpc_request(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}
    )

    assert invalid["error"]["message"] == "method is required"
    assert initialized is None


def test_daemon_control_stop_takes_precedence_over_jsonrpc_envelope(tmp_path: Path) -> None:
    from mcp_broker.daemon import BrokerDaemon

    daemon = BrokerDaemon(runtime_root=tmp_path / "runtime", socket_path=tmp_path / "broker.sock")

    response = daemon._handle_request(
        {"jsonrpc": "2.0", "id": "stop", "method": "broker/stop"}
    )

    assert response == {
        "id": "stop",
        "result": {
            "remaining_broker_processes": [],
            "stopped_upstreams": [],
            "stopping": True,
        },
    }
    assert daemon._stop_requested.is_set()


def test_daemon_connection_does_not_respond_to_jsonrpc_notifications(tmp_path: Path) -> None:
    from mcp_broker.daemon import BrokerDaemon

    daemon = BrokerDaemon(runtime_root=tmp_path / "runtime", socket_path=tmp_path / "broker.sock")
    connection = BufferConnection(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')

    daemon._handle_connection(connection)

    assert connection.sent == b""


def test_daemon_notification_request_log_does_not_restart_upstreams(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerSettings, RuntimeConfig, UpstreamConfig
    from mcp_broker.daemon import BrokerDaemon

    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={
            "research-notes": UpstreamConfig(
                name="research-notes",
                command="research-notes",
                mode="shared",
                tool_prefix="research-notes",
            )
        },
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    client = RecoverableExitedClient()
    daemon._stdio_upstreams["research-notes"] = client
    connection = BufferConnection(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')

    daemon._handle_connection(connection)

    assert connection.sent == b""
    assert client.ensure_running_calls == 0


def test_daemon_serve_loop_does_not_let_one_connection_block_next_request(
    tmp_path: Path,
) -> None:
    from mcp_broker.daemon import BrokerDaemon

    first_started = threading.Event()
    release_first = threading.Event()
    fast_handled = threading.Event()
    accepted = [_ContextConnection("slow"), _ContextConnection("fast")]

    class FakeServer:
        def accept(self) -> tuple["_ContextConnection", object]:
            if accepted:
                return accepted.pop(0), None
            raise OSError("closed")

        def close(self) -> None:
            return None

    class TestDaemon(BrokerDaemon):
        def _handle_connection(self, connection: "_ContextConnection") -> None:  # type: ignore[override]
            if connection.name == "slow":
                first_started.set()
                release_first.wait(timeout=2)
                return
            fast_handled.set()
            self._stop_requested.set()
            release_first.set()

    daemon = TestDaemon(runtime_root=tmp_path / "runtime", socket_path=tmp_path / "broker.sock")
    daemon._server = FakeServer()  # type: ignore[assignment]
    thread = threading.Thread(target=daemon._serve_loop)
    thread.start()
    try:
        assert first_started.wait(timeout=1)
        assert fast_handled.wait(timeout=1)
    finally:
        release_first.set()
        thread.join(timeout=1)


def test_daemon_join_waits_for_connection_threads_before_cleanup(tmp_path: Path) -> None:
    from mcp_broker.daemon import BrokerDaemon

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    cleanup_started = threading.Event()

    class TestDaemon(BrokerDaemon):
        def _handle_connection(self, _connection: "_ContextConnection") -> None:  # type: ignore[override]
            started.set()
            release.wait(timeout=1)
            finished.set()

        def _cleanup(self) -> None:  # type: ignore[override]
            cleanup_started.set()

    daemon = TestDaemon(runtime_root=tmp_path / "runtime", socket_path=tmp_path / "broker.sock")
    daemon._start_connection_thread(_ContextConnection("slow"))
    assert started.wait(timeout=1)

    join_thread = threading.Thread(target=lambda: daemon.join(timeout=1))
    join_thread.start()
    try:
        assert not cleanup_started.wait(timeout=0.05)
        release.set()
        join_thread.join(timeout=1)
    finally:
        release.set()
        join_thread.join(timeout=1)

    assert finished.is_set()
    assert cleanup_started.is_set()


def test_daemon_connection_thread_join_respects_timeout(tmp_path: Path) -> None:
    from mcp_broker.daemon import BrokerDaemon

    class AlwaysAliveThread:
        def is_alive(self) -> bool:
            return True

        def join(self, timeout: float | None = None) -> None:
            raise AssertionError(f"join should not run after timeout: {timeout}")

    daemon = BrokerDaemon(runtime_root=tmp_path / "runtime", socket_path=tmp_path / "broker.sock")
    daemon._connection_threads = [AlwaysAliveThread()]  # type: ignore[list-item]

    daemon._join_connection_threads(timeout=0)

    assert len(daemon._connection_threads) == 1


def test_daemon_client_request_reads_chunked_socket_response(tmp_path: Path) -> None:
    from mcp_broker.daemon import _client_request

    del tmp_path
    temp_dir = tempfile.TemporaryDirectory(prefix="mb-", dir="/tmp")
    socket_path = Path(temp_dir.name) / "broker.sock"
    payload = json.dumps({"id": "broker/health", "result": {"status": "ok", "items": list(range(4000))}})
    received: list[bytes] = []
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    server.listen(1)

    def serve_once() -> None:
        try:
            connection, _ = server.accept()
            with connection:
                received.append(connection.recv(65536))
                midpoint = len(payload) // 2
                connection.sendall(payload[:midpoint].encode("utf-8"))
                connection.sendall(payload[midpoint:].encode("utf-8"))
                connection.sendall(b"\n")
        finally:
            server.close()

    thread = threading.Thread(target=serve_once)
    thread.start()
    try:
        response = _client_request(socket_path, "broker/health")
    finally:
        thread.join(timeout=2)
        server.close()
        temp_dir.cleanup()

    assert response["result"]["status"] == "ok"
    assert received == [b'{"id": "broker/health", "method": "broker/health"}\n']


def test_daemon_connection_sends_response_before_request_log_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker.daemon import BrokerDaemon

    daemon = BrokerDaemon(runtime_root=tmp_path / "runtime", socket_path=tmp_path / "broker.sock")
    connection = BufferConnection(
        b'{"jsonrpc":"2.0","id":"init","method":"initialize",'
        b'"params":{"protocolVersion":"2025-11-25"}}\n'
    )

    def broken_request_log(*_args: object) -> None:
        raise RuntimeError("snapshot blocked")

    monkeypatch.setattr(daemon, "_write_request_log", broken_request_log)

    daemon._handle_connection(connection)

    assert json.loads(connection.sent.decode("utf-8"))["id"] == "init"


def test_daemon_join_and_cleanup_are_idempotent_without_started_thread(tmp_path: Path) -> None:
    from mcp_broker.daemon import BrokerDaemon

    daemon = BrokerDaemon(runtime_root=tmp_path / "runtime", socket_path=tmp_path / "broker.sock")
    daemon._stop_logged = True

    daemon.join(timeout=0)
    daemon.join(timeout=0)

    assert json.loads((tmp_path / "runtime" / "state" / "broker-status.json").read_text(encoding="utf-8"))[
        "status"
    ] == "stopped"


def test_daemon_health_reports_profile_and_configured_upstreams(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerSettings, RuntimeConfig, UpstreamConfig
    from mcp_broker.daemon import BrokerDaemon

    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={
            "read-store": UpstreamConfig(
                name="read-store",
                command="read-store",
                mode="shared",
                enabled=True,
                tool_prefix="read-store",
            ),
            "disabled": UpstreamConfig(
                name="disabled",
                command="disabled",
                mode="disabled",
                enabled=True,
                tool_prefix="disabled",
            ),
        },
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )

    response = daemon._handle_request(
        {"method": "broker/health", "id": "health", "params": {"profile": "llm-profile"}}
    )

    assert response["id"] == "health"
    assert response["result"] == {
        "pid": os.getpid(),
        "socket_path": str(config.runtime.socket_path),
        "status": "ok",
        "profile": "llm-profile",
        "upstreams": {
            "disabled": {
                "state": "disabled",
                "pid": None,
                "cpu_percent": None,
                "memory_mb": None,
                "restarts": 0,
                "last_error": None,
                "auth_probe": "none",
            },
            "read-store": {
                "state": "configured",
                "pid": None,
                "cpu_percent": None,
                "memory_mb": None,
                "restarts": 0,
                "last_error": None,
                "auth_probe": "none",
            },
        },
    }


def test_daemon_health_degrades_when_configured_upstream_exited(tmp_path: Path) -> None:
    from mcp_broker.daemon import BrokerDaemon

    daemon = BrokerDaemon(runtime_root=tmp_path / "runtime", socket_path=tmp_path / "broker.sock")
    result = daemon._health_result({"method": "broker/health"})
    result["upstreams"] = {
        "read-store": {"state": "exited", "last_error": None},
        "disabled": {"state": "disabled", "last_error": None},
    }

    assert daemon._health_status(result["upstreams"]) == "degraded"


def test_daemon_session_and_auth_health_ignore_unconfigured_metadata(
    tmp_path: Path,
) -> None:
    from mcp_broker.daemon import BrokerDaemon

    daemon = BrokerDaemon(runtime_root=tmp_path / "runtime", socket_path=tmp_path / "broker.sock")

    assert daemon._session_id_from_params({"_meta": {"mcp_broker": "not-a-mapping"}}) is None
    assert daemon._upstream_health_with_auth("unconfigured", {"state": "configured"}) == {
        "state": "configured"
    }


def test_daemon_cwd_project_injection_returns_arguments_without_config(
    tmp_path: Path,
) -> None:
    from mcp_broker.daemon import BrokerDaemon

    daemon = BrokerDaemon(runtime_root=tmp_path / "runtime", socket_path=tmp_path / "broker.sock")
    arguments = {"query": "BrokerCore"}

    assert (
        daemon._inject_cwd_project_arg(
            "memory-index.search_graph",
            arguments,
            {"client_cwd": str(tmp_path)},
        )
        is arguments
    )


def test_daemon_cwd_project_injection_adds_project_for_configured_upstream(
    tmp_path: Path,
) -> None:
    from mcp_broker.config import BrokerSettings, RuntimeConfig, UpstreamConfig
    from mcp_broker.daemon import BrokerDaemon

    repo = tmp_path / "Projects" / "apps" / "demo"
    (repo / ".git").mkdir(parents=True)
    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={
            "memory-index": UpstreamConfig(
                name="memory-index",
                command="memory-index",
                tool_prefix="memory-index",
                inject_cwd_project=True,
            )
        },
    )
    daemon = BrokerDaemon(
        runtime_root=tmp_path / "runtime",
        socket_path=tmp_path / "broker.sock",
        broker_config=config,
    )

    result = daemon._inject_cwd_project_arg(
        "memory-index.search_graph",
        {"query": "BrokerCore"},
        {"client_cwd": str(repo / "src")},
    )

    assert result == {
        "query": "BrokerCore",
        "project": str(repo).lstrip("/").replace("/", "-"),
    }


def test_daemon_health_reports_passive_missing_auth_without_secret_paths(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerSettings, RuntimeConfig, UpstreamConfig
    from mcp_broker.daemon import BrokerDaemon

    missing_secret = tmp_path / "runtime" / "secrets" / "API_TOKEN"
    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={
            "api": UpstreamConfig(
                name="api",
                command="api",
                env={"API_TOKEN": "MCP_BROKER_TEST_MISSING_AUTH_TOKEN"},
                env_files={"FILE_TOKEN": missing_secret},
            ),
        },
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )

    response = daemon._handle_request({"method": "broker/health", "id": "health"})
    health = response["result"]["upstreams"]["api"]

    assert health["auth_probe"] == "credentials_missing"
    assert health["auth_state"] == "unauthenticated"
    assert health["last_error"] == (
        "missing auth source for upstream api: "
        "env:MCP_BROKER_TEST_MISSING_AUTH_TOKEN, secret_file:FILE_TOKEN"
    )
    assert str(missing_secret) not in repr(health)


def test_daemon_health_uses_existing_stdio_client_snapshot(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerSettings, RuntimeConfig, UpstreamConfig
    from mcp_broker.daemon import BrokerDaemon
    from mcp_broker.upstream_stdio import StdioUpstreamProcess

    upstream = UpstreamConfig(name="read-store", command="read-store", tool_prefix="read-store")
    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={"read-store": upstream},
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    daemon._stdio_upstreams["read-store"] = StdioUpstreamProcess(
        upstream,
        runtime_state_dir=config.runtime.state_dir,
    )

    response = daemon._handle_request({"method": "broker/health", "id": "health"})

    assert response["result"]["upstreams"]["read-store"] == {
        "state": "configured",
        "pid": None,
        "cpu_percent": None,
        "memory_mb": None,
        "restarts": 0,
        "last_error": None,
        "auth_probe": "none",
    }


def test_daemon_connection_reports_jsonrpc_parse_error(tmp_path: Path) -> None:
    from mcp_broker.daemon import BrokerDaemon

    daemon = BrokerDaemon(runtime_root=tmp_path / "runtime", socket_path=tmp_path / "broker.sock")
    connection = BufferConnection(b'{"jsonrpc":"2.0",')

    daemon._handle_connection(connection)

    assert json.loads(connection.sent.decode("utf-8")) == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32700, "message": "Parse error"},
    }


def test_daemon_cleanup_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp_broker.daemon import BrokerDaemon

    daemon = BrokerDaemon(runtime_root=tmp_path / "runtime", socket_path=tmp_path / "broker.sock")
    shutdown_calls: list[str] = []
    snapshots: list[str] = []

    def shutdown_upstreams() -> dict[str, object]:
        shutdown_calls.append("called")
        return {"stopped_upstreams": [], "remaining_broker_processes": []}

    monkeypatch.setattr(daemon, "_shutdown_upstreams", shutdown_upstreams)
    monkeypatch.setattr(daemon, "_write_status_snapshot", snapshots.append)

    daemon._cleanup()
    daemon._cleanup()

    assert shutdown_calls == ["called"]
    assert snapshots == ["stopped"]


def test_daemon_cli_status_uses_health_method() -> None:
    from mcp_broker.daemon import _broker_method_for_command
    from mcp_broker.daemon_cli import _broker_method_for_command as cli_method_for_command

    assert _broker_method_for_command("status") == "broker/health"
    assert _broker_method_for_command("stop") == "broker/stop"
    assert cli_method_for_command("status") == "broker/health"
    assert cli_method_for_command("stop") == "broker/stop"


def test_daemon_cli_loads_config_for_serve(tmp_path: Path) -> None:
    import yaml

    from mcp_broker.daemon import _broker_config_for_serve
    from mcp_broker.daemon_cli import _broker_config_for_serve as cli_broker_config_for_serve

    config_path = tmp_path / "broker.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "runtime": {"root": str(tmp_path / "runtime")},
                "upstreams": {"read-store": {"command": "read-store", "tool_prefix": "read-store"}},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    config = _broker_config_for_serve(config_path)
    cli_config = cli_broker_config_for_serve(config_path)

    assert config is not None
    assert cli_config is not None
    assert config.runtime.root == tmp_path / "runtime"
    assert cli_config.runtime.root == tmp_path / "runtime"
    assert sorted(config.upstreams) == ["read-store"]
    assert sorted(cli_config.upstreams) == ["read-store"]


def test_daemon_cli_keeps_legacy_serve_without_config() -> None:
    from mcp_broker.daemon import _broker_config_for_serve
    from mcp_broker.daemon_cli import _broker_config_for_serve as cli_broker_config_for_serve

    assert _broker_config_for_serve(None) is None
    assert cli_broker_config_for_serve(None) is None


def test_daemon_cli_rejects_unknown_command_and_missing_required_paths(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.daemon_cli import main

    with pytest.raises(SystemExit) as unknown:
        main(["restart", "--runtime-root", "/tmp/runtime", "--socket-path", "/tmp/broker.sock"])
    with pytest.raises(SystemExit) as missing_runtime:
        main(["status", "--socket-path", "/tmp/broker.sock"])
    with pytest.raises(SystemExit) as missing_socket:
        main(["status", "--runtime-root", "/tmp/runtime"])

    assert unknown.value.code == 2
    assert missing_runtime.value.code == 2
    assert missing_socket.value.code == 2
    error_text = capsys.readouterr().err
    assert "invalid choice: 'restart'" in error_text
    assert "--runtime-root" in error_text
    assert "--socket-path" in error_text


def test_daemon_cli_help_includes_description(capsys: pytest.CaptureFixture[str]) -> None:
    from mcp_broker.daemon_cli import main

    with pytest.raises(SystemExit) as exc:
        main(["--help"])

    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "\nRun and inspect mcp-broker daemon\n" in output
    assert "XXRun" not in output
    assert "serve" in output
    assert "status" in output
    assert "stop" in output


def test_daemon_cli_status_and_stop_do_not_enter_serve_branch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.daemon_cli import main

    class ExplodingDaemon:
        def __init__(self, **_kwargs: object) -> None:
            raise AssertionError("status and stop must not construct daemon")

    requests: list[tuple[Path, str]] = []

    def request_fn(socket_path: Path, method: str) -> dict[str, object]:
        requests.append((socket_path, method))
        return {"id": method, "result": {"ok": True}}

    runtime_root = tmp_path / "runtime"
    socket_path = tmp_path / "broker.sock"

    assert (
        main(
            ["status", "--runtime-root", str(runtime_root), "--socket-path", str(socket_path)],
            daemon_cls=ExplodingDaemon,  # type: ignore[arg-type]
            request_fn=request_fn,
        )
        == 0
    )
    assert (
        main(
            ["stop", "--runtime-root", str(runtime_root), "--socket-path", str(socket_path)],
            daemon_cls=ExplodingDaemon,  # type: ignore[arg-type]
            request_fn=request_fn,
        )
        == 0
    )

    assert requests == [(socket_path, "broker/health"), (socket_path, "broker/stop")]
    assert [json.loads(line)["result"] for line in capsys.readouterr().out.splitlines()] == [
        {"ok": True},
        {"ok": True},
    ]


def test_daemon_cli_writes_sorted_json_response(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.daemon_cli import main

    class ExplodingDaemon:
        def __init__(self, **_kwargs: object) -> None:
            raise AssertionError("status must not construct daemon")

    def request_fn(_socket_path: Path, method: str) -> dict[str, object]:
        return {"z": 1, "id": method, "a": 2}

    assert (
        main(
            [
                "status",
                "--runtime-root",
                str(tmp_path / "runtime"),
                "--socket-path",
                str(tmp_path / "broker.sock"),
            ],
            daemon_cls=ExplodingDaemon,  # type: ignore[arg-type]
            request_fn=request_fn,
        )
        == 0
    )

    assert capsys.readouterr().out == '{"a": 2, "id": "broker/health", "z": 1}\n'


def test_daemon_cli_client_request_uses_unix_stream_socket_and_exact_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_broker.daemon_cli as daemon_cli

    events: list[tuple[str, object]] = []

    class FakeSocket:
        def __init__(self, family: object, kind: object) -> None:
            events.append(("init", (family, kind)))
            self._chunks = [b'{"result": ', b'{"ok": true}}', b""]

        def __enter__(self) -> "FakeSocket":
            events.append(("enter", None))
            return self

        def __exit__(self, *_args: object) -> None:
            events.append(("exit", None))

        def connect(self, address: str) -> None:
            events.append(("connect", address))

        def sendall(self, payload: bytes) -> None:
            events.append(("sendall", payload))

        def recv(self, size: int) -> bytes:
            events.append(("recv", size))
            return self._chunks.pop(0)

    monkeypatch.setattr(daemon_cli.socket, "socket", FakeSocket)

    response = daemon_cli._client_request(tmp_path / "broker.sock", "broker/health")

    assert response == {"result": {"ok": True}}
    assert events == [
        ("init", (daemon_cli.socket.AF_UNIX, daemon_cli.socket.SOCK_STREAM)),
        ("enter", None),
        ("connect", str(tmp_path / "broker.sock")),
        ("sendall", b'{"id": "broker/health", "method": "broker/health"}\n'),
        ("recv", 65536),
        ("recv", 65536),
        ("recv", 65536),
        ("exit", None),
    ]


def test_daemon_serve_forever_starts_waits_and_joins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker.daemon import BrokerDaemon

    daemon = BrokerDaemon(runtime_root=tmp_path / "runtime", socket_path=tmp_path / "broker.sock")
    calls: list[str] = []

    def start() -> None:
        calls.append("start")
        daemon._stop_requested.set()

    def join(*, timeout: float) -> None:
        calls.append(f"join:{timeout}")

    monkeypatch.setattr(daemon, "start", start)
    monkeypatch.setattr(daemon, "join", join)

    daemon.serve_forever()

    assert calls == ["start", "join:5"]


def test_daemon_main_queries_status_and_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import mcp_broker.daemon as daemon_module

    class ExplodingDaemon:
        def __init__(self, **_kwargs: object) -> None:
            raise AssertionError("status and stop must not construct daemon")

    requests: list[tuple[Path, str]] = []

    def client_request(socket_path: Path, method: str) -> dict[str, object]:
        requests.append((socket_path, method))
        return {"id": method, "result": {"status": "ok"}}

    monkeypatch.setattr(daemon_module, "_client_request", client_request)
    monkeypatch.setattr(daemon_module, "BrokerDaemon", ExplodingDaemon)

    assert (
        daemon_module.main(
            [
                "status",
                "--runtime-root",
                str(tmp_path / "runtime"),
                "--socket-path",
                str(tmp_path / "broker.sock"),
            ]
        )
        == 0
    )
    assert (
        daemon_module.main(
            [
                "stop",
                "--runtime-root",
                str(tmp_path / "runtime"),
                "--socket-path",
                str(tmp_path / "broker.sock"),
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert json.loads(lines[0])["id"] == "broker/health"
    assert json.loads(lines[1])["id"] == "broker/stop"
    assert requests == [
        (tmp_path / "broker.sock", "broker/health"),
        (tmp_path / "broker.sock", "broker/stop"),
    ]


def test_daemon_main_serve_uses_loaded_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import yaml
    import mcp_broker.daemon as daemon_module

    config_path = tmp_path / "broker.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "runtime": {"root": str(tmp_path / "runtime")},
                "upstreams": {"fake": {"command": "fake", "tool_prefix": "fake"}},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    seen: dict[str, object] = {}

    class FakeDaemon:
        def __init__(self, **kwargs: object) -> None:
            seen.update(kwargs)

        def serve_forever(self) -> None:
            seen["served"] = True

    monkeypatch.setattr(daemon_module, "BrokerDaemon", FakeDaemon)

    assert (
        daemon_module.main(
            [
                "serve",
                "--runtime-root",
                str(tmp_path / "runtime"),
                "--socket-path",
                str(tmp_path / "broker.sock"),
                "--config",
                str(config_path),
            ]
        )
        == 0
    )

    assert seen["runtime_root"] == tmp_path / "runtime"
    assert seen["socket_path"] == tmp_path / "broker.sock"
    assert isinstance(seen["broker_config"], BrokerConfig)
    assert seen["served"] is True


def test_daemon_main_passes_daemon_dependencies_to_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_broker.daemon as daemon_module
    import mcp_broker.daemon_cli as daemon_cli

    seen: dict[str, object] = {}

    def fake_cli_main(argv: object, **kwargs: object) -> int:
        seen["argv"] = argv
        seen.update(kwargs)
        return 17

    monkeypatch.setattr(daemon_cli, "main", fake_cli_main)

    result = daemon_module.main(
        [
            "status",
            "--runtime-root",
            "/tmp/runtime",
            "--socket-path",
            "/tmp/broker.sock",
        ]
    )

    assert result == 17
    assert seen["argv"] == [
        "status",
        "--runtime-root",
        "/tmp/runtime",
        "--socket-path",
        "/tmp/broker.sock",
    ]
    assert seen["daemon_cls"] is daemon_module.BrokerDaemon
    assert seen["request_fn"] is daemon_module._client_request


@pytest.mark.parametrize(
    ("params", "message"),
    [
        (None, "broker config is not loaded"),
        ([], "tools/call params must be an object"),
        ({"name": 1, "arguments": {}}, "tools/call name and arguments required"),
        ({"name": "fake.echo", "arguments": []}, "tools/call name and arguments required"),
    ],
)
def test_daemon_tools_call_validation_errors(
    tmp_path: Path,
    params: object,
    message: str,
) -> None:
    from mcp_broker.daemon import BrokerDaemon
    from mcp_broker.jsonrpc import JsonRpcRequest

    config = _empty_config(tmp_path)
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=None if params is None else config,
    )
    request = JsonRpcRequest(
        method="tools/call",
        id="call",
        params=params,
        has_id=True,
    )

    response = daemon._handle_tools_call(request)

    assert response.error == {"code": -32000 if params is None else -32602, "message": message}


def test_daemon_tools_call_maps_broker_errors(tmp_path: Path) -> None:
    from mcp_broker.daemon import BrokerDaemon
    from mcp_broker.jsonrpc import JsonRpcRequest

    config = _empty_config(tmp_path)
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    request = JsonRpcRequest(
        method="tools/call",
        id="call",
        params={"name": "missing.echo", "arguments": {}},
        has_id=True,
    )

    response = daemon._handle_tools_call(request)

    assert response.error == {"code": -32000, "message": "unknown tool prefix: missing"}


def test_daemon_broker_search_tools_searches_allowed_catalog(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig
    from mcp_broker.catalog import BrokerCatalogFacade
    from mcp_broker.daemon import BrokerDaemon
    from mcp_broker.profiles import ToolExposureProfile

    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        profiles={
            "llm-profile": ToolExposureProfile(name="llm-profile", max_tools=80, compact_tools_enabled=True),
            "protected": ToolExposureProfile(name="protected", max_tools=80),
        },
        upstreams={
            "remote-repo": UpstreamConfig(
                name="remote-repo",
                command="remote-repo",
                tool_prefix="remote-repo",
                profiles=("llm-profile",),
                purpose="GitHub repositories, issues, pull requests, and code search",
                tags=("repo", "issue", "pull-request"),
            ),
            "notes-writer": UpstreamConfig(
                name="notes-writer",
                command="notes-writer",
                tool_prefix="notes-writer",
                profiles=("protected",),
                purpose="Obsidian vault notes",
            ),
        },
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    daemon._stdio_upstreams["remote-repo"] = CatalogClient(
        tools=[
            {
                "name": "search_issues",
                "description": "Search GitHub issues",
                "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
            }
        ],
        response={"content": [{"type": "text", "text": "unused"}]},
    )
    daemon._stdio_upstreams["notes-writer"] = CatalogClient(
        tools=[{"name": "search_notes", "description": "Search notes"}],
        response={"content": []},
    )

    response = daemon._handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": "search",
            "method": "tools/call",
            "params": {
                "name": "broker.search_tools",
                "arguments": {"query": "issue", "limit": 5},
                "profile": "llm-profile",
            },
        }
    )

    assert response["result"]["structuredContent"] == {
        "matches": [
            {
                "name": "remote-repo.search_issues",
                "upstream": "remote-repo",
                "description": "Search GitHub issues",
                "purpose": "GitHub repositories, issues, pull requests, and code search",
                "tags": ["repo", "issue", "pull-request"],
                "mutating": False,
            }
        ]
    }
    assert "remote-repo.search_issues" in response["result"]["content"][0]["text"]
    assert daemon._stdio_upstreams["remote-repo"].list_calls == [60]
    assert daemon._stdio_upstreams["notes-writer"].list_calls == []


def test_daemon_broker_describe_tool_returns_schema(tmp_path: Path) -> None:
    import mcp_broker.daemon as daemon_module
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig
    from mcp_broker.profiles import ToolExposureProfile

    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        profiles={"llm-profile": ToolExposureProfile(name="llm-profile", max_tools=80, compact_tools_enabled=True)},
        upstreams={
            "read-store": UpstreamConfig(
                name="read-store",
                command="read-store",
                tool_prefix="read-store",
                profiles=("llm-profile",),
                purpose="Persistent project read-store",
                tags=("read-store",),
            )
        },
    )
    daemon = daemon_module.BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    daemon._stdio_upstreams["read-store"] = CatalogClient(
        tools=[
            {
                "name": "search",
                "description": "Search project read-store",
                "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
            }
        ],
        response={"content": []},
    )

    response = daemon._handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": "describe",
            "method": "tools/call",
            "params": {
                "name": "broker.describe_tool",
                "arguments": {"name": "read-store.search"},
                "profile": "llm-profile",
            },
        }
    )

    assert response["result"]["structuredContent"] == {
        "tool": {
            "name": "read-store.search",
            "upstream": "read-store",
            "description": "Search project read-store",
            "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
            "purpose": "Persistent project read-store",
            "tags": ["read-store"],
            "mutating": False,
        }
    }


def test_daemon_broker_call_tool_routes_named_tool(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig
    from mcp_broker.daemon import BrokerDaemon
    from mcp_broker.profiles import ToolExposureProfile

    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        profiles={"llm-profile": ToolExposureProfile(name="llm-profile", max_tools=80, compact_tools_enabled=True)},
        upstreams={
            "read-store": UpstreamConfig(
                name="read-store",
                command="read-store",
                tool_prefix="read-store",
                profiles=("llm-profile",),
            )
        },
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    daemon._stdio_upstreams["read-store"] = CatalogClient(
        tools=[{"name": "search", "description": "Search project read-store"}],
        response={"content": [{"type": "text", "text": "found refund note"}]},
    )

    response = daemon._handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": "call",
            "method": "tools/call",
            "params": {
                "name": "broker.call_tool",
                "arguments": {"name": "read-store.search", "arguments": {"query": "refund"}},
                "profile": "llm-profile",
            },
        }
    )

    assert response["result"] == {"content": [{"type": "text", "text": "found refund note"}]}
    assert daemon._stdio_upstreams["read-store"].call_calls == [
        ("search", {"query": "refund"}, 60)
    ]


def test_daemon_runs_configured_auth_repair_and_retries_stdio_call(tmp_path: Path) -> None:
    from mcp_broker.config import (
        AuthRepairPolicy,
        BrokerConfig,
        BrokerSettings,
        RuntimeConfig,
        UpstreamConfig,
    )
    from mcp_broker.daemon import BrokerDaemon

    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={
            "notebook-service": UpstreamConfig(
                name="notebook-service",
                command="notebook-service",
                tool_prefix="notebook-service",
                auth_repair=AuthRepairPolicy(
                    tool="setup_auth",
                    arguments={"show_browser": True, "headless": False},
                    trigger_errors=("Not authenticated", "setup_auth"),
                    retry_original=True,
                    timeout_seconds=300,
                ),
            )
        },
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    daemon._stdio_upstreams["notebook-service"] = SequenceClient(
        [
            {
                "isError": True,
                "content": [
                    {
                        "type": "text",
                        "text": '{"success":false,"error":"Not authenticated. Run setup_auth first."}',
                    }
                ],
            },
            {"content": [{"type": "text", "text": "auth saved"}]},
            {"content": [{"type": "text", "text": "notebooks"}]},
        ]
    )

    result = daemon._call_stdio_upstream("notebook-service", "list_notebooks", {}, 60)

    assert result == {"content": [{"type": "text", "text": "notebooks"}]}
    assert daemon._stdio_upstreams["notebook-service"].call_calls == [
        ("list_notebooks", {}, 60),
        ("setup_auth", {"show_browser": True, "headless": False}, 300),
        ("list_notebooks", {}, 60),
    ]
    assert daemon._upstream_health()["notebook-service"] == {
        "state": "running",
        "pid": None,
        "cpu_percent": None,
        "memory_mb": None,
        "restarts": 0,
        "last_error": None,
        "auth_probe": "auth_repair_configured",
        "auth_state": "authenticated",
        "auth_repair_attempts": 1,
        "auth_repair_successes": 1,
        "auth_repair_failures": 0,
    }


def test_daemon_records_failed_auth_repair_in_health(tmp_path: Path) -> None:
    from mcp_broker.config import (
        AuthRepairPolicy,
        BrokerConfig,
        BrokerSettings,
        RuntimeConfig,
        UpstreamConfig,
    )
    from mcp_broker.daemon import BrokerDaemon
    from mcp_broker.broker import BrokerToolError
    from mcp_broker.upstream_stdio import StdioUpstreamError

    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={
            "notebook-service": UpstreamConfig(
                name="notebook-service",
                command="notebook-service",
                tool_prefix="notebook-service",
                auth_repair=AuthRepairPolicy(
                    tool="setup_auth",
                    trigger_errors=("Not authenticated",),
                    retry_original=True,
                ),
            )
        },
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    daemon._stdio_upstreams["notebook-service"] = SequenceClient(
        [
            {
                "isError": True,
                "content": [{"type": "text", "text": "Error: Not authenticated"}],
            },
            StdioUpstreamError("browser auth failed"),
        ]
    )

    with pytest.raises(BrokerToolError, match="browser auth failed"):
        daemon._call_stdio_upstream("notebook-service", "list_notebooks", {}, 60)

    assert daemon._upstream_health()["notebook-service"] == {
        "state": "running",
        "pid": None,
        "cpu_percent": None,
        "memory_mb": None,
        "restarts": 0,
        "last_error": "browser auth failed",
        "auth_probe": "auth_repair_configured",
        "auth_state": "unauthenticated",
        "auth_repair_attempts": 1,
        "auth_repair_successes": 0,
        "auth_repair_failures": 1,
    }


def test_daemon_records_retry_auth_error_as_failed_auth_repair(tmp_path: Path) -> None:
    from mcp_broker.config import (
        AuthRepairPolicy,
        BrokerConfig,
        BrokerSettings,
        RuntimeConfig,
        UpstreamConfig,
    )
    from mcp_broker.daemon import BrokerDaemon

    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={
            "notebook-service": UpstreamConfig(
                name="notebook-service",
                command="notebook-service",
                tool_prefix="notebook-service",
                auth_repair=AuthRepairPolicy(
                    tool="setup_auth",
                    trigger_errors=("Not authenticated",),
                    retry_original=True,
                ),
            )
        },
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    auth_error = {
        "isError": True,
        "content": [{"type": "text", "text": "Error: Not authenticated"}],
    }
    daemon._stdio_upstreams["notebook-service"] = SequenceClient(
        [
            auth_error,
            {"content": [{"type": "text", "text": "auth setup ran"}]},
            auth_error,
        ]
    )

    result = daemon._call_stdio_upstream("notebook-service", "list_notebooks", {}, 60)

    assert result == auth_error
    assert daemon._upstream_health()["notebook-service"] == {
        "state": "running",
        "pid": None,
        "cpu_percent": None,
        "memory_mb": None,
        "restarts": 0,
        "last_error": None,
        "auth_probe": "auth_repair_configured",
        "auth_state": "unauthenticated",
        "auth_repair_attempts": 1,
        "auth_repair_successes": 0,
        "auth_repair_failures": 1,
    }


def test_daemon_health_restarts_exited_shared_stdio_upstream(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig
    from mcp_broker.daemon import BrokerDaemon

    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={
            "read-store": UpstreamConfig(
                name="read-store",
                command="read-store",
                mode="shared",
                tool_prefix="read-store",
            )
        },
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    client = RecoverableExitedClient()
    daemon._stdio_upstreams["read-store"] = client

    assert daemon._upstream_health()["read-store"] == {
        "state": "running",
        "pid": 12345,
        "cpu_percent": None,
        "memory_mb": None,
        "restarts": 1,
        "last_error": None,
        "auth_probe": "none",
    }
    assert client.ensure_running_calls == 1


def test_daemon_health_reports_shared_stdio_restart_failure(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig
    from mcp_broker.daemon import BrokerDaemon
    from mcp_broker.upstream_stdio import StdioUpstreamError

    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={
            "read-store": UpstreamConfig(
                name="read-store",
                command="read-store",
                mode="shared",
                tool_prefix="read-store",
            )
        },
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    client = RecoverableExitedClient(restart_error=StdioUpstreamError("start failed"))
    daemon._stdio_upstreams["read-store"] = client

    assert daemon._upstream_health()["read-store"] == {
        "state": "backoff",
        "pid": None,
        "cpu_percent": None,
        "memory_mb": None,
        "restarts": 0,
        "last_error": "start failed",
        "auth_probe": "none",
    }
    assert client.ensure_running_calls == 1


def test_broker_status_only_restarts_shared_stdio_upstreams_visible_to_profile(
    tmp_path: Path,
) -> None:
    from mcp_broker.catalog import BrokerCatalogFacade
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig
    from mcp_broker.daemon import BrokerDaemon
    from mcp_broker.profiles import ToolExposureProfile

    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        profiles={
            "allowed-profile": ToolExposureProfile(
                name="allowed-profile",
                max_tools=80,
                compact_tools_enabled=True,
            ),
            "other-profile": ToolExposureProfile(
                name="other-profile",
                max_tools=80,
                compact_tools_enabled=True,
            ),
        },
        upstreams={
            "read-store": UpstreamConfig(
                name="read-store",
                command="read-store",
                mode="shared",
                profiles=("allowed-profile",),
                tool_prefix="read-store",
            ),
            "hidden-store": UpstreamConfig(
                name="hidden-store",
                command="hidden-store",
                mode="shared",
                profiles=("other-profile",),
                tool_prefix="hidden-store",
            ),
        },
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    visible_client = RecoverableExitedClient()
    hidden_client = RecoverableExitedClient()
    daemon._stdio_upstreams["read-store"] = visible_client
    daemon._stdio_upstreams["hidden-store"] = hidden_client

    result = BrokerCatalogFacade(
        broker_config=config,
        profile=config.profiles["allowed-profile"],
        list_upstream=lambda name, timeout: [],
        call_upstream=lambda name, tool, args, timeout: {"content": []},
        call_locks={},
        status_provider=daemon._upstream_health_for_status,
    ).call_tool("broker.status", {})

    payload = json.loads(result["content"][0]["text"])
    assert payload["upstreams"]["read-store"]["state"] == "running"
    assert "hidden-store" not in payload["upstreams"]
    assert visible_client.ensure_running_calls == 1
    assert hidden_client.ensure_running_calls == 0


def test_daemon_routes_per_session_stdio_clients_by_broker_session_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_broker.daemon as daemon_module
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig

    created: list[CreatedClient] = []

    def create_client(*_args: object, **_kwargs: object) -> "CreatedClient":
        client = CreatedClient(len(created) + 1)
        created.append(client)
        return client

    monkeypatch.setattr(daemon_module, "StdioUpstreamProcess", create_client)
    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={
            "browser-session": UpstreamConfig(
                name="browser-session",
                command="browser-session",
                mode="per_session",
                tool_prefix="browser-session",
            )
        },
    )
    daemon = daemon_module.BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )

    first = daemon._handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": "call-a1",
            "method": "tools/call",
            "params": {
                "name": "browser-session.browser_console_messages",
                "arguments": {},
                "broker_session_id": "llm-session-a",
            },
        }
    )
    same_session = daemon._handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": "call-a2",
            "method": "tools/call",
            "params": {
                "name": "browser-session.browser_console_messages",
                "arguments": {},
                "broker_session_id": "llm-session-a",
            },
        }
    )
    second = daemon._handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": "call-b",
            "method": "tools/call",
            "params": {
                "name": "browser-session.browser_console_messages",
                "arguments": {},
                "broker_session_id": "llm-session-b",
            },
        }
    )

    assert first["result"]["content"][0]["text"] == "client-1"
    assert same_session["result"]["content"][0]["text"] == "client-1"
    assert second["result"]["content"][0]["text"] == "client-2"
    assert sorted(daemon._stdio_upstreams) == [
        ("browser-session", "llm-session-a"),
        ("browser-session", "llm-session-b"),
    ]
    assert len(created) == 2


def test_daemon_passes_client_cwd_to_per_session_stdio_upstream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_broker.daemon as daemon_module
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig

    created_kwargs: list[dict[str, object]] = []

    def create_client(*_args: object, **kwargs: object) -> "CreatedClient":
        created_kwargs.append(kwargs)
        return CreatedClient(len(created_kwargs))

    monkeypatch.setattr(daemon_module, "StdioUpstreamProcess", create_client)
    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={
            "session-tool": UpstreamConfig(
                name="session-tool",
                command="session-tool",
                mode="per_session",
                tool_prefix="session-tool",
                session_env={"PROJECT_DIR": "client_cwd"},
            )
        },
    )
    daemon = daemon_module.BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )

    response = daemon._handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": "call",
            "method": "tools/call",
            "params": {
                "name": "session-tool.echo",
                "arguments": {},
                "broker_session_id": "llm-session-a",
                "broker_client_cwd": str(tmp_path / "client-project"),
            },
        }
    )

    assert response["result"]["content"][0]["text"] == "client-1"
    assert created_kwargs[0]["session_context"] == {
        "client_cwd": str(tmp_path / "client-project")
    }


def test_daemon_rejects_missing_session_context_before_caching_stdio_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_broker.daemon as daemon_module
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig

    created: list[object] = []

    def create_client(*_args: object, **_kwargs: object) -> "CreatedClient":
        created.append(object())
        return CreatedClient(len(created))

    monkeypatch.setattr(daemon_module, "StdioUpstreamProcess", create_client)
    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={
            "session-tool": UpstreamConfig(
                name="session-tool",
                command="session-tool",
                mode="per_session",
                tool_prefix="session-tool",
                session_env={"PROJECT_DIR": "client_cwd"},
            )
        },
    )
    daemon = daemon_module.BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )

    response = daemon._handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": "call",
            "method": "tools/call",
            "params": {
                "name": "session-tool.echo",
                "arguments": {},
                "broker_session_id": "llm-session-a",
            },
        }
    )

    assert response["error"]["message"] == (
        "missing session context for upstream session-tool: client_cwd"
    )
    assert created == []
    assert daemon._upstream_health()["session-tool"]["last_error"] is None


def test_daemon_rejects_per_session_stdio_without_broker_session_id(
    tmp_path: Path,
) -> None:
    import mcp_broker.daemon as daemon_module
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig

    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={
            "browser-session": UpstreamConfig(
                name="browser-session",
                command="browser-session",
                mode="per_session",
                tool_prefix="browser-session",
            )
        },
    )
    daemon = daemon_module.BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )

    response = daemon._handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": "call",
            "method": "tools/call",
            "params": {
                "name": "browser-session.browser_console_messages",
                "arguments": {},
            },
        }
    )

    assert response["error"] == {
        "code": -32000,
        "message": "broker_session_id is required for per_session upstream: browser-session",
    }


def test_daemon_reads_session_id_from_mcp_meta_and_validates_type(tmp_path: Path) -> None:
    from mcp_broker.daemon import BrokerDaemon

    daemon = BrokerDaemon(runtime_root=tmp_path / "runtime", socket_path=tmp_path / "broker.sock")

    assert (
        daemon._session_id_from_params({"_meta": {"mcp_broker": {"session_id": "meta-session"}}})
        == "meta-session"
    )
    with pytest.raises(ValueError, match="broker_session_id must be a non-empty string"):
        daemon._session_id_from_params({"broker_session_id": ""})


def test_daemon_reads_client_cwd_from_mcp_meta(tmp_path: Path) -> None:
    from mcp_broker.daemon import BrokerDaemon

    daemon = BrokerDaemon(runtime_root=tmp_path / "runtime", socket_path=tmp_path / "broker.sock")
    client_project = tmp_path / "client-project"

    assert daemon._session_context_from_params(
        {"_meta": {"mcp_broker": {"client_cwd": str(client_project)}}}
    ) == {"client_cwd": str(client_project)}


def test_daemon_ignores_non_mapping_mcp_meta_for_client_cwd(tmp_path: Path) -> None:
    from mcp_broker.daemon import BrokerDaemon

    daemon = BrokerDaemon(runtime_root=tmp_path / "runtime", socket_path=tmp_path / "broker.sock")

    assert daemon._session_context_from_params({"_meta": {"mcp_broker": "invalid"}}) == {}


@pytest.mark.parametrize(
    "params",
    [
        {"broker_client_cwd": ""},
        {"broker_client_cwd": 7},
        {"_meta": {"mcp_broker": {"client_cwd": ""}}},
    ],
)
def test_daemon_rejects_empty_or_non_string_client_cwd(
    tmp_path: Path,
    params: object,
) -> None:
    from mcp_broker.daemon import BrokerDaemon

    daemon = BrokerDaemon(runtime_root=tmp_path / "runtime", socket_path=tmp_path / "broker.sock")

    with pytest.raises(ValueError, match="broker_client_cwd must be a non-empty string"):
        daemon._session_context_from_params(params)


def test_daemon_rejects_relative_client_cwd(tmp_path: Path) -> None:
    from mcp_broker.daemon import BrokerDaemon

    daemon = BrokerDaemon(runtime_root=tmp_path / "runtime", socket_path=tmp_path / "broker.sock")

    with pytest.raises(ValueError, match="broker_client_cwd must be an absolute path"):
        daemon._session_context_from_params({"broker_client_cwd": "relative/project"})


def test_daemon_effective_profile_name_returns_resolved_profile(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig
    from mcp_broker.daemon import BrokerDaemon
    from mcp_broker.profiles import ToolExposureProfile

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
        profiles={
            "claude": ToolExposureProfile(name="claude", max_tools=80, compact_tools_enabled=True)
        },
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )

    assert daemon._effective_profile_name({"params": {"profile": "claude"}}) == "claude"


def test_daemon_health_aggregates_per_session_stdio_clients(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig
    from mcp_broker.daemon import BrokerDaemon

    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={
            "browser-session": UpstreamConfig(
                name="browser-session",
                command="browser-session",
                mode="per_session",
                tool_prefix="browser-session",
            )
        },
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    daemon._stdio_upstreams[("browser-session", "llm-session-a")] = CreatedClient(1)
    daemon._stdio_upstreams[("browser-session", "llm-session-b")] = CreatedClient(2)

    assert daemon._upstream_health()["browser-session"] == {
        "state": "running",
        "pid": None,
        "cpu_percent": None,
        "memory_mb": None,
        "restarts": 0,
        "last_error": None,
        "auth_probe": "none",
        "sessions": 2,
    }


def test_daemon_shutdown_names_per_session_stdio_clients(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig
    from mcp_broker.daemon import BrokerDaemon

    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={
            "browser-session": UpstreamConfig(
                name="browser-session",
                command="browser-session",
                mode="per_session",
                tool_prefix="browser-session",
            )
        },
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    daemon._stdio_upstreams[("browser-session", "llm-session-a")] = CreatedClient(1)

    assert daemon._shutdown_upstreams() == {
        "stopped_upstreams": ["browser-session:llm-session-a"],
        "remaining_broker_processes": [],
    }


def test_daemon_can_stop_one_broker_session_without_stopping_shared_clients(
    tmp_path: Path,
) -> None:
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig
    from mcp_broker.daemon import BrokerDaemon

    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={
            "read-store": UpstreamConfig(name="read-store", command="read-store", tool_prefix="read-store"),
            "browser-session": UpstreamConfig(
                name="browser-session",
                command="browser-session",
                mode="per_session",
                tool_prefix="browser-session",
            ),
        },
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    daemon._stdio_upstreams["read-store"] = CreatedClient(1)
    daemon._stdio_upstreams[("browser-session", "llm-session-a")] = CreatedClient(2)
    daemon._stdio_upstreams[("browser-session", "llm-session-b")] = CreatedClient(3)

    response = daemon._handle_request(
        {
            "id": "session-stop",
            "method": "broker/session/stop",
            "params": {"broker_session_id": "llm-session-a"},
        }
    )

    assert response == {
        "id": "session-stop",
        "result": {
            "stopped_upstreams": ["browser-session:llm-session-a"],
            "remaining_broker_processes": [],
        },
    }
    assert set(daemon._stdio_upstreams) == {
        "read-store",
        ("browser-session", "llm-session-b"),
    }


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({}, "broker_session_id is required"),
        ({"broker_session_id": ""}, "broker_session_id must be a non-empty string"),
    ],
)
def test_daemon_session_stop_rejects_missing_or_invalid_session_id(
    tmp_path: Path,
    params: dict[str, object],
    message: str,
) -> None:
    from mcp_broker.daemon import BrokerDaemon

    daemon = BrokerDaemon(runtime_root=tmp_path / "runtime", socket_path=tmp_path / "broker.sock")

    response = daemon._handle_request(
        {
            "id": "session-stop",
            "method": "broker/session/stop",
            "params": params,
        }
    )

    assert response == {
        "id": "session-stop",
        "error": {"code": "invalid_params", "message": message},
    }


def test_daemon_auth_repair_can_return_setup_result_without_retry(tmp_path: Path) -> None:
    from mcp_broker.config import (
        AuthRepairPolicy,
        BrokerConfig,
        BrokerSettings,
        RuntimeConfig,
        UpstreamConfig,
    )
    from mcp_broker.daemon import BrokerDaemon

    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={
            "notebook-service": UpstreamConfig(
                name="notebook-service",
                command="notebook-service",
                tool_prefix="notebook-service",
                auth_repair=AuthRepairPolicy(
                    tool="setup_auth",
                    arguments={},
                    trigger_errors=("Not authenticated",),
                    retry_original=False,
                    timeout_seconds=300,
                ),
            )
        },
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    daemon._stdio_upstreams["notebook-service"] = SequenceClient(
        [
            {
                "isError": True,
                "content": [{"type": "text", "text": "Not authenticated"}],
            },
            {"content": [{"type": "text", "text": "auth setup started"}]},
        ]
    )

    result = daemon._call_stdio_upstream("notebook-service", "list_notebooks", {}, 60)

    assert result == {"content": [{"type": "text", "text": "auth setup started"}]}
    assert daemon._stdio_upstreams["notebook-service"].call_calls == [
        ("list_notebooks", {}, 60),
        ("setup_auth", {}, 300),
    ]
    assert daemon._upstream_health()["notebook-service"]["auth_state"] == "authenticated"


def test_daemon_records_no_retry_auth_repair_error_as_failure(tmp_path: Path) -> None:
    from mcp_broker.config import (
        AuthRepairPolicy,
        BrokerConfig,
        BrokerSettings,
        RuntimeConfig,
        UpstreamConfig,
    )
    from mcp_broker.daemon import BrokerDaemon

    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={
            "notebook-service": UpstreamConfig(
                name="notebook-service",
                command="notebook-service",
                tool_prefix="notebook-service",
                auth_repair=AuthRepairPolicy(
                    tool="setup_auth",
                    trigger_errors=("Not authenticated",),
                    retry_original=False,
                ),
            )
        },
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    auth_error = {
        "isError": True,
        "content": [{"type": "text", "text": "Error: Not authenticated"}],
    }
    daemon._stdio_upstreams["notebook-service"] = SequenceClient([auth_error, auth_error])

    result = daemon._call_stdio_upstream("notebook-service", "list_notebooks", {}, 60)

    assert result == auth_error
    assert daemon._upstream_health()["notebook-service"] == {
        "state": "running",
        "pid": None,
        "cpu_percent": None,
        "memory_mb": None,
        "restarts": 0,
        "last_error": None,
        "auth_probe": "auth_repair_configured",
        "auth_state": "unauthenticated",
        "auth_repair_attempts": 1,
        "auth_repair_successes": 0,
        "auth_repair_failures": 1,
    }


def test_auth_repair_matcher_ignores_non_auth_content() -> None:
    from mcp_broker.config import AuthRepairPolicy, UpstreamConfig
    from mcp_broker.daemon import _result_matches_auth_repair

    upstream = UpstreamConfig(
        name="notebook-service",
        command="notebook-service",
        auth_repair=AuthRepairPolicy(
            tool="setup_auth",
            trigger_errors=("Not authenticated",),
        ),
    )

    assert _result_matches_auth_repair(upstream, {"content": []}) is False
    assert (
        _result_matches_auth_repair(
            upstream,
            {"content": [{"type": "text", "text": "Not authenticated"}]},
        )
        is False
    )
    assert (
        _result_matches_auth_repair(
            upstream,
            {"isError": True, "content": [{"type": "text", "text": "Other failure"}]},
        )
        is False
    )


def test_result_content_text_handles_non_text_payloads() -> None:
    from mcp_broker.daemon import _result_content_text

    assert _result_content_text({"content": "not-a-list"}) == ""
    assert (
        _result_content_text(
            {
                "content": [
                    "bad-item",
                    {"type": "image", "data": "..."},
                    {"type": "text", "text": "usable"},
                ]
            }
        )
        == "usable"
    )


def test_daemon_broker_facade_maps_invalid_requests_to_jsonrpc_errors(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig
    from mcp_broker.daemon import BrokerDaemon
    from mcp_broker.profiles import ToolExposureProfile

    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        profiles={"llm-profile": ToolExposureProfile(name="llm-profile", max_tools=80, compact_tools_enabled=True)},
        upstreams={
            "read-store": UpstreamConfig(
                name="read-store",
                command="read-store",
                tool_prefix="read-store",
                profiles=("llm-profile",),
            )
        },
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    daemon._stdio_upstreams["read-store"] = CatalogClient(
        tools=[{"name": "search", "description": "Search project read-store"}],
        response={"content": []},
    )

    cases = [
        (
            "unknown",
            {"name": "broker.unknown", "arguments": {}, "profile": "llm-profile"},
            "unknown broker tool: broker.unknown",
        ),
        (
            "bad-describe",
            {"name": "broker.describe_tool", "arguments": {"name": 12}, "profile": "llm-profile"},
            "broker.describe_tool requires string name",
        ),
        (
            "missing-describe",
            {
                "name": "broker.describe_tool",
                "arguments": {"name": "read-store.missing"},
                "profile": "llm-profile",
            },
            "broker tool not found: read-store.missing",
        ),
        (
            "bad-call",
            {
                "name": "broker.call_tool",
                "arguments": {"name": "read-store.search", "arguments": []},
                "profile": "llm-profile",
            },
            "broker.call_tool requires name and object arguments",
        ),
    ]

    for request_id, params, message in cases:
        response = daemon._handle_jsonrpc_request(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": params,
            }
        )

        assert response["error"] == {"code": -32000, "message": message}


def test_daemon_broker_catalog_skips_unavailable_or_disallowed_entries(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig
    from mcp_broker.catalog import BrokerCatalogFacade
    from mcp_broker.daemon import BrokerDaemon
    from mcp_broker.profiles import ToolExposureProfile

    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        profiles={},
        upstreams={
            "read-store": UpstreamConfig(
                name="read-store",
                command="read-store",
                tool_prefix="read-store",
                profiles=("llm-profile",),
            ),
            "disabled": UpstreamConfig(
                name="disabled",
                command="disabled",
                enabled=False,
                tool_prefix="disabled",
                profiles=("llm-profile",),
            ),
            "writeable": UpstreamConfig(
                name="writeable",
                command="writeable",
                mutating=True,
                tool_prefix="writeable",
                profiles=("llm-profile",),
            ),
        },
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    daemon._stdio_upstreams["read-store"] = CatalogClient(
        tools=[
            {"description": "missing name"},
            {"name": "search", "description": "Search project read-store"},
        ],
        response={"content": []},
    )
    daemon._stdio_upstreams["disabled"] = CatalogClient(
        tools=[{"name": "hidden", "description": "Hidden"}],
        response={"content": []},
    )
    daemon._stdio_upstreams["writeable"] = CatalogClient(
        tools=[{"name": "write", "description": "Write"}],
        response={"content": []},
    )

    response = BrokerCatalogFacade(
        broker_config=config,
        profile=ToolExposureProfile(name="llm-profile", max_tools=80, compact_tools_enabled=True),
        list_upstream=daemon._list_upstream,
        call_upstream=daemon._call_upstream,
        call_locks=daemon._upstream_call_locks,
    ).call_tool("broker.search_tools", {"query": ""})

    assert response["structuredContent"] == {
        "matches": [
            {
                "name": "read-store.search",
                "upstream": "read-store",
                "description": "Search project read-store",
                "purpose": "",
                "tags": [],
                "mutating": False,
            }
        ]
    }
    assert daemon._stdio_upstreams["disabled"].list_calls == []
    assert daemon._stdio_upstreams["writeable"].list_calls == []


def test_daemon_broker_catalog_skips_upstreams_that_fail_tool_listing(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig
    from mcp_broker.catalog import BrokerCatalogFacade
    from mcp_broker.profiles import ToolExposureProfile

    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        profiles={},
        upstreams={
            "read-store": UpstreamConfig(
                name="read-store",
                command="read-store",
                tool_prefix="read-store",
                profiles=("llm-profile",),
            ),
            "remote-repo": UpstreamConfig(
                name="remote-repo",
                command="remote-repo",
                tool_prefix="remote-repo",
                profiles=("llm-profile",),
            ),
        },
    )

    def list_upstream(upstream_name: str, _timeout: int) -> list[dict[str, object]]:
        if upstream_name == "remote-repo":
            raise ValueError("missing environment variable for upstream remote-repo")
        return [{"name": "get_project_scope", "description": "Current project scope"}]

    response = BrokerCatalogFacade(
        broker_config=config,
        profile=ToolExposureProfile(name="llm-profile", max_tools=80, compact_tools_enabled=True),
        list_upstream=list_upstream,
        call_upstream=lambda *_args: {"content": []},
        call_locks={},
    ).call_tool("broker.search_tools", {"query": "scope"})

    assert response["structuredContent"] == {
        "matches": [
            {
                "name": "read-store.get_project_scope",
                "upstream": "read-store",
                "description": "Current project scope",
                "purpose": "",
                "tags": [],
                "mutating": False,
            }
        ],
        "skipped_upstreams": {
            "remote-repo": "missing environment variable for upstream remote-repo",
        },
    }

    skipped_search = BrokerCatalogFacade(
        broker_config=config,
        profile=ToolExposureProfile(name="llm-profile", max_tools=80, compact_tools_enabled=True),
        list_upstream=list_upstream,
        call_upstream=lambda *_args: {"content": []},
        call_locks={},
    ).call_tool("broker.search_tools", {"query": "remote-repo"})

    assert skipped_search["structuredContent"] == {
        "matches": [
            {
                "name": "remote-repo",
                "upstream": "remote-repo",
                "description": "upstream unavailable: missing environment variable for upstream remote-repo",
                "purpose": "",
                "tags": [],
                "mutating": False,
                "available": False,
            }
        ],
        "skipped_upstreams": {
            "remote-repo": "missing environment variable for upstream remote-repo",
        },
    }


def test_daemon_tools_list_namespaces_stdio_tools(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig
    from mcp_broker.daemon import BrokerDaemon

    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={
            "fake": UpstreamConfig(
                name="fake",
                command="fake",
                mode="shared",
                enabled=True,
                tool_prefix="fake",
            )
        },
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    daemon._stdio_upstreams["fake"] = ListingClient(
        [{"name": "echo", "description": "Echo input"}]
    )
    daemon._handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": "init",
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25"},
        }
    )

    response = daemon._handle_jsonrpc_request(
        {"jsonrpc": "2.0", "id": "list", "method": "tools/list"}
    )

    assert response["id"] == "list"
    assert response["result"] == {
        "tools": [{"name": "fake.echo", "description": "Echo input"}]
    }


def test_daemon_tools_list_requires_initialize_and_config(tmp_path: Path) -> None:
    from mcp_broker.daemon import BrokerDaemon

    configured = BrokerDaemon(
        runtime_root=tmp_path / "runtime",
        socket_path=tmp_path / "broker.sock",
        broker_config=_empty_config(tmp_path),
    )

    not_initialized = configured._handle_jsonrpc_request(
        {"jsonrpc": "2.0", "id": "list", "method": "tools/list"}
    )

    assert not_initialized["error"] == {"code": -32002, "message": "Server not initialized"}

    missing_config = BrokerDaemon(
        runtime_root=tmp_path / "runtime2",
        socket_path=tmp_path / "broker2.sock",
        broker_config=None,
    )
    missing_config._handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": "init",
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25"},
        }
    )
    no_config = missing_config._handle_jsonrpc_request(
        {"jsonrpc": "2.0", "id": "list", "method": "tools/list"}
    )

    assert no_config["error"] == {"code": -32000, "message": "broker config is not loaded"}


def test_daemon_tools_list_returns_compact_profile_without_starting_upstreams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig
    from mcp_broker.daemon import BrokerDaemon
    from mcp_broker.profiles import ToolExposureProfile

    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        profiles={
            "llm-profile": ToolExposureProfile(
                name="llm-profile",
                max_tools=80,
                compact_tools_enabled=True,
            )
        },
        upstreams={
            "remote-repo": UpstreamConfig(
                name="remote-repo",
                command="https://remote.example.invalid/mcp/",
                transport="http",
                tool_prefix="remote-repo",
                env={"GITHUB_PERSONAL_ACCESS_TOKEN": "CODEX_GITHUB_PERSONAL_ACCESS_TOKEN"},
                profiles=("llm-profile",),
            )
        },
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    daemon._handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": "init",
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25"},
        }
    )

    def fail_if_called(
        _name: str,
        _timeout_seconds: int,
        *,
        session_id: str | None = None,
        session_context: dict[str, str] | None = None,
    ) -> list[dict[str, object]]:
        assert session_id is None
        assert session_context == {}
        raise AssertionError("compact tools/list started an upstream")

    monkeypatch.setattr(daemon, "_list_upstream", fail_if_called)

    response = daemon._handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": "compact",
            "method": "tools/list",
            "params": {"profile": "llm-profile"},
        }
    )

    assert [tool["name"] for tool in response["result"]["tools"]] == [
        "broker.search_tools",
        "broker.describe_tool",
        "broker.call_tool",
        "broker.status",
    ]
    assert all(len(tool["description"]) >= 160 for tool in response["result"]["tools"])
    assert response["result"]["tools"][0]["inputSchema"]["properties"]["query"]["description"]
    assert (
        response["result"]["tools"][2]["inputSchema"]["properties"]["arguments"]["additionalProperties"]
        is True
    )


def test_daemon_tools_list_returns_profile_safe_compact_names(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig
    from mcp_broker.daemon import BrokerDaemon
    from mcp_broker.profiles import ToolExposureProfile

    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        profiles={
            "safe-client": ToolExposureProfile(
                name="safe-client",
                max_tools=80,
                compact_tools_enabled=True,
                broker_tool_name_style="snake",
            )
        },
        upstreams={},
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    daemon._handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": "init",
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25"},
        }
    )

    response = daemon._handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": "compact",
            "method": "tools/list",
            "params": {"profile": "safe-client"},
        }
    )

    assert [tool["name"] for tool in response["result"]["tools"]] == [
        "broker_search_tools",
        "broker_describe_tool",
        "broker_call_tool",
        "broker_status",
    ]


def test_daemon_accepts_profile_safe_broker_tool_aliases(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig
    from mcp_broker.daemon import BrokerDaemon
    from mcp_broker.profiles import ToolExposureProfile

    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        profiles={
            "safe-client": ToolExposureProfile(
                name="safe-client",
                max_tools=80,
                compact_tools_enabled=True,
                broker_tool_name_style="snake",
            )
        },
        upstreams={},
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )

    response = daemon._handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": "status",
            "method": "tools/call",
            "params": {
                "profile": "safe-client",
                "name": "broker_status",
                "arguments": {},
            },
        }
    )

    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["profile"] == "safe-client"


def test_daemon_tools_list_maps_upstream_list_errors(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig
    from mcp_broker.daemon import BrokerDaemon
    from mcp_broker.upstream_stdio import StdioUpstreamError

    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={"fake": UpstreamConfig(name="fake", command="fake")},
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    daemon._stdio_upstreams["fake"] = ListErrorClient(StdioUpstreamError("list failed"))
    daemon._handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": "init",
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25"},
        }
    )

    response = daemon._handle_jsonrpc_request(
        {"jsonrpc": "2.0", "id": "list", "method": "tools/list"}
    )

    assert response["error"] == {"code": -32000, "message": "list failed"}


@pytest.mark.error_simulation
def test_daemon_tools_list_and_call_route_http_upstreams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_broker.daemon as daemon_module
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig

    class FakeHttpUpstream:
        def __init__(self, upstream, *, environ=None):
            self.upstream = upstream
            self.environ = environ
            self.calls = []

        def list_tools(self, *, timeout_seconds):
            self.calls.append(("tools/list", timeout_seconds))
            return [{"name": "search_repositories", "description": "Search repositories"}]

        def call_tool(self, tool_name, arguments, *, timeout_seconds):
            self.calls.append(("tools/call", tool_name, arguments, timeout_seconds))
            return {"content": [{"type": "text", "text": "found"}]}

        def health_snapshot(self):
            return {
                "state": "reachable",
                "pid": None,
                "cpu_percent": None,
                "memory_mb": None,
                "restarts": 0,
                "last_error": None,
            }

    monkeypatch.setattr(daemon_module, "HttpUpstreamClient", FakeHttpUpstream)
    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={
            "remote-repo": UpstreamConfig(
                name="remote-repo",
                command="https://remote.example.invalid/mcp/",
                transport="http",
                mode="shared",
                tool_prefix="remote-repo",
                profiles=("llm-profile",),
            )
        },
    )
    daemon = daemon_module.BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    daemon._protocol._initialize_seen = True

    list_response = daemon._handle_jsonrpc_request(
        {"jsonrpc": "2.0", "id": "list-http", "method": "tools/list"}
    )
    call_response = daemon._handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": "call-http",
            "method": "tools/call",
            "params": {
                "name": "remote-repo.search_repositories",
                "arguments": {"query": "mcp-broker"},
            },
        }
    )

    assert list_response == {
        "id": "list-http",
        "jsonrpc": "2.0",
        "result": {
            "tools": [
                {
                    "name": "remote-repo.search_repositories",
                    "description": "Search repositories",
                }
            ]
        },
    }
    assert call_response == {
        "id": "call-http",
        "jsonrpc": "2.0",
        "result": {"content": [{"type": "text", "text": "found"}]},
    }
    assert daemon._http_upstreams["remote-repo"].calls == [
        ("tools/list", 60),
        ("tools/call", "search_repositories", {"query": "mcp-broker"}, 60),
    ]


def test_daemon_skips_disabled_upstream_when_listing_tools(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig
    from mcp_broker.daemon import BrokerDaemon

    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={
            "disabled": UpstreamConfig(
                name="disabled",
                command="disabled",
                mode="shared",
                enabled=False,
                tool_prefix="disabled",
            ),
            "hidden": UpstreamConfig(
                name="hidden",
                command="hidden",
                mode="disabled",
                enabled=True,
                tool_prefix="hidden",
            ),
        },
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    daemon._protocol._initialize_seen = True

    response = daemon._handle_jsonrpc_request(
        {"jsonrpc": "2.0", "id": "list-disabled", "method": "tools/list"}
    )

    assert response == {
        "id": "list-disabled",
        "jsonrpc": "2.0",
        "result": {"tools": []},
    }


def test_daemon_maps_http_timeout_and_error_and_reports_http_health(tmp_path: Path) -> None:
    from mcp_broker.broker import BrokerToolError
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig
    from mcp_broker.daemon import BrokerDaemon
    from mcp_broker.upstream_http import HttpUpstreamError, HttpUpstreamTimeout

    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={
            "remote-repo": UpstreamConfig(
                name="remote-repo",
                command="https://remote.example.invalid/mcp/",
                transport="http",
                mode="shared",
                tool_prefix="remote-repo",
            )
        },
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )

    daemon._http_upstreams["remote-repo"] = RaisingHttpClient(HttpUpstreamTimeout("slow"))
    with pytest.raises(BrokerToolError, match="upstream timed out: remote-repo"):
        daemon._call_http_upstream("remote-repo", "search", {}, 1)

    daemon._http_upstreams["remote-repo"] = RaisingHttpClient(HttpUpstreamError("broken"))
    with pytest.raises(BrokerToolError, match="broken"):
        daemon._call_http_upstream("remote-repo", "search", {}, 1)

    daemon._http_upstreams["remote-repo"] = HealthHttpClient()
    assert daemon._upstream_health()["remote-repo"] == {
        "state": "reachable",
        "pid": None,
        "cpu_percent": None,
        "memory_mb": None,
        "restarts": 0,
        "last_error": None,
        "auth_probe": "none",
    }


def test_daemon_tools_call_rejects_disabled_upstream_without_starting_stdio(
    tmp_path: Path,
) -> None:
    from mcp_broker.config import BrokerSettings, RuntimeConfig, UpstreamConfig
    from mcp_broker.daemon import BrokerDaemon
    from mcp_broker.jsonrpc import JsonRpcRequest

    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={
            "disabled": UpstreamConfig(
                name="disabled",
                command="disabled",
                mode="disabled",
                enabled=True,
                tool_prefix="disabled",
            )
        },
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    request = JsonRpcRequest(
        method="tools/call",
        id="call",
        params={"name": "disabled.echo", "arguments": {}},
        has_id=True,
    )

    response = daemon._handle_tools_call(request)

    assert response.error == {"code": -32000, "message": "tool prefix disabled: disabled"}
    assert daemon._stdio_upstreams == {}


def test_daemon_tools_call_delegates_normal_calls_through_hybrid_router(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_broker.daemon as daemon_module
    import mcp_broker.daemon_tool_calls as daemon_tool_calls_module
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig
    from mcp_broker.jsonrpc import JsonRpcRequest

    router_calls: list[dict[str, object]] = []

    class RecordingHybridRouter:
        def __init__(  # type: ignore[no-untyped-def]
            self,
            *,
            upstreams,
            shared_worker,
            settings,
            profile,
            call_locks,
        ):
            self.upstreams = upstreams
            self.shared_worker = shared_worker
            self.settings = settings
            self.profile = profile
            self.call_locks = call_locks

        def call_tool(
            self,
            *,
            advertised_name,
            arguments,
            edge_caller,
            tenant_context,
            team_id,
            quota_snapshot,
        ):  # type: ignore[no-untyped-def]
            router_calls.append(
                {
                    "advertised_name": advertised_name,
                    "arguments": arguments,
                    "tenant_context": tenant_context,
                    "team_id": team_id,
                    "quota_snapshot": quota_snapshot,
                    "upstreams": sorted(self.upstreams),
                    "shared_worker_type": type(self.shared_worker).__name__,
                }
            )
            return edge_caller("read-store", "search", arguments, 60)

    monkeypatch.setattr(daemon_tool_calls_module, "HybridToolRouter", RecordingHybridRouter)
    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={
            "read-store": UpstreamConfig(
                name="read-store",
                command="read-store",
                tool_prefix="read-store",
            )
        },
    )
    daemon = daemon_module.BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    daemon._stdio_upstreams["read-store"] = CatalogClient(
        tools=[{"name": "search"}],
        response={"content": [{"type": "text", "text": "found"}]},
    )
    request = JsonRpcRequest(
        method="tools/call",
        id="call",
        params={"name": "read-store.search", "arguments": {"query": "refund"}},
        has_id=True,
    )

    response = daemon._handle_tools_call(request)

    assert response.result == {"content": [{"type": "text", "text": "found"}]}
    assert router_calls == [
        {
            "advertised_name": "read-store.search",
            "arguments": {"query": "refund"},
            "tenant_context": None,
            "team_id": None,
            "quota_snapshot": None,
            "upstreams": ["read-store"],
            "shared_worker_type": "SharedWorkerRuntime",
        }
    ]


def test_daemon_maps_stdio_timeout_and_error(tmp_path: Path) -> None:
    from mcp_broker.broker import BrokerToolError
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig
    from mcp_broker.daemon import BrokerDaemon
    from mcp_broker.upstream_stdio import StdioUpstreamError, StdioUpstreamTimeout

    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={"fake": UpstreamConfig(name="fake", command="fake")},
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )

    daemon._stdio_upstreams["fake"] = RaisingClient(StdioUpstreamTimeout("slow"))
    with pytest.raises(BrokerToolError, match="upstream timed out: fake"):
        daemon._call_stdio_upstream("fake", "echo", {}, 1)

    daemon._stdio_upstreams["fake"] = RaisingClient(StdioUpstreamError("broken"))
    with pytest.raises(BrokerToolError, match="broken"):
        daemon._call_stdio_upstream("fake", "echo", {}, 1)


class RaisingClient:
    def __init__(self, exception: Exception) -> None:
        self.exception = exception

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, object],
        *,
        timeout_seconds: int,
    ) -> dict[str, object]:
        raise self.exception


class CreatedClient:
    def __init__(self, client_id: int) -> None:
        self.client_id = client_id
        self.call_calls: list[tuple[str, dict[str, object], int]] = []

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, object],
        *,
        timeout_seconds: int,
    ) -> dict[str, object]:
        self.call_calls.append((tool_name, arguments, timeout_seconds))
        return {"content": [{"type": "text", "text": f"client-{self.client_id}"}]}

    def health_snapshot(self) -> dict[str, object]:
        return {
            "state": "running",
            "pid": None,
            "cpu_percent": None,
            "memory_mb": None,
            "restarts": 0,
            "last_error": None,
        }

    def stop(self) -> list[int]:
        return []


class RecoverableExitedClient:
    def __init__(self, restart_error: Exception | None = None) -> None:
        self.restart_error = restart_error
        self.ensure_running_calls = 0
        self.running = False
        self.last_error: str | None = None

    def ensure_running(self) -> None:
        self.ensure_running_calls += 1
        if self.restart_error is not None:
            self.last_error = str(self.restart_error)
            raise self.restart_error
        self.running = True
        self.last_error = None

    def health_snapshot(self) -> dict[str, object]:
        if not self.running:
            return {
                "state": "exited",
                "pid": None,
                "cpu_percent": None,
                "memory_mb": None,
                "restarts": 0,
                "last_error": self.last_error,
            }
        return {
            "state": "running",
            "pid": 12345,
            "cpu_percent": None,
            "memory_mb": None,
            "restarts": 1,
            "last_error": None,
        }


class RaisingHttpClient:
    def __init__(self, exception: Exception) -> None:
        self.exception = exception

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, object],
        *,
        timeout_seconds: int,
    ) -> dict[str, object]:
        raise self.exception


class HealthHttpClient:
    def health_snapshot(self) -> dict[str, object]:
        return {
            "state": "reachable",
            "pid": None,
            "cpu_percent": None,
            "memory_mb": None,
            "restarts": 0,
            "last_error": None,
        }


class ListingClient:
    def __init__(self, tools: list[dict[str, object]]) -> None:
        self.tools = tools

    def list_tools(self, *, timeout_seconds: int) -> list[dict[str, object]]:
        assert timeout_seconds == DEFAULT_CALL_TIMEOUT_SECONDS
        return self.tools


class ListErrorClient:
    def __init__(self, exception: Exception) -> None:
        self.exception = exception

    def list_tools(self, *, timeout_seconds: int) -> list[dict[str, object]]:
        assert timeout_seconds == DEFAULT_CALL_TIMEOUT_SECONDS
        raise self.exception


class CatalogClient:
    def __init__(self, tools: list[dict[str, object]], response: dict[str, object]) -> None:
        self.tools = tools
        self.response = response
        self.list_calls: list[int] = []
        self.call_calls: list[tuple[str, dict[str, object], int]] = []

    def list_tools(self, *, timeout_seconds: int) -> list[dict[str, object]]:
        self.list_calls.append(timeout_seconds)
        return self.tools

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, object],
        *,
        timeout_seconds: int,
    ) -> dict[str, object]:
        self.call_calls.append((tool_name, arguments, timeout_seconds))
        return self.response


class SequenceClient:
    def __init__(self, responses: list[dict[str, object] | Exception]) -> None:
        self.responses = responses
        self.call_calls: list[tuple[str, dict[str, object], int]] = []
        self.last_error: str | None = None

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, object],
        *,
        timeout_seconds: int,
    ) -> dict[str, object]:
        self.call_calls.append((tool_name, arguments, timeout_seconds))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            self.last_error = str(response)
            raise response
        self.last_error = None
        return response

    def health_snapshot(self) -> dict[str, object]:
        return {
            "state": "running",
            "pid": None,
            "cpu_percent": None,
            "memory_mb": None,
            "restarts": 0,
            "last_error": self.last_error,
        }


class BufferConnection:
    def __init__(self, received: bytes) -> None:
        self.received = received
        self.sent = b""

    def recv(self, _size: int) -> bytes:
        return self.received

    def sendall(self, data: bytes) -> None:
        self.sent += data


class _ContextConnection:
    def __init__(self, name: str) -> None:
        self.name = name

    def __enter__(self) -> "_ContextConnection":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def _empty_config(tmp_path: Path) -> BrokerConfig:
    from mcp_broker.config import BrokerSettings, RuntimeConfig

    return BrokerConfig(
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
