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

def test_http_response_parsing_rejects_invalid_bodies() -> None:
    from mcp_broker.upstream_http import (
        _HttpResponse,
        _parse_http_response_body,
        _parse_sse_response,
        HttpUpstreamError,
    )

    with pytest.raises(HttpUpstreamError, match="missing result"):
        _parse_http_response_body(
            _HttpResponse(status=202, content_type="application/json", body=b""),
            "remote-repo",
        )
    with pytest.raises(HttpUpstreamError, match="missing body"):
        _parse_http_response_body(
            _HttpResponse(status=200, content_type="application/json", body=b""),
            "remote-repo",
        )
    with pytest.raises(HttpUpstreamError, match="must be an object"):
        _parse_http_response_body(
            _HttpResponse(status=200, content_type="application/json", body=b"[]"),
            "remote-repo",
        )
    with pytest.raises(HttpUpstreamError, match="must be an object"):
        _parse_sse_response(b"data: []\n\n", "remote-repo")
    with pytest.raises(HttpUpstreamError, match="missing data"):
        _parse_sse_response(b"event: ping\n\n", "remote-repo")
    with pytest.raises(HttpUpstreamError, match="missing data"):
        _parse_sse_response(b"data: []", "remote-repo")
    assert _parse_sse_response(
        b"data: {\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{}}",
        "remote-repo",
    ) == {"jsonrpc": "2.0", "id": 1, "result": {}}

def test_http_response_parsing_routes_sse_and_preserves_upstream_name() -> None:
    from mcp_broker.upstream_http import (
        _HttpResponse,
        _parse_http_response_body,
        HttpUpstreamError,
    )

    parsed = _parse_http_response_body(
        _HttpResponse(
            status=200,
            content_type="text/event-stream; charset=utf-8",
            body=b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n',
        ),
        "remote-repo",
    )

    assert parsed == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}

    with pytest.raises(HttpUpstreamError, match="remote-repo"):
        _parse_http_response_body(
            _HttpResponse(
                status=200,
                content_type="text/event-stream",
                body=b"data: []\n\n",
            ),
            "remote-repo",
        )

def test_http_response_parsing_handles_multiline_sse_data_before_blank_line() -> None:
    from mcp_broker.upstream_http import _parse_sse_response

    parsed = _parse_sse_response(
        b'data: {"jsonrpc":"2.0",\n'
        b'data: "id":1,\n'
        b'data: "result":{"ok":true}}\n\n'
        b'event: ignored\n'
        b'data: {"jsonrpc":"2.0","id":2,"result":{"ignored":true}}\n\n',
        "remote-repo",
    )

    assert parsed == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}

def test_http_response_parsing_handles_final_multiline_sse_data() -> None:
    from mcp_broker.upstream_http import _parse_sse_response

    parsed = _parse_sse_response(
        b'data: {"jsonrpc":"2.0",\n'
        b'data: "id":1,\n'
        b'data: "result":{"ok":true}}',
        "remote-repo",
    )

    assert parsed == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}

def test_jsonrpc_notification_detection_requires_missing_id_and_string_method() -> None:
    from mcp_broker.upstream_http import _is_jsonrpc_notification

    assert _is_jsonrpc_notification({"jsonrpc": "2.0", "method": "notifications/progress"})
    assert not _is_jsonrpc_notification(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    assert not _is_jsonrpc_notification({"jsonrpc": "2.0", "method": 42})
    assert not _is_jsonrpc_notification({"jsonrpc": "2.0", "id": 1})

def test_retryable_http_error_detection_matches_only_retryable_statuses() -> None:
    from mcp_broker.upstream_http import HttpUpstreamError, _is_retryable_http_error

    assert _is_retryable_http_error(HttpUpstreamError("upstream failed: status 429"))
    assert _is_retryable_http_error(HttpUpstreamError("upstream failed: status 503"))
    assert not _is_retryable_http_error(HttpUpstreamError("upstream failed: status 400"))
    assert not _is_retryable_http_error(HttpUpstreamError("upstream failed: status 404"))
    assert not _is_retryable_http_error(HttpUpstreamError("upstream failed without status"))

@pytest.mark.error_simulation
def test_http_notification_accepts_non_error_jsonrpc_body(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker import upstream_http
    from mcp_broker.upstream_http import HttpUpstreamClient

    client = HttpUpstreamClient(
        UpstreamConfig(
            name="remote-repo",
            command="http://127.0.0.1:1/mcp",
            transport="http",
            tool_prefix="remote-repo",
        ),
        environ={},
    )
    monkeypatch.setattr(
        client,
        "_http_post",
        lambda *_args, **_kwargs: upstream_http._HttpResponse(
            status=200,
            content_type="application/json",
            body=b'{"jsonrpc":"2.0","result":{}}',
        ),
    )

    client._post_notification(
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        timeout_seconds=1,
    )

@pytest.mark.error_simulation
@pytest.mark.parametrize("status", [202, 203])
def test_http_notification_rejects_error_body_even_for_accepted_statuses(
    status: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker import upstream_http
    from mcp_broker.upstream_http import HttpUpstreamClient, HttpUpstreamError

    client = HttpUpstreamClient(
        UpstreamConfig(
            name="remote-repo",
            command="http://127.0.0.1:1/mcp",
            transport="http",
            tool_prefix="remote-repo",
        ),
        environ={},
    )
    monkeypatch.setattr(
        client,
        "_http_post",
        lambda *_args, **_kwargs: upstream_http._HttpResponse(
            status=status,
            content_type="application/json",
            body=b'{"jsonrpc":"2.0","error":{"message":"init denied"}}',
        ),
    )

    with pytest.raises(
        HttpUpstreamError,
        match="upstream notification failed: remote-repo: init denied",
    ):
        client._post_notification(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            timeout_seconds=1,
        )

@pytest.mark.error_simulation
def test_http_notification_parse_errors_preserve_upstream_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker import upstream_http
    from mcp_broker.upstream_http import HttpUpstreamClient, HttpUpstreamError

    client = HttpUpstreamClient(
        UpstreamConfig(
            name="remote-repo",
            command="http://127.0.0.1:1/mcp",
            transport="http",
            tool_prefix="remote-repo",
        ),
        environ={},
    )
    monkeypatch.setattr(
        client,
        "_http_post",
        lambda *_args, **_kwargs: upstream_http._HttpResponse(
            status=200,
            content_type="application/json",
            body=b"[]",
        ),
    )

    with pytest.raises(HttpUpstreamError, match="upstream response must be an object: remote-repo"):
        client._post_notification(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            timeout_seconds=1,
        )

@pytest.mark.error_simulation
def test_http_jsonrpc_parse_errors_preserve_upstream_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker import upstream_http
    from mcp_broker.upstream_http import HttpUpstreamClient, HttpUpstreamError

    client = HttpUpstreamClient(
        UpstreamConfig(name="remote-repo", command="http://127.0.0.1:1/mcp", transport="http"),
        environ={},
    )
    monkeypatch.setattr(
        client,
        "_http_post",
        lambda *_args, **_kwargs: upstream_http._HttpResponse(
            status=200,
            content_type="application/json",
            body=b"[]",
        ),
    )

    with pytest.raises(HttpUpstreamError, match="upstream response must be an object: remote-repo"):
        client._post_jsonrpc(
            {"jsonrpc": "2.0", "id": 7, "method": "tools/list"},
            timeout_seconds=1,
            expected_id=7,
        )
