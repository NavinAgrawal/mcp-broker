from pathlib import Path
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
