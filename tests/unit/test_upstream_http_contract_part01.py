from __future__ import annotations
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from typing import Any
import pytest
pytestmark = pytest.mark.unit
class FakeMcpHttpServer:
    def __init__(
        self,
        *,
        status: int = 200,
        tools_result: dict[str, Any] | None = None,
        call_error_message: str | None = None,
        notification_error_message: str | None = None,
        list_notification_only: bool = False,
        list_response_id: int | str | None = None,
        method_statuses: dict[str, list[int]] | None = None,
    ) -> None:
        self.status = status
        self.method_statuses = {
            method: list(statuses) for method, statuses in (method_statuses or {}).items()
        }
        self.tools_result = tools_result or {
            "tools": [
                {
                    "name": "search_repositories",
                    "description": "Search repositories",
                }
            ]
        }
        self.call_error_message = call_error_message
        self.notification_error_message = notification_error_message
        self.list_notification_only = list_notification_only
        self.list_response_id = list_response_id
        self.records: list[dict[str, Any]] = []
        self._server = _FakeThreadingHttpServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever)
        self._thread.daemon = True

    @property
    def url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}/mcp"

    def __enter__(self) -> "FakeMcpHttpServer":
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._server.shutdown()
        self._thread.join(timeout=2)
        self._server.server_close()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.close_connection = True
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                owner.records.append(
                    {
                        "method": payload.get("method"),
                        "params": payload.get("params"),
                        "payload": payload,
                        "headers": dict(self.headers),
                    }
                )
                method_statuses = owner.method_statuses.get(str(payload.get("method")), [])
                status = method_statuses.pop(0) if method_statuses else owner.status
                if status != 200:
                    body = b"auth failed"
                    self.send_response(status)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if payload.get("method") == "initialize":
                    self._send_json(
                        payload["id"],
                        {
                            "protocolVersion": "2025-11-25",
                            "capabilities": {},
                            "serverInfo": {"name": "fake-remote-repo", "version": "1.0.0"},
                        },
                        session_id="session-1",
                    )
                    return
                if payload.get("method") == "notifications/initialized":
                    if owner.notification_error_message is not None:
                        self._send_error(payload.get("id"), owner.notification_error_message)
                        return
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    return
                if payload.get("method") == "tools/list":
                    if owner.list_notification_only:
                        self._send_raw_json({"jsonrpc": "2.0", "method": "notifications/progress"})
                        return
                    self._send_sse(payload["id"], owner.tools_result)
                    return
                if payload.get("method") == "tools/call":
                    if owner.call_error_message is not None:
                        self._send_error(payload["id"], owner.call_error_message)
                        return
                    self._send_json(
                        payload["id"],
                        {"content": [{"type": "text", "text": "repo result"}]},
                    )
                    return
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()

            def log_message(self, *_args: object) -> None:
                return

            def _send_json(
                self,
                request_id: int,
                result: dict[str, Any],
                *,
                session_id: str | None = None,
            ) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Connection", "close")
                if session_id is not None:
                    self.send_header("Mcp-Session-Id", session_id)
                body = json.dumps(
                    {"jsonrpc": "2.0", "id": request_id, "result": result},
                ).encode("utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_sse(self, request_id: int, result: dict[str, Any]) -> None:
                response_id = owner.list_response_id
                if response_id is None:
                    response_id = request_id
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                body = (
                    b"event: message\n"
                    + b"data: "
                    + json.dumps(
                        {"jsonrpc": "2.0", "id": response_id, "result": result},
                    ).encode("utf-8")
                    + b"\n\n"
                )
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_error(self, request_id: int | None, message: str) -> None:
                self._send_raw_json(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32000, "message": message},
                    }
                )

            def _send_raw_json(self, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Connection", "close")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler
class _FakeThreadingHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
def _header(headers: dict[str, str], name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    raise KeyError(name)

def test_http_upstream_initializes_lists_tools_and_reuses_session_header() -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.protocol import SUPPORTED_PROTOCOL_VERSIONS
    from mcp_broker.upstream_http import HttpUpstreamClient

    with FakeMcpHttpServer() as server:
        upstream = UpstreamConfig(
            name="remote-repo",
            command=server.url,
            transport="http",
            env={"GITHUB_PERSONAL_ACCESS_TOKEN": "UNIT_GITHUB_TOKEN"},
        )
        client = HttpUpstreamClient(
            upstream,
            environ={"UNIT_GITHUB_TOKEN": "unit-test-token"},
        )

        tools = client.list_tools(timeout_seconds=2)

    assert tools == [{"name": "search_repositories", "description": "Search repositories"}]
    assert [record["method"] for record in server.records] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]
    assert server.records[0]["headers"]["Authorization"] == "Bearer unit-test-token"
    assert server.records[0]["headers"]["Accept"] == "application/json, text/event-stream"
    assert server.records[0]["headers"]["Content-Type"] == "application/json"
    assert (
        _header(server.records[0]["headers"], "MCP-Protocol-Version")
        == SUPPORTED_PROTOCOL_VERSIONS[0]
    )
    assert "Mcp-Session-Id" not in server.records[0]["headers"]
    assert _header(server.records[2]["headers"], "Mcp-Session-Id") == "session-1"

def test_http_upstream_initialize_payload_contract() -> None:
    from mcp_broker import __version__
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.protocol import SUPPORTED_PROTOCOL_VERSIONS
    from mcp_broker.upstream_http import HttpUpstreamClient

    with FakeMcpHttpServer() as server:
        client = HttpUpstreamClient(
            UpstreamConfig(name="remote-repo", command=server.url, transport="http"),
            environ={},
        )

        client.list_tools(timeout_seconds=2)

    assert server.records[0]["payload"] == {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": SUPPORTED_PROTOCOL_VERSIONS[0],
            "capabilities": {},
            "clientInfo": {"name": "mcp-broker", "version": __version__},
        },
    }
    assert server.records[1]["payload"] == {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    }

