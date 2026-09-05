import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit


class ExternalUpstream:
    def __init__(self, text: str = "edge") -> None:
        self.text = text
        self.call_calls: list[tuple[str, dict[str, object], int]] = []
        self.list_calls: list[int] = []

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, object],
        *,
        timeout_seconds: int,
    ) -> dict[str, object]:
        self.call_calls.append((tool_name, arguments, timeout_seconds))
        return {"content": [{"type": "text", "text": self.text}]}

    def list_tools(self, *, timeout_seconds: int) -> list[dict[str, object]]:
        self.list_calls.append(timeout_seconds)
        return [{"name": "search", "description": "Search records"}]

    def health_snapshot(self) -> dict[str, object]:
        return {
            "state": "running",
            "pid": 42,
            "cpu_percent": 0.0,
            "memory_mb": 1.0,
            "restarts": 0,
            "last_error": None,
        }

    def stop(self) -> list[int]:
        return []


class OverlapUpstream(ExternalUpstream):
    def __init__(self) -> None:
        super().__init__()
        self._state_lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, object],
        *,
        timeout_seconds: int,
    ) -> dict[str, object]:
        with self._state_lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.05)
        with self._state_lock:
            self.active -= 1
        return super().call_tool(tool_name, arguments, timeout_seconds=timeout_seconds)


def _config(tmp_path: Path, *, upstreams=None, broker=None):
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig

    return BrokerConfig(
        runtime=RuntimeConfig(
            root=tmp_path / "runtime",
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "runtime" / "logs",
            state_dir=tmp_path / "runtime" / "state",
            secrets_dir=tmp_path / "runtime" / "secrets",
        ),
        broker=broker or BrokerSettings(),
        upstreams=upstreams or {},
    )


@pytest.mark.parametrize(
    ("loaded", "params", "expected_code"),
    [
        (False, None, -32000),
        (True, [], -32602),
        (True, {"name": 1}, -32602),
        (True, {"name": "missing.echo", "arguments": {}}, -32000),
    ],
)
def test_tools_call_errors_preserve_request_id(
    tmp_path: Path,
    loaded: bool,
    params: object,
    expected_code: int,
) -> None:
    from mcp_broker.daemon import BrokerDaemon
    from mcp_broker.jsonrpc import JsonRpcRequest

    config = _config(tmp_path)
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config if loaded else None,
    )

    response = daemon._handle_tools_call(
        JsonRpcRequest(method="tools/call", id="request-17", params=params, has_id=True)
    )

    assert response.id == "request-17"
    assert response.error is not None
    assert response.error["code"] == expected_code


def test_tools_call_defaults_missing_arguments_to_empty_object(tmp_path: Path) -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.daemon import BrokerDaemon
    from mcp_broker.jsonrpc import JsonRpcRequest

    config = _config(
        tmp_path,
        upstreams={
            "reader": UpstreamConfig(name="reader", command="reader", tool_prefix="reader")
        },
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    client = ExternalUpstream()
    daemon._stdio_upstreams["reader"] = client

    response = daemon._handle_tools_call(
        JsonRpcRequest(
            method="tools/call",
            id="default-args",
            params={"name": "reader.search"},
            has_id=True,
        )
    )

    assert response.error is None
    assert client.call_calls == [("search", {}, 60)]


def test_tools_call_context_error_preserves_request_id(tmp_path: Path) -> None:
    from mcp_broker.daemon import BrokerDaemon
    from mcp_broker.jsonrpc import JsonRpcRequest

    config = _config(tmp_path)
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )

    response = daemon._handle_tools_call(
        JsonRpcRequest(
            method="tools/call",
            id="bad-context",
            params={
                "name": "broker.status",
                "arguments": {},
                "broker_client_cwd": "relative/path",
            },
            has_id=True,
        )
    )

    assert response.id == "bad-context"
    assert response.error == {
        "code": -32602,
        "message": "broker_client_cwd must be an absolute path",
    }


def test_broker_catalog_error_preserves_request_id(tmp_path: Path) -> None:
    from mcp_broker.daemon import BrokerDaemon
    from mcp_broker.jsonrpc import JsonRpcRequest

    config = _config(tmp_path)
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )

    response = daemon._handle_tools_call(
        JsonRpcRequest(
            method="tools/call",
            id="bad-catalog-call",
            params={"name": "broker.describe_tool", "arguments": {}},
            has_id=True,
        )
    )

    assert response.id == "bad-catalog-call"
    assert response.error == {
        "code": -32000,
        "message": "broker.describe_tool requires string name",
    }


