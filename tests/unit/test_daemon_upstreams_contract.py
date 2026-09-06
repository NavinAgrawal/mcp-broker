from __future__ import annotations

from pathlib import Path

import pytest

from mcp_broker.broker import BrokerToolError
from mcp_broker.daemon_upstreams import _broker_error, _broker_timeout
from mcp_broker.schema import AuthRepairPolicy
from mcp_broker.upstream_http import HttpUpstreamError, HttpUpstreamTimeout
from mcp_broker.upstream_stdio import StdioUpstreamError, StdioUpstreamTimeout
from tests.support.daemon_upstreams import FakeHttpClient, FakeStdioClient, UpstreamHarness, _error_result, _upstream


pytestmark = pytest.mark.unit


def test_call_upstream_routes_http_without_session_context(tmp_path: Path) -> None:
    harness = UpstreamHarness(tmp_path, {"remote": _upstream("remote", transport="http")})
    http_client = FakeHttpClient(call_result={"content": [{"text": "remote"}]})
    harness.http_clients_to_create.append(http_client)

    result = harness._call_upstream(
        "remote",
        "search",
        {"query": "mcp"},
        11,
        session_id="session-1",
        session_context={"client_cwd": "/tmp/project"},
    )

    assert result == {"content": [{"text": "remote"}]}
    assert http_client.calls == [("search", {"query": "mcp"}, 11)]
    assert harness.stdio_creates == []
    assert harness.events == [
        (
            "upstream.call",
            "remote",
            {"method": "tools/call", "tool_name": "search", "timeout_seconds": 11},
        )
    ]


def test_call_upstream_routes_sse_as_http(tmp_path: Path) -> None:
    harness = UpstreamHarness(tmp_path, {"events": _upstream("events", transport="sse")})
    http_client = FakeHttpClient(call_result={"content": [{"text": "sse"}]})
    harness.http_clients_to_create.append(http_client)

    result = harness._call_upstream("events", "tail", {}, 7)

    assert result == {"content": [{"text": "sse"}]}
    assert harness.http_creates == ["events"]
    assert http_client.calls == [("tail", {}, 7)]


def test_call_upstream_routes_stdio_with_session_context(tmp_path: Path) -> None:
    harness = UpstreamHarness(tmp_path, {"local": _upstream("local", mode="per_session")})
    stdio_client = FakeStdioClient(call_result={"content": [{"text": "local"}]})
    harness.stdio_clients_to_create.append(stdio_client)
    session_context = {"client_cwd": "/tmp/project"}

    result = harness._call_upstream(
        "local",
        "read",
        {"path": "README.md"},
        9,
        session_id="session-7",
        session_context=session_context,
    )

    assert result == {"content": [{"text": "local"}]}
    assert stdio_client.calls == [("read", {"path": "README.md"}, 9)]
    assert harness.stdio_creates[0]["upstream"] == "local"
    assert harness.stdio_creates[0]["runtime_state_dir"] == tmp_path / "state"
    assert harness.stdio_creates[0]["session_context"] is session_context
    assert harness.stdio_creates[0]["event_logger"] == harness._write_upstream_event
    assert harness.stdio_creates[0]["runtime_paths"] == harness._paths


def test_stdio_client_reuses_shared_process_by_upstream_name(tmp_path: Path) -> None:
    harness = UpstreamHarness(tmp_path, {"local": _upstream("local")})
    created = FakeStdioClient(call_result={"content": []})
    harness.stdio_clients_to_create.append(created)

    first = harness._stdio_client("local", session_id=None)
    second = harness._stdio_client("local", session_id="ignored-for-shared")

    assert first is created
    assert second is created
    assert list(harness._stdio_upstreams) == ["local"]
    assert len(harness.stdio_creates) == 1


def test_stdio_client_uses_session_id_in_per_session_cache(tmp_path: Path) -> None:
    harness = UpstreamHarness(tmp_path, {"local": _upstream("local", mode="per_session")})
    first_client = FakeStdioClient(call_result={"content": []})
    second_client = FakeStdioClient(call_result={"content": []})
    harness.stdio_clients_to_create.extend([first_client, second_client])

    first = harness._stdio_client("local", session_id="session-a")
    again = harness._stdio_client("local", session_id="session-a")
    second = harness._stdio_client("local", session_id="session-b")

    assert first is first_client
    assert again is first_client
    assert second is second_client
    assert set(harness._stdio_upstreams) == {("local", "session-a"), ("local", "session-b")}
    assert [create["session_context"] for create in harness.stdio_creates] == [None, None]