def test_http_upstream_initial_health_is_configured_without_error() -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.upstream_http import HttpUpstreamClient

    client = HttpUpstreamClient(
        UpstreamConfig(name="remote-repo", command="https://example.invalid/mcp", transport="http"),
        environ={},
    )

    assert client.health_snapshot() == {
        "state": "configured",
        "pid": None,
        "cpu_percent": None,
        "memory_mb": None,
        "restarts": 0,
        "last_error": None,
    }
    assert client._initialized is False

def test_http_upstream_calls_tool_with_json_response() -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.upstream_http import HttpUpstreamClient

    with FakeMcpHttpServer() as server:
        upstream = UpstreamConfig(name="remote-repo", command=server.url, transport="http")
        client = HttpUpstreamClient(upstream, environ={})

        response = client.call_tool(
            "search_repositories",
            {"query": "mcp-broker"},
            timeout_seconds=2,
        )

    assert response == {"content": [{"type": "text", "text": "repo result"}]}
    assert server.records[-1]["method"] == "tools/call"
    assert server.records[-1]["params"] == {
        "name": "search_repositories",
        "arguments": {"query": "mcp-broker"},
    }
    assert client.health_snapshot()["last_error"] is None

def test_http_upstream_reuses_existing_initialization_for_later_calls() -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.upstream_http import HttpUpstreamClient

    with FakeMcpHttpServer() as server:
        upstream = UpstreamConfig(name="remote-repo", command=server.url, transport="http")
        client = HttpUpstreamClient(upstream, environ={})

        tools = client.list_tools(timeout_seconds=2)
        response = client.call_tool(
            "search_repositories",
            {"query": "mcp-broker"},
            timeout_seconds=2,
        )

    assert tools == [{"name": "search_repositories", "description": "Search repositories"}]
    assert response == {"content": [{"type": "text", "text": "repo result"}]}
    assert [record["method"] for record in server.records] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
    ]
    assert _header(server.records[2]["headers"], "Mcp-Session-Id") == "session-1"
    assert _header(server.records[3]["headers"], "Mcp-Session-Id") == "session-1"

