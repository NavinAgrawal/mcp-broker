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
