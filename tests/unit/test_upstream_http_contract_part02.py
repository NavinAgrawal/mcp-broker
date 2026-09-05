from __future__ import annotations
from email.message import Message
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from typing import Any
import urllib.request
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

def test_http_upstream_http_post_forwards_timeout_to_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.upstream_http import HttpUpstreamClient
    from mcp_broker import upstream_http

    client = HttpUpstreamClient(
        UpstreamConfig(name="remote", command="https://example.invalid/mcp", transport="http"),
        environ={},
    )
    calls: list[int] = []

    def http_post_once(
        payload: dict[str, Any],
        *,
        timeout_seconds: int,
    ) -> upstream_http._HttpResponse:
        assert payload == {"jsonrpc": "2.0", "id": 0, "method": "tools/list"}
        calls.append(timeout_seconds)
        return upstream_http._HttpResponse(status=200, content_type="application/json", body=b"{}")

    monkeypatch.setattr(client, "_http_post_once", http_post_once)

    client._http_post({"jsonrpc": "2.0", "id": 0, "method": "tools/list"}, timeout_seconds=59)

    assert calls == [59]

def test_http_post_once_builds_exact_request_and_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.protocol import SUPPORTED_PROTOCOL_VERSIONS
    from mcp_broker.upstream_http import HttpUpstreamClient

    headers = Message()
    headers["Content-Type"] = "application/json"
    headers["Mcp-Session-Id"] = "remote-session"
    captured: dict[str, Any] = {}

    class FakeResponse:
        status = 200

        def __init__(self) -> None:
            self.headers = headers

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *_exc_info: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"jsonrpc":"2.0","id":0,"result":{"ok":true}}'

    def fake_urlopen(request: urllib.request.Request, *, timeout: int) -> FakeResponse:
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["explicit_method"] = request.method
        captured["data"] = request.data
        captured["headers"] = {key.lower(): value for key, value in request.header_items()}
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    client = HttpUpstreamClient(
        UpstreamConfig(
            name="remote",
            command="https://example.invalid/mcp",
            transport="http",
            env={"REMOTE_TOKEN": "UNIT_REMOTE_TOKEN"},
        ),
        environ={"UNIT_REMOTE_TOKEN": "token-value"},
    )
    response = client._http_post_once(
        {"jsonrpc": "2.0", "id": 0, "method": "tools/list"},
        timeout_seconds=53,
    )

    assert captured == {
        "url": "https://example.invalid/mcp",
        "method": "POST",
        "explicit_method": "POST",
        "data": b'{"id": 0, "jsonrpc": "2.0", "method": "tools/list"}',
        "headers": {
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
            "mcp-protocol-version": SUPPORTED_PROTOCOL_VERSIONS[0],
            "authorization": "Bearer token-value",
        },
        "timeout": 53,
    }
    assert response.status == 200
    assert response.content_type == "application/json"
    assert response.body == b'{"jsonrpc":"2.0","id":0,"result":{"ok":true}}'
    assert client._session_id == "remote-session"

def test_http_post_once_defaults_missing_content_type_to_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.upstream_http import HttpUpstreamClient

    headers = Message()

    class FakeResponse:
        status = 200

        def __init__(self) -> None:
            self.headers = headers

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *_exc_info: object) -> None:
            return None

        def read(self) -> bytes:
            return b"{}"

    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: FakeResponse())

    client = HttpUpstreamClient(
        UpstreamConfig(name="remote", command="https://example.invalid/mcp", transport="http"),
        environ={},
    )

    response = client._http_post_once(
        {"jsonrpc": "2.0", "id": 0, "method": "tools/list"},
        timeout_seconds=1,
    )

    assert response.content_type == ""

