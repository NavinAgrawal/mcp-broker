from pathlib import Path
import json
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

@pytest.mark.error_simulation
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

@pytest.mark.error_simulation
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

@pytest.mark.error_simulation
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
