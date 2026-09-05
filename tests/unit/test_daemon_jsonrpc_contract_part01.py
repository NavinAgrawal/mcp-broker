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
        self.received = [received, b""]
        self.sent = b""

    def recv(self, _size: int) -> bytes:
        return self.received.pop(0)

    def sendall(self, data: bytes) -> None:
        self.sent += data
class _ContextConnection:
    def __init__(self, name: str) -> None:
        self.name = name
        self.timeout: float | None = None

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

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
    server.settimeout(1)

    def serve_once() -> None:
        try:
            connection, _ = server.accept()
            with connection:
                received.append(connection.recv(65536))
                midpoint = len(payload) // 2
                connection.sendall(payload[:midpoint].encode("utf-8"))
                connection.sendall(payload[midpoint:].encode("utf-8"))
                connection.sendall(b"\n")
        except (OSError, socket.timeout):
            return
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

    assert not thread.is_alive()
    assert response["result"]["status"] == "ok"
    assert received == [b'{"id": "broker/health", "method": "broker/health"}\n']

@pytest.mark.error_simulation
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


@pytest.mark.parametrize("disconnect_error", [BrokenPipeError, ConnectionResetError])
def test_daemon_connection_tolerates_client_disconnect_before_response(
    tmp_path: Path,
    disconnect_error: type[OSError],
) -> None:
    from mcp_broker.daemon import BrokerDaemon

    class DisconnectedConnection(BufferConnection):
        def sendall(self, data: bytes) -> None:
            raise disconnect_error("client disconnected")

    daemon = BrokerDaemon(runtime_root=tmp_path / "runtime", socket_path=tmp_path / "broker.sock")
    connection = DisconnectedConnection(b'{"jsonrpc":"2.0","id":"health","method":"broker/health"}\n')

    daemon._handle_connection(connection)


def test_daemon_rejects_request_larger_than_configured_limit(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerSettings
    from mcp_broker.daemon import BrokerDaemon

    config = _empty_config(tmp_path)
    config = BrokerConfig(
        runtime=config.runtime,
        broker=BrokerSettings(socket_max_request_bytes=32),
        upstreams={},
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    connection = BufferConnection(b'{"jsonrpc":"2.0","id":"too-large","method":"broker/health"}\n')

    daemon._handle_connection(connection)

    assert json.loads(connection.sent.decode("utf-8")) == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32600, "message": "Request exceeds 32 bytes"},
    }


def test_daemon_stops_reading_when_client_stalls(tmp_path: Path) -> None:
    from mcp_broker.daemon import BrokerDaemon

    class StalledConnection(BufferConnection):
        def recv(self, _size: int) -> bytes:
            raise socket.timeout("client stalled")

    daemon = BrokerDaemon(runtime_root=tmp_path / "runtime", socket_path=tmp_path / "broker.sock")
    connection = StalledConnection(b"")

    daemon._handle_connection(connection)

    assert connection.sent == b""


def test_daemon_applies_configured_read_timeout_to_real_connection_context(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerSettings
    from mcp_broker.daemon import BrokerDaemon

    config = _empty_config(tmp_path)
    config = BrokerConfig(
        runtime=config.runtime,
        broker=BrokerSettings(socket_read_timeout_seconds=17),
        upstreams={},
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    connection = _ContextConnection("timed")
    daemon._handle_connection = lambda _connection: None  # type: ignore[method-assign]

    daemon._handle_connection_with_context(connection)  # type: ignore[arg-type]

    assert connection.timeout == 17


def test_daemon_connection_reads_request_across_multiple_socket_chunks(tmp_path: Path) -> None:
    from mcp_broker.daemon import BrokerDaemon

    daemon = BrokerDaemon(runtime_root=tmp_path / "runtime", socket_path=tmp_path / "broker.sock")
    payload = json.dumps(
        {
            "method": "broker/health",
            "id": "large-health",
            "attachment_probe": "x" * 818_504,
        }
    ).encode("utf-8") + b"\n"
    connection = BufferConnection(payload[:65_536])
    connection.received = [payload[:65_536], payload[65_536:], b""]

    daemon._handle_connection(connection)

    response = json.loads(connection.sent.decode("utf-8"))
    assert response["id"] == "large-health"
    assert response["result"]["status"] == "ok"
