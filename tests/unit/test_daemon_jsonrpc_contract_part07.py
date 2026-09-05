from pathlib import Path
import pytest
from mcp_broker.config import BrokerConfig
from mcp_broker.schema import DEFAULT_CALL_TIMEOUT_SECONDS
pytestmark = pytest.mark.unit

def test_daemon_exposes_close_session_only_for_the_calling_broker_session(
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
            "shared": UpstreamConfig(name="shared", command="shared", tool_prefix="shared"),
            "session": UpstreamConfig(
                name="session",
                command="session",
                mode="per_session",
                tool_prefix="session",
            ),
        },
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    daemon._stdio_upstreams["shared"] = CreatedClient(1)
    daemon._stdio_upstreams[("session", "codex-a")] = CreatedClient(2)
    daemon._stdio_upstreams[("session", "codex-b")] = CreatedClient(3)

    response = daemon._handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": "close-session",
            "method": "tools/call",
            "params": {
                "name": "broker.close_session",
                "arguments": {},
                "_meta": {"mcp_broker": {"session_id": "codex-a"}},
            },
        }
    )

    assert response == {
        "jsonrpc": "2.0",
        "id": "close-session",
        "result": {
            "structuredContent": {
                "stopped_upstreams": ["session:codex-a"],
                "remaining_broker_processes": [],
            },
            "content": [
                {
                    "type": "text",
                    "text": '{"remaining_broker_processes": [], "stopped_upstreams": ["session:codex-a"]}',
                }
            ],
        },
    }
    assert set(daemon._stdio_upstreams) == {"shared", ("session", "codex-b")}

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

@pytest.mark.error_simulation
def test_daemon_tools_call_delegates_normal_calls_through_hybrid_router(
    tmp_path: Path,
) -> None:
    import mcp_broker.daemon as daemon_module
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig
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
    upstream = CatalogClient(
        tools=[{"name": "search"}],
        response={"content": [{"type": "text", "text": "found"}]},
    )
    daemon._stdio_upstreams["read-store"] = upstream
    request = JsonRpcRequest(
        method="tools/call",
        id="call",
        params={"name": "read-store.search", "arguments": {"query": "refund"}},
        has_id=True,
    )

    response = daemon._handle_tools_call(request)

    assert response.result == {"content": [{"type": "text", "text": "found"}]}
    assert upstream.call_calls == [
        ("search", {"query": "refund"}, DEFAULT_CALL_TIMEOUT_SECONDS)
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