def test_stdio_client_requires_session_id_for_per_session_upstream(tmp_path: Path) -> None:
    harness = UpstreamHarness(tmp_path, {"local": _upstream("local", mode="per_session")})

    with pytest.raises(BrokerToolError) as exc:
        harness._stdio_client("local", session_id=None)

    assert exc.value.code == "missing_session"
    assert exc.value.message == "broker_session_id is required for per_session upstream: local"
    assert exc.value.upstream_name == "local"
    assert harness.stdio_creates == []


def test_stdio_client_validates_configured_session_environment(tmp_path: Path) -> None:
    harness = UpstreamHarness(
        tmp_path,
        {
            "local": _upstream(
                "local",
                mode="per_session",
                session_env={"PROJECT_CWD": "client_cwd"},
            )
        },
    )
    harness.stdio_clients_to_create.append(FakeStdioClient(call_result={"content": []}))

    with pytest.raises(ValueError, match="missing session context for upstream local: client_cwd"):
        harness._stdio_client("local", session_id="session-1", session_context={})

    result = harness._stdio_client(
        "local",
        session_id="session-1",
        session_context={"client_cwd": "/tmp/project"},
    )

    assert isinstance(result, FakeStdioClient)
    assert harness.stdio_creates[0]["session_context"] == {"client_cwd": "/tmp/project"}


def test_list_stdio_upstream_passes_timeout_and_session_context(tmp_path: Path) -> None:
    harness = UpstreamHarness(tmp_path, {"local": _upstream("local", mode="per_session")})
    stdio_client = FakeStdioClient(list_result=[{"name": "read"}])
    harness.stdio_clients_to_create.append(stdio_client)
    session_context = {"client_cwd": "/tmp/project"}

    result = harness._list_stdio_upstream(
        "local",
        13,
        session_id="session-4",
        session_context=session_context,
    )

    assert result == [{"name": "read"}]
    assert stdio_client.lists == [13]
    assert harness.stdio_creates[0]["session_context"] is session_context


def test_call_stdio_upstream_maps_stdio_timeout(tmp_path: Path) -> None:
    harness = UpstreamHarness(tmp_path, {"local": _upstream("local")})
    harness.stdio_clients_to_create.append(
        FakeStdioClient(call_exception=StdioUpstreamTimeout("too slow"))
    )

    with pytest.raises(BrokerToolError) as exc:
        harness._call_stdio_upstream("local", "read", {"path": "x"}, 3)

    assert exc.value.code == "upstream_timeout"
    assert exc.value.message == "upstream timed out: local"
    assert exc.value.upstream_name == "local"
    assert exc.value.tool_name == "read"


def test_call_stdio_upstream_maps_stdio_error_message(tmp_path: Path) -> None:
    harness = UpstreamHarness(tmp_path, {"local": _upstream("local")})
    harness.stdio_clients_to_create.append(FakeStdioClient(call_exception=StdioUpstreamError("bad")))

    with pytest.raises(BrokerToolError) as exc:
        harness._call_stdio_upstream("local", "read", {}, 3)

    assert exc.value.code == "upstream_error"
    assert exc.value.message == "bad"
    assert exc.value.upstream_name == "local"
    assert exc.value.tool_name == "read"


def test_call_stdio_upstream_runs_auth_repair_and_retries_original(tmp_path: Path) -> None:
    harness = UpstreamHarness(
        tmp_path,
        {
            "local": _upstream(
                "local",
                auth_repair=AuthRepairPolicy(
                    tool="auth.refresh",
                    arguments={"force": True},
                    trigger_errors=("expired",),
                    retry_original=True,
                    timeout_seconds=17,
                ),
            )
        },
    )
    client = FakeStdioClient(
        call_results=[
            _error_result("token expired"),
            {"content": [{"text": "repair ok"}]},
            {"content": [{"text": "original ok"}]},
        ]
    )
    harness.stdio_clients_to_create.append(client)

    result = harness._call_stdio_upstream("local", "read", {"path": "x"}, 5)

    assert result == {"content": [{"text": "original ok"}]}
    assert client.calls == [
        ("read", {"path": "x"}, 5),
        ("auth.refresh", {"force": True}, 17),
        ("read", {"path": "x"}, 5),
    ]
    assert harness.auth_repair_events == [("attempt", "local"), ("success", "local")]