def test_http_post_once_closes_http_error_and_reports_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker import upstream_http
    from mcp_broker.upstream_http import HttpUpstreamClient, HttpUpstreamError

    closed: list[bool] = []

    class ClosingHttpError(upstream_http.urllib.error.HTTPError):
        def close(self) -> None:
            closed.append(True)
            super().close()

    def fail_http(*_args: object, **_kwargs: object) -> None:
        raise ClosingHttpError(
            "https://example.invalid/mcp",
            503,
            "unavailable",
            {},
            None,
        )

    monkeypatch.setattr(upstream_http.urllib.request, "urlopen", fail_http)

    client = HttpUpstreamClient(
        UpstreamConfig(name="remote-repo", command="https://example.invalid/mcp", transport="http"),
        environ={},
    )

    with pytest.raises(
        HttpUpstreamError,
        match="upstream HTTP request failed: remote-repo: status 503",
    ):
        client._http_post_once(
            {"jsonrpc": "2.0", "id": 0, "method": "tools/list"},
            timeout_seconds=1,
        )

    assert closed == [True]

def test_http_upstream_rejects_invalid_tools_and_records_health_error() -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.upstream_http import HttpUpstreamClient, HttpUpstreamError

    with FakeMcpHttpServer(tools_result={"tools": {"bad": "shape"}}) as server:
        client = HttpUpstreamClient(
            UpstreamConfig(name="remote-repo", command=server.url, transport="http"),
            environ={},
        )

        with pytest.raises(HttpUpstreamError, match="upstream tools/list response invalid"):
            client.list_tools(timeout_seconds=2)

    assert client.health_snapshot() == {
        "state": "reachable",
        "pid": None,
        "cpu_percent": None,
        "memory_mb": None,
        "restarts": 0,
        "last_error": "upstream tools/list response invalid: remote-repo",
    }

def test_http_upstream_rejects_tools_list_with_non_object_items() -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.upstream_http import HttpUpstreamClient, HttpUpstreamError

    with FakeMcpHttpServer(tools_result={"tools": [{"name": "ok"}, "bad"]}) as server:
        client = HttpUpstreamClient(
            UpstreamConfig(name="remote-repo", command=server.url, transport="http"),
            environ={},
        )

        with pytest.raises(HttpUpstreamError, match="upstream tools/list response invalid"):
            client.list_tools(timeout_seconds=2)

    assert client.health_snapshot()["last_error"] == (
        "upstream tools/list response invalid: remote-repo"
    )

def test_http_upstream_call_records_error_health() -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.upstream_http import HttpUpstreamClient, HttpUpstreamError

    with FakeMcpHttpServer(call_error_message="bad call") as server:
        client = HttpUpstreamClient(
            UpstreamConfig(name="remote-repo", command=server.url, transport="http"),
            environ={},
        )

        with pytest.raises(HttpUpstreamError, match="bad call"):
            client.call_tool("search_repositories", {}, timeout_seconds=2)

    assert client.health_snapshot()["state"] == "reachable"
    assert client.health_snapshot()["last_error"] == "upstream returned error: remote-repo: bad call"

def test_http_upstream_rejects_notification_error_response() -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.upstream_http import HttpUpstreamClient, HttpUpstreamError

    with FakeMcpHttpServer(notification_error_message="init denied") as server:
        client = HttpUpstreamClient(
            UpstreamConfig(name="remote-repo", command=server.url, transport="http"),
            environ={},
        )

        with pytest.raises(HttpUpstreamError, match="upstream notification failed: remote-repo"):
            client.list_tools(timeout_seconds=2)

def test_http_upstream_notification_error_message_includes_upstream_message() -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.upstream_http import HttpUpstreamClient, HttpUpstreamError

    with FakeMcpHttpServer(notification_error_message="init denied") as server:
        client = HttpUpstreamClient(
            UpstreamConfig(name="remote-repo", command=server.url, transport="http"),
            environ={},
        )

        with pytest.raises(
            HttpUpstreamError,
            match="upstream notification failed: remote-repo: init denied",
        ):
            client.list_tools(timeout_seconds=2)