def test_http_upstream_errors_do_not_include_token_values() -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.upstream_http import HttpUpstreamClient, HttpUpstreamError

    with FakeMcpHttpServer(status=401) as server:
        upstream = UpstreamConfig(
            name="remote-repo",
            command=server.url,
            transport="http",
            env={"GITHUB_PERSONAL_ACCESS_TOKEN": "UNIT_GITHUB_TOKEN"},
        )
        client = HttpUpstreamClient(
            upstream,
            environ={"UNIT_GITHUB_TOKEN": "unit-test-token"},
        )

        with pytest.raises(HttpUpstreamError) as exc:
            client.list_tools(timeout_seconds=2)

    assert "status 401" in str(exc.value)
    assert "unit-test-token" not in str(exc.value)
    assert [record["method"] for record in server.records] == ["initialize"]

def test_http_upstream_retries_transient_status_and_reuses_session_header() -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.schema import HealthPolicy
    from mcp_broker.upstream_http import HttpUpstreamClient

    with FakeMcpHttpServer(method_statuses={"tools/list": [503, 200]}) as server:
        upstream = UpstreamConfig(
            name="remote",
            command=server.url,
            transport="http",
            health=HealthPolicy(http_retry_attempts=1, http_retry_backoff_seconds=0),
        )
        client = HttpUpstreamClient(upstream, environ={})

        tools = client.list_tools(timeout_seconds=2)

    assert tools == [{"name": "search_repositories", "description": "Search repositories"}]
    assert client.health_snapshot()["last_error"] is None
    assert [record["method"] for record in server.records] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/list",
    ]
    assert _header(server.records[2]["headers"], "Mcp-Session-Id") == "session-1"
    assert _header(server.records[3]["headers"], "Mcp-Session-Id") == "session-1"

@pytest.mark.error_simulation
def test_http_upstream_retry_uses_configured_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.schema import HealthPolicy
    from mcp_broker import upstream_http
    from mcp_broker.upstream_http import HttpUpstreamClient

    sleeps: list[int] = []
    monkeypatch.setattr(upstream_http, "_retry_wait", sleeps.append)

    with FakeMcpHttpServer(method_statuses={"tools/list": [503, 200]}) as server:
        client = HttpUpstreamClient(
            UpstreamConfig(
                name="remote",
                command=server.url,
                transport="http",
                health=HealthPolicy(
                    http_retry_attempts=1,
                    http_retry_backoff_seconds=2,
                ),
            ),
            environ={},
        )

        client.list_tools(timeout_seconds=2)

    assert sleeps == [2]

def test_http_upstream_does_not_retry_non_retryable_status() -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.schema import HealthPolicy
    from mcp_broker.upstream_http import HttpUpstreamClient, HttpUpstreamError

    with FakeMcpHttpServer(method_statuses={"tools/list": [404, 200]}) as server:
        upstream = UpstreamConfig(
            name="remote",
            command=server.url,
            transport="http",
            health=HealthPolicy(http_retry_attempts=1, http_retry_backoff_seconds=0),
        )
        client = HttpUpstreamClient(upstream, environ={})

        with pytest.raises(HttpUpstreamError, match="status 404"):
            client.list_tools(timeout_seconds=2)

    assert [record["method"] for record in server.records] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]