def test_call_stdio_upstream_returns_repair_result_without_retry_original(tmp_path: Path) -> None:
    harness = UpstreamHarness(
        tmp_path,
        {
            "local": _upstream(
                "local",
                auth_repair=AuthRepairPolicy(
                    tool="auth.refresh",
                    arguments=None,
                    trigger_errors=("expired",),
                    retry_original=False,
                    timeout_seconds=19,
                ),
            )
        },
    )
    client = FakeStdioClient(
        call_results=[
            _error_result("token expired"),
            {"content": [{"text": "repair ok"}]},
        ]
    )
    harness.stdio_clients_to_create.append(client)

    result = harness._call_stdio_upstream("local", "read", {}, 5)

    assert result == {"content": [{"text": "repair ok"}]}
    assert client.calls == [
        ("read", {}, 5),
        ("auth.refresh", {}, 19),
    ]
    assert harness.auth_repair_events == [("attempt", "local"), ("success", "local")]


def test_call_stdio_upstream_records_failed_auth_repair(tmp_path: Path) -> None:
    harness = UpstreamHarness(
        tmp_path,
        {
            "local": _upstream(
                "local",
                auth_repair=AuthRepairPolicy(
                    tool="auth.refresh",
                    trigger_errors=("expired",),
                    retry_original=True,
                ),
            )
        },
    )
    client = FakeStdioClient(
        call_results=[
            _error_result("token expired"),
            {"content": [{"text": "repair ok"}]},
            _error_result("token expired"),
        ]
    )
    harness.stdio_clients_to_create.append(client)

    result = harness._call_stdio_upstream("local", "read", {}, 5)

    assert result == _error_result("token expired")
    assert harness.auth_repair_events == [("attempt", "local"), ("failure", "local")]


def test_call_http_upstream_logs_call_and_timeout(tmp_path: Path) -> None:
    harness = UpstreamHarness(tmp_path, {"remote": _upstream("remote", transport="http")})
    harness.http_clients_to_create.append(FakeHttpClient(call_exception=HttpUpstreamTimeout("slow")))

    with pytest.raises(BrokerToolError) as exc:
        harness._call_http_upstream("remote", "search", {"q": "x"}, 23)

    assert exc.value.code == "upstream_timeout"
    assert exc.value.message == "upstream timed out: remote"
    assert exc.value.upstream_name == "remote"
    assert exc.value.tool_name == "search"
    assert harness.events == [
        (
            "upstream.call",
            "remote",
            {"method": "tools/call", "tool_name": "search", "timeout_seconds": 23},
        ),
        (
            "upstream.timeout",
            "remote",
            {"method": "tools/call", "tool_name": "search", "timeout_seconds": 23},
        ),
    ]


def test_call_http_upstream_maps_http_error_message(tmp_path: Path) -> None:
    harness = UpstreamHarness(tmp_path, {"remote": _upstream("remote", transport="http")})
    harness.http_clients_to_create.append(FakeHttpClient(call_exception=HttpUpstreamError("bad")))

    with pytest.raises(BrokerToolError) as exc:
        harness._call_http_upstream("remote", "search", {}, 23)

    assert exc.value.code == "upstream_error"
    assert exc.value.message == "bad"
    assert exc.value.upstream_name == "remote"
    assert exc.value.tool_name == "search"
    assert harness.events == [
        (
            "upstream.call",
            "remote",
            {"method": "tools/call", "tool_name": "search", "timeout_seconds": 23},
        )
    ]


def test_list_upstream_logs_http_tools_list_and_uses_cached_client(tmp_path: Path) -> None:
    harness = UpstreamHarness(tmp_path, {"remote": _upstream("remote", transport="http")})
    http_client = FakeHttpClient(list_result=[{"name": "search"}])
    harness.http_clients_to_create.append(http_client)

    first = harness._list_upstream("remote", 31)
    second = harness._list_upstream("remote", 37)

    assert first == [{"name": "search"}]
    assert second == [{"name": "search"}]
    assert http_client.lists == [31, 37]
    assert harness.http_creates == ["remote"]
    assert harness.events == [
        ("upstream.call", "remote", {"method": "tools/list", "timeout_seconds": 31}),
        ("upstream.call", "remote", {"method": "tools/list", "timeout_seconds": 37}),
    ]