def test_broker_catalog_listing_uses_calling_session_and_context(tmp_path: Path) -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.daemon import BrokerDaemon
    from mcp_broker.jsonrpc import JsonRpcRequest

    config = _config(
        tmp_path,
        upstreams={
            "session-reader": UpstreamConfig(
                name="session-reader",
                command="session-reader",
                mode="per_session",
                tool_prefix="session-reader",
                session_env={"PROJECT_DIR": "client_cwd"},
            )
        },
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    client = ExternalUpstream()
    daemon._stdio_upstreams[("session-reader", "session-a")] = client

    response = daemon._handle_tools_call(
        JsonRpcRequest(
            method="tools/call",
            id="catalog-session",
            params={
                "name": "broker.search_tools",
                "arguments": {"query": "search"},
                "broker_client_cwd": str(tmp_path),
                "_meta": {"mcp_broker": {"session_id": "session-a"}},
            },
            has_id=True,
        )
    )

    assert response.error is None
    assert response.result["structuredContent"]["matches"][0]["name"] == (
        "session-reader.search"
    )
    assert client.list_calls == [60]


def test_broker_catalog_call_injects_calling_project(tmp_path: Path) -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.daemon import BrokerDaemon
    from mcp_broker.jsonrpc import JsonRpcRequest

    repo = tmp_path / "Projects" / "apps" / "mcp-broker"
    (repo / ".git").mkdir(parents=True)
    config = _config(
        tmp_path,
        upstreams={
            "example-store": UpstreamConfig(
                name="example-store",
                command="example-store",
                tool_prefix="example-store",
                inject_cwd_project=True,
            )
        },
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    client = ExternalUpstream()
    daemon._stdio_upstreams["example-store"] = client

    response = daemon._handle_tools_call(
        JsonRpcRequest(
            method="tools/call",
            id="catalog-cwd",
            params={
                "name": "broker.call_tool",
                "arguments": {
                    "name": "example-store.search",
                    "arguments": {"query": "BrokerDaemon"},
                },
                "broker_client_cwd": str(repo / "src"),
            },
            has_id=True,
        )
    )

    assert response.error is None
    assert client.call_calls == [
        (
            "search",
            {
                "query": "BrokerDaemon",
                "project": str(repo).lstrip("/").replace("/", "-"),
            },
            60,
        )
    ]


def test_broker_status_uses_daemon_health_provider(tmp_path: Path) -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.daemon import BrokerDaemon
    from mcp_broker.jsonrpc import JsonRpcRequest

    config = _config(
        tmp_path,
        upstreams={
            "reader": UpstreamConfig(name="reader", command="reader", tool_prefix="reader")
        },
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    daemon._stdio_upstreams["reader"] = ExternalUpstream()

    response = daemon._handle_tools_call(
        JsonRpcRequest(
            method="tools/call",
            id="catalog-status",
            params={"name": "broker.status", "arguments": {}},
            has_id=True,
        )
    )

    payload = json.loads(response.result["content"][0]["text"])
    assert payload["upstreams"]["reader"]["state"] == "running"
    assert payload["upstreams"]["reader"]["pid"] == 42


def test_broker_catalog_calls_share_daemon_serialization_lock(tmp_path: Path) -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.daemon import BrokerDaemon
    from mcp_broker.jsonrpc import JsonRpcRequest

    config = _config(
        tmp_path,
        upstreams={
            "writer": UpstreamConfig(
                name="writer",
                command="writer",
                tool_prefix="writer",
                serialize_calls=True,
            )
        },
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    client = OverlapUpstream()
    daemon._stdio_upstreams["writer"] = client

    def call(number: int):
        return daemon._handle_tools_call(
            JsonRpcRequest(
                method="tools/call",
                id=f"call-{number}",
                params={
                    "name": "broker.call_tool",
                    "arguments": {
                        "name": "writer.write",
                        "arguments": {"number": number},
                    },
                },
                has_id=True,
            )
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(call, (1, 2)))

    assert all(response.error is None for response in responses)
    assert client.max_active == 1


def test_direct_tool_call_uses_custom_namespace_separator(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerSettings, UpstreamConfig
    from mcp_broker.daemon import BrokerDaemon
    from mcp_broker.jsonrpc import JsonRpcRequest

    config = _config(
        tmp_path,
        broker=BrokerSettings(tool_namespace_separator="::"),
        upstreams={
            "reader": UpstreamConfig(name="reader", command="reader", tool_prefix="reader")
        },
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    client = ExternalUpstream()
    daemon._stdio_upstreams["reader"] = client

    response = daemon._handle_tools_call(
        JsonRpcRequest(
            method="tools/call",
            id="custom-separator",
            params={"name": "reader::search", "arguments": {"query": "mcp"}},
            has_id=True,
        )
    )

    assert response.error is None
    assert client.call_calls == [("search", {"query": "mcp"}, 60)]


def test_direct_tool_call_injects_calling_project(tmp_path: Path) -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.daemon import BrokerDaemon
    from mcp_broker.jsonrpc import JsonRpcRequest

    repo = tmp_path / "Projects" / "apps" / "mcp-broker"
    (repo / ".git").mkdir(parents=True)
    config = _config(
        tmp_path,
        upstreams={
            "example-store": UpstreamConfig(
                name="example-store",
                command="example-store",
                tool_prefix="example-store",
                inject_cwd_project=True,
            )
        },
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    client = ExternalUpstream()
    daemon._stdio_upstreams["example-store"] = client

    response = daemon._handle_tools_call(
        JsonRpcRequest(
            method="tools/call",
            id="direct-cwd",
            params={
                "name": "example-store.search",
                "arguments": {"query": "BrokerDaemon"},
                "broker_client_cwd": str(repo / "src"),
            },
            has_id=True,
        )
    )

    assert response.error is None
    assert client.call_calls == [
        (
            "search",
            {
                "query": "BrokerDaemon",
                "project": str(repo).lstrip("/").replace("/", "-"),
            },
            60,
        )
    ]


def test_direct_tool_calls_share_daemon_serialization_lock(tmp_path: Path) -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.daemon import BrokerDaemon
    from mcp_broker.jsonrpc import JsonRpcRequest

    config = _config(
        tmp_path,
        upstreams={
            "writer": UpstreamConfig(
                name="writer",
                command="writer",
                tool_prefix="writer",
                serialize_calls=True,
            )
        },
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    client = OverlapUpstream()
    daemon._stdio_upstreams["writer"] = client

    def call(number: int):
        return daemon._handle_tools_call(
            JsonRpcRequest(
                method="tools/call",
                id=f"direct-{number}",
                params={"name": "writer.write", "arguments": {"number": number}},
                has_id=True,
            )
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(call, (1, 2)))

    assert all(response.error is None for response in responses)
    assert client.max_active == 1


def test_direct_tool_call_passes_shared_routing_context(tmp_path: Path) -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.daemon import BrokerDaemon
    from mcp_broker.jsonrpc import JsonRpcRequest
    from mcp_broker.shared_worker import SharedWorkerRuntime, SharedWorkerTool

    config = _config(
        tmp_path,
        upstreams={
            "example-stateless": UpstreamConfig(
                name="example-stateless",
                command="example-stateless",
                tool_prefix="example",
                mode="shared",
                tags=("stateless", "shared-worker"),
            )
        },
    )
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    daemon._stdio_upstreams["example-stateless"] = ExternalUpstream("edge")
    daemon._shared_worker_runtime = SharedWorkerRuntime(
        tools=[
            SharedWorkerTool(
                upstream_id="example-stateless",
                name="example.echo",
                behavior="echo",
            )
        ]
    )
    quota_snapshot = {
        "kill_switches": {
            "global": False,
            "teams": [],
            "users": [],
            "upstreams": [],
            "tools": [],
        },
        "limits": {
            "global": {"limit": 10, "used": 0},
            "teams": {"team-a": {"limit": 10, "used": 0}},
            "users": {"user-a": {"limit": 10, "used": 0}},
            "upstreams": {"example-stateless": {"limit": 10, "used": 0}},
            "tools": {"example.echo": {"limit": 10, "used": 0}},
        },
    }

    response = daemon._handle_tools_call(
        JsonRpcRequest(
            method="tools/call",
            id="shared-context",
            params={
                "name": "example.echo",
                "arguments": {"message": "from-worker"},
                "tenant_context": {
                    "tenant_id": "tenant-a",
                    "workspace_id": "workspace-a",
                    "user_id": "user-a",
                },
                "team_id": "team-a",
                "quota_snapshot": quota_snapshot,
            },
            has_id=True,
        )
    )

    assert response.error is None
    assert response.result["structuredContent"] == {"message": "from-worker"}
