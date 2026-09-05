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
        "broker.close_session",
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
        "broker_close_session",
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