def test_list_upstream_routes_sse_tools_list_to_http_client(tmp_path: Path) -> None:
    harness = UpstreamHarness(tmp_path, {"events": _upstream("events", transport="sse")})
    http_client = FakeHttpClient(list_result=[{"name": "tail"}])
    harness.http_clients_to_create.append(http_client)

    result = harness._list_upstream(
        "events",
        39,
        session_id="ignored-session",
        session_context={"client_cwd": "/tmp/project"},
    )

    assert result == [{"name": "tail"}]
    assert http_client.lists == [39]
    assert harness.http_creates == ["events"]
    assert harness.stdio_creates == []
    assert harness.events == [
        ("upstream.call", "events", {"method": "tools/list", "timeout_seconds": 39})
    ]


def test_list_upstream_delegates_stdio_session_context(tmp_path: Path) -> None:
    harness = UpstreamHarness(tmp_path, {"local": _upstream("local", mode="per_session")})
    harness.stdio_clients_to_create.append(FakeStdioClient(list_result=[{"name": "read"}]))
    session_context = {"client_cwd": "/tmp/project"}

    result = harness._list_upstream(
        "local",
        29,
        session_id="session-3",
        session_context=session_context,
    )

    assert result == [{"name": "read"}]
    assert harness.stdio_creates[0]["session_context"] is session_context


def test_http_client_reuses_client_by_upstream_name(tmp_path: Path) -> None:
    harness = UpstreamHarness(tmp_path, {"remote": _upstream("remote", transport="http")})
    created = FakeHttpClient()
    harness.http_clients_to_create.append(created)

    first = harness._http_client("remote")
    second = harness._http_client("remote")

    assert first is created
    assert second is created
    assert harness.http_creates == ["remote"]
    assert list(harness._http_upstreams) == ["remote"]


def test_session_bound_call_and_list_wrappers_forward_session_context(tmp_path: Path) -> None:
    harness = UpstreamHarness(tmp_path, {"local": _upstream("local", mode="per_session")})
    client = FakeStdioClient(
        call_result={"content": [{"text": "ok"}]},
        list_result=[{"name": "read"}],
    )
    harness.stdio_clients_to_create.append(client)
    session_context = {"client_cwd": "/tmp/project"}

    call_for_session = harness._call_upstream_for_session("session-2", session_context)
    list_for_session = harness._list_upstream_for_session("session-2", session_context)

    assert call_for_session("local", "read", {"path": "x"}, 41) == {
        "content": [{"text": "ok"}]
    }
    assert list_for_session("local", 43) == [{"name": "read"}]
    assert client.calls == [("read", {"path": "x"}, 41)]
    assert client.lists == [43]
    assert len(harness.stdio_creates) == 1
    assert harness.stdio_creates[0]["session_context"] is session_context


def test_session_bound_list_wrapper_uses_session_context_for_new_stdio_client(
    tmp_path: Path,
) -> None:
    harness = UpstreamHarness(
        tmp_path,
        {
            "local": _upstream(
                "local",
                mode="per_session",
                session_env={"PROJECT_CWD": "client_cwd"},
            )
        },
    )
    harness.stdio_clients_to_create.append(FakeStdioClient(list_result=[{"name": "read"}]))
    session_context = {"client_cwd": "/tmp/project"}

    list_for_session = harness._list_upstream_for_session("session-9", session_context)

    assert list_for_session("local", 59) == [{"name": "read"}]
    assert harness.stdio_creates[0]["session_context"] is session_context


def test_log_http_helpers_write_exact_event_payloads(tmp_path: Path) -> None:
    harness = UpstreamHarness(tmp_path, {"remote": _upstream("remote", transport="http")})

    harness._log_http_call("remote", "search", 47)
    harness._log_http_timeout("remote", "search", 53)

    assert harness.events == [
        (
            "upstream.call",
            "remote",
            {"method": "tools/call", "tool_name": "search", "timeout_seconds": 47},
        ),
        (
            "upstream.timeout",
            "remote",
            {"method": "tools/call", "tool_name": "search", "timeout_seconds": 53},
        ),
    ]


def test_broker_error_helpers_return_exact_tool_error() -> None:
    timeout = _broker_timeout("remote", "search")
    error = _broker_error("local", "read", "boom")

    assert timeout.code == "upstream_timeout"
    assert timeout.message == "upstream timed out: remote"
    assert timeout.upstream_name == "remote"
    assert timeout.tool_name == "search"
    assert error.code == "upstream_error"
    assert error.message == "boom"
    assert error.upstream_name == "local"
    assert error.tool_name == "read"