def test_http_upstream_rejects_notification_only_and_id_mismatch_responses() -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.upstream_http import HttpUpstreamClient, HttpUpstreamError

    with FakeMcpHttpServer(list_notification_only=True) as notification_server:
        notification_client = HttpUpstreamClient(
            UpstreamConfig(
                name="remote-repo",
                command=notification_server.url,
                transport="http",
            ),
            environ={},
        )

        with pytest.raises(HttpUpstreamError, match="upstream returned notification only"):
            notification_client.list_tools(timeout_seconds=2)

    with FakeMcpHttpServer(list_response_id="wrong") as mismatch_server:
        mismatch_client = HttpUpstreamClient(
            UpstreamConfig(name="remote-repo", command=mismatch_server.url, transport="http"),
            environ={},
        )

        with pytest.raises(HttpUpstreamError, match="expected 1, received id='wrong'"):
            mismatch_client.list_tools(timeout_seconds=2)

def test_http_upstream_auth_header_accepts_authorization_and_rejects_ambiguous_tokens() -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.upstream_http import HttpUpstreamClient, HttpUpstreamError

    direct = HttpUpstreamClient(
        UpstreamConfig(
            name="direct",
            command="https://example.invalid/mcp",
            transport="http",
            env={"AUTHORIZATION": "UNIT_AUTHORIZATION"},
        ),
        environ={"UNIT_AUTHORIZATION": "Bearer direct-token"},
    )
    ambiguous = HttpUpstreamClient(
        UpstreamConfig(
            name="ambiguous",
            command="https://example.invalid/mcp",
            transport="http",
            env={
                "FIRST_TOKEN": "UNIT_FIRST_TOKEN",
                "SECOND_TOKEN": "UNIT_SECOND_TOKEN",
            },
        ),
        environ={
            "UNIT_FIRST_TOKEN": "one",
            "UNIT_SECOND_TOKEN": "two",
        },
    )

    assert direct._headers()["Authorization"] == "Bearer direct-token"
    with pytest.raises(HttpUpstreamError, match="multiple bearer token env values"):
        ambiguous._headers()

def test_http_upstream_headers_are_exact_and_include_optional_auth_and_session() -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.protocol import SUPPORTED_PROTOCOL_VERSIONS
    from mcp_broker.upstream_http import (
        HttpUpstreamClient,
        MCP_PROTOCOL_VERSION_HEADER,
        MCP_SESSION_ID_HEADER,
    )

    client = HttpUpstreamClient(
        UpstreamConfig(
            name="remote",
            command="https://example.invalid/mcp",
            transport="http",
            env={"REMOTE_ACCESS_TOKEN": "UNIT_REMOTE_ACCESS_TOKEN"},
        ),
        environ={"UNIT_REMOTE_ACCESS_TOKEN": "access-token"},
    )

    assert client._headers() == {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        MCP_PROTOCOL_VERSION_HEADER: SUPPORTED_PROTOCOL_VERSIONS[0],
        "Authorization": "Bearer access-token",
    }

    client._session_id = "session-1"

    assert client._headers() == {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        MCP_PROTOCOL_VERSION_HEADER: SUPPORTED_PROTOCOL_VERSIONS[0],
        "Authorization": "Bearer access-token",
        MCP_SESSION_ID_HEADER: "session-1",
    }