@pytest.mark.error_simulation
def test_http_upstream_does_not_retry_timeout_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.schema import HealthPolicy
    from mcp_broker.upstream_http import (
        HttpUpstreamClient,
        HttpUpstreamTimeout,
    )

    client = HttpUpstreamClient(
        UpstreamConfig(
            name="remote",
            command="https://example.invalid/mcp",
            transport="http",
            health=HealthPolicy(http_retry_attempts=2, http_retry_backoff_seconds=0),
        ),
        environ={},
    )
    calls = 0

    def fail_once(*_args: object, **_kwargs: object) -> None:
        nonlocal calls
        calls += 1
        raise HttpUpstreamTimeout("upstream timed out: remote")

    monkeypatch.setattr(client, "_http_post_once", fail_once)

    with pytest.raises(HttpUpstreamTimeout, match="upstream timed out: remote"):
        client._http_post({"jsonrpc": "2.0", "id": 0, "method": "tools/list"}, timeout_seconds=1)

    assert calls == 1

@pytest.mark.error_simulation
@pytest.mark.parametrize("backoff_seconds", [0, 7])
def test_retry_wait_passes_configured_backoff_to_event_wait(
    backoff_seconds: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import upstream_http
    from mcp_broker.upstream_http import _retry_wait

    waits: list[int | None] = []

    class RecordingEvent:
        def wait(self, timeout: int | None) -> None:
            waits.append(timeout)

    monkeypatch.setattr(upstream_http.threading, "Event", RecordingEvent)

    _retry_wait(backoff_seconds)

    assert waits == [backoff_seconds]

@pytest.mark.error_simulation
def test_http_upstream_retry_attempts_are_exhausted_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.schema import HealthPolicy
    from mcp_broker.upstream_http import HttpUpstreamClient, HttpUpstreamError

    client = HttpUpstreamClient(
        UpstreamConfig(
            name="remote",
            command="https://example.invalid/mcp",
            transport="http",
            health=HealthPolicy(http_retry_attempts=2, http_retry_backoff_seconds=0),
        ),
        environ={},
    )
    calls: list[int] = []

    def fail_with_retryable_status(*_args: object, **_kwargs: object) -> None:
        calls.append(len(calls))
        if len(calls) > 3:
            raise AssertionError("retry budget was not exhausted")
        raise HttpUpstreamError("upstream HTTP request failed: remote: status 503")

    monkeypatch.setattr(client, "_http_post_once", fail_with_retryable_status)

    with pytest.raises(HttpUpstreamError, match="status 503"):
        client._http_post({"jsonrpc": "2.0", "id": 0, "method": "tools/list"}, timeout_seconds=1)

    assert calls == [0, 1, 2]

@pytest.mark.error_simulation
def test_http_upstream_retry_budget_does_not_grow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.schema import HealthPolicy
    from mcp_broker.upstream_http import HttpUpstreamClient, HttpUpstreamError

    client = HttpUpstreamClient(
        UpstreamConfig(
            name="remote",
            command="https://example.invalid/mcp",
            transport="http",
            health=HealthPolicy(http_retry_attempts=1, http_retry_backoff_seconds=0),
        ),
        environ={},
    )
    calls = 0

    def fail_with_retryable_status(*_args: object, **_kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls > 2:
            raise AssertionError("retry loop exceeded configured budget")
        raise HttpUpstreamError("upstream HTTP request failed: remote: status 503")

    monkeypatch.setattr(client, "_http_post_once", fail_with_retryable_status)

    with pytest.raises(HttpUpstreamError, match="status 503"):
        client._http_post({"jsonrpc": "2.0", "id": 0, "method": "tools/list"}, timeout_seconds=1)

    assert calls == 2

@pytest.mark.error_simulation
def test_http_upstream_public_methods_forward_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.upstream_http import HttpUpstreamClient

    client = HttpUpstreamClient(
        UpstreamConfig(name="remote", command="https://example.invalid/mcp", transport="http"),
        environ={},
    )
    calls: list[tuple[str, dict[str, Any] | None, int]] = []

    def jsonrpc_request(
        method: str,
        params: dict[str, Any] | None,
        *,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        calls.append((method, params, timeout_seconds))
        if method == "tools/list":
            return {"tools": [{"name": "search"}]}
        return {"content": [{"type": "text", "text": "ok"}]}

    monkeypatch.setattr(client, "_jsonrpc_request", jsonrpc_request)

    assert client.list_tools(timeout_seconds=17) == [{"name": "search"}]
    assert client.call_tool("search", {"q": "mcp"}, timeout_seconds=23) == {
        "content": [{"type": "text", "text": "ok"}]
    }
    assert calls == [
        ("tools/list", None, 17),
        ("tools/call", {"name": "search", "arguments": {"q": "mcp"}}, 23),
    ]

@pytest.mark.error_simulation
def test_http_upstream_jsonrpc_request_forwards_timeout_to_initialize_and_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.upstream_http import HttpUpstreamClient

    client = HttpUpstreamClient(
        UpstreamConfig(name="remote", command="https://example.invalid/mcp", transport="http"),
        environ={},
    )
    calls: list[tuple[str, int]] = []

    def initialize(*, timeout_seconds: int) -> None:
        calls.append(("initialize", timeout_seconds))
        client._initialized = True

    def post_jsonrpc(
        payload: dict[str, Any],
        *,
        timeout_seconds: int,
        expected_id: int,
    ) -> dict[str, Any]:
        calls.append((f"post:{payload['method']}:{expected_id}", timeout_seconds))
        return {"jsonrpc": "2.0", "id": expected_id, "result": {"ok": True}}

    monkeypatch.setattr(client, "_initialize", initialize)
    monkeypatch.setattr(client, "_post_jsonrpc", post_jsonrpc)

    assert client._jsonrpc_request("tools/list", None, timeout_seconds=31) == {"ok": True}
    assert calls == [("initialize", 31), ("post:tools/list:0", 31)]

@pytest.mark.error_simulation
def test_http_upstream_post_helpers_forward_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker import upstream_http
    from mcp_broker.upstream_http import HttpUpstreamClient

    client = HttpUpstreamClient(
        UpstreamConfig(name="remote", command="https://example.invalid/mcp", transport="http"),
        environ={},
    )
    calls: list[tuple[str, int]] = []

    def http_post(payload: dict[str, Any], *, timeout_seconds: int) -> upstream_http._HttpResponse:
        calls.append((str(payload["method"]), timeout_seconds))
        if payload["method"] == "notifications/initialized":
            return upstream_http._HttpResponse(status=202, content_type="", body=b"")
        return upstream_http._HttpResponse(
            status=200,
            content_type="application/json",
            body=b'{"jsonrpc":"2.0","id":7,"result":{"ok":true}}',
        )

    monkeypatch.setattr(client, "_http_post", http_post)

    assert client._post_jsonrpc(
        {"jsonrpc": "2.0", "id": 7, "method": "tools/list"},
        timeout_seconds=41,
        expected_id=7,
    ) == {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}
    client._post_notification(
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        timeout_seconds=43,
    )
    assert calls == [("tools/list", 41), ("notifications/initialized", 43)]

@pytest.mark.error_simulation
def test_http_upstream_initialize_forwards_timeout_to_each_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.upstream_http import HttpUpstreamClient

    client = HttpUpstreamClient(
        UpstreamConfig(name="remote", command="https://example.invalid/mcp", transport="http"),
        environ={},
    )
    calls: list[tuple[str, int]] = []

    def post_jsonrpc(
        payload: dict[str, Any],
        *,
        timeout_seconds: int,
        expected_id: int,
    ) -> dict[str, Any]:
        calls.append((f"request:{payload['method']}:{expected_id}", timeout_seconds))
        return {"jsonrpc": "2.0", "id": expected_id, "result": {}}

    def post_notification(payload: dict[str, Any], *, timeout_seconds: int) -> None:
        calls.append((f"notification:{payload['method']}", timeout_seconds))

    monkeypatch.setattr(client, "_post_jsonrpc", post_jsonrpc)
    monkeypatch.setattr(client, "_post_notification", post_notification)

    client._initialize(timeout_seconds=47)

    assert calls == [
        ("request:initialize:0", 47),
        ("notification:notifications/initialized", 47),
    ]