def test_http_upstream_bearer_token_sources_are_exact() -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.upstream_http import HttpUpstreamClient

    bearer = HttpUpstreamClient(
        UpstreamConfig(
            name="remote",
            command="https://example.invalid/mcp",
            transport="http",
            env={"AUTHORIZATION": "UNIT_AUTHORIZATION"},
        ),
        environ={"UNIT_AUTHORIZATION": "Bearer direct-token"},
    )
    raw_authorization = HttpUpstreamClient(
        UpstreamConfig(
            name="remote",
            command="https://example.invalid/mcp",
            transport="http",
            env={"AUTHORIZATION": "UNIT_AUTHORIZATION"},
        ),
        environ={"UNIT_AUTHORIZATION": "raw-token"},
    )
    token = HttpUpstreamClient(
        UpstreamConfig(
            name="remote",
            command="https://example.invalid/mcp",
            transport="http",
            env={"REMOTE_TOKEN": "UNIT_REMOTE_TOKEN"},
        ),
        environ={"UNIT_REMOTE_TOKEN": "token-value"},
    )
    access_token = HttpUpstreamClient(
        UpstreamConfig(
            name="remote",
            command="https://example.invalid/mcp",
            transport="http",
            env={"REMOTE_ACCESS_TOKEN": "UNIT_REMOTE_ACCESS_TOKEN"},
        ),
        environ={"UNIT_REMOTE_ACCESS_TOKEN": "access-token-value"},
    )
    no_token = HttpUpstreamClient(
        UpstreamConfig(name="remote", command="https://example.invalid/mcp", transport="http"),
        environ={},
    )

    assert bearer._bearer_token() == "direct-token"
    assert raw_authorization._bearer_token() == "raw-token"
    assert token._bearer_token() == "token-value"
    assert access_token._bearer_token() == "access-token-value"
    assert no_token._bearer_token() is None

def test_http_upstream_jsonrpc_payload_contract() -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.upstream_http import HttpUpstreamClient

    client = HttpUpstreamClient(
        UpstreamConfig(name="remote", command="https://example.invalid/mcp", transport="http"),
        environ={},
    )

    first_id, first_payload = client._jsonrpc_payload("tools/list", None)
    second_id, second_payload = client._jsonrpc_payload("tools/call", {"name": "echo"})
    third_id, third_payload = client._jsonrpc_payload("tools/list", None)

    assert first_id == 0
    assert first_payload == {"jsonrpc": "2.0", "id": 0, "method": "tools/list"}
    assert second_id == 1
    assert second_payload == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "echo"},
    }
    assert third_id == 2
    assert third_payload == {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}

def test_http_upstream_result_validation_rejects_missing_result_and_id_mismatch() -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.upstream_http import HttpUpstreamClient, HttpUpstreamError

    client = HttpUpstreamClient(
        UpstreamConfig(name="remote-repo", command="https://example.invalid/mcp", transport="http"),
        environ={},
    )

    with pytest.raises(HttpUpstreamError, match="upstream response missing result"):
        client._result_from_response({"jsonrpc": "2.0", "id": 0, "result": []}, 0)
    with pytest.raises(
        HttpUpstreamError,
        match="expected 0, received id='wrong', method='tools/list'",
    ):
        client._result_from_response(
            {
                "jsonrpc": "2.0",
                "id": "wrong",
                "method": "tools/list",
                "result": {},
            },
            0,
        )

@pytest.mark.error_simulation
def test_http_upstream_maps_urlopen_timeout_and_url_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker import upstream_http
    from mcp_broker.upstream_http import HttpUpstreamClient, HttpUpstreamError, HttpUpstreamTimeout

    client = HttpUpstreamClient(
        UpstreamConfig(name="remote-repo", command="https://example.invalid/mcp", transport="http"),
        environ={},
    )

    for raised in (
        TimeoutError("slow"),
        upstream_http.urllib.error.URLError(TimeoutError("slow")),
    ):
        monkeypatch.setattr(
            upstream_http.urllib.request,
            "urlopen",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(raised),
        )
        with pytest.raises(HttpUpstreamTimeout, match="upstream timed out: remote-repo"):
            client._http_post({"jsonrpc": "2.0", "id": 0, "method": "tools/list"}, timeout_seconds=1)

    monkeypatch.setattr(
        upstream_http.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            upstream_http.urllib.error.URLError("dns failed")
        ),
    )
    with pytest.raises(HttpUpstreamError, match="dns failed"):
        client._http_post({"jsonrpc": "2.0", "id": 0, "method": "tools/list"}, timeout_seconds=1)
