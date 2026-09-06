from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable

import pytest

from mcp_broker.broker import BrokerToolError
from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig
from mcp_broker.daemon_upstreams import (
    BrokerDaemonUpstreamMixin,
    _broker_error,
    _broker_timeout,
)
from mcp_broker.runtime_reaper import RuntimePaths
from mcp_broker.schema import AuthRepairPolicy
from mcp_broker.upstream_http import HttpUpstreamError, HttpUpstreamTimeout
from mcp_broker.upstream_protocols import (
    HttpUpstreamClientProtocol,
    StdioUpstreamClientProtocol,
)
from mcp_broker.upstream_stdio import StdioUpstreamError, StdioUpstreamTimeout


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


def test_per_call_upstream_creates_stops_and_does_not_cache_client(tmp_path: Path) -> None:
    harness = UpstreamHarness(tmp_path, {"task": _upstream("task", mode="per_call")})
    first = FakeStdioClient(call_result={"content": [{"text": "one"}]})
    second = FakeStdioClient(call_result={"content": [{"text": "two"}]})
    harness.stdio_clients_to_create.extend([first, second])

    assert harness._call_stdio_upstream("task", "run", {"value": "one"}, 9) == {
        "content": [{"text": "one"}]
    }
    assert harness._call_stdio_upstream("task", "run", {"value": "two"}, 11) == {
        "content": [{"text": "two"}]
    }

    assert first.calls == [("run", {"value": "one"}, 9)]
    assert second.calls == [("run", {"value": "two"}, 11)]
    assert first.stop_calls == 1
    assert second.stop_calls == 1
    assert harness._stdio_upstreams == {}
    for create in harness.stdio_creates:
        assert create["runtime_state_dir"] == tmp_path / "state"
        assert create["session_context"] is None
        assert create["event_logger"] == harness._write_upstream_event
        assert create["runtime_paths"] == harness._paths
    metadata_names = [create["process_metadata_name"] for create in harness.stdio_creates]
    assert len(set(metadata_names)) == 2
    assert all(str(name).startswith("task.call.") for name in metadata_names)


def test_per_call_upstream_is_registered_while_call_is_active(tmp_path: Path) -> None:
    harness = UpstreamHarness(tmp_path, {"task": _upstream("task", mode="per_call")})
    active_snapshots: list[dict[str, tuple[str, FakeStdioClient]]] = []
    client = FakeStdioClient(
        call_result={"content": [{"text": "done"}]},
        on_call=lambda: active_snapshots.append(dict(harness._active_per_call_upstreams)),
    )
    harness.stdio_clients_to_create.append(client)

    result = harness._call_stdio_upstream("task", "run", {}, 9)

    assert result == {"content": [{"text": "done"}]}
    assert len(active_snapshots) == 1
    assert len(active_snapshots[0]) == 1
    assert next(iter(active_snapshots[0].values())) == ("task", client)
    assert harness._active_per_call_upstreams == {}


def test_per_call_creation_and_registration_hold_registry_lock(tmp_path: Path) -> None:
    harness = UpstreamHarness(tmp_path, {"task": _upstream("task", mode="per_call")})
    client = FakeStdioClient(call_result={"content": [{"text": "done"}]})
    harness.stdio_clients_to_create.append(client)

    harness._call_stdio_upstream("task", "run", {}, 9)

    assert harness.create_lock_states == [True]


def test_per_call_upstream_stops_after_call_error(tmp_path: Path) -> None:
    harness = UpstreamHarness(tmp_path, {"task": _upstream("task", mode="per_call")})
    client = FakeStdioClient(call_exception=StdioUpstreamError("failed"))
    harness.stdio_clients_to_create.append(client)

    with pytest.raises(BrokerToolError, match="failed"):
        harness._call_stdio_upstream("task", "run", {}, 9)

    assert client.stop_calls == 1
    assert harness._stdio_upstreams == {}


def test_per_call_cleanup_error_does_not_replace_original_call_error(tmp_path: Path) -> None:
    harness = UpstreamHarness(tmp_path, {"task": _upstream("task", mode="per_call")})
    client = FakeStdioClient(
        call_exception=StdioUpstreamError("call failed"),
        stop_exception=RuntimeError("cleanup failed"),
    )
    harness.stdio_clients_to_create.append(client)

    with pytest.raises(BrokerToolError, match="call failed"):
        harness._call_stdio_upstream("task", "run", {}, 9)

    assert client.stop_calls == 1
    assert list(harness._active_per_call_upstreams.values()) == [("task", client)]
    assert harness.events[-1] == (
        "upstream.stop_error",
        "task",
        {"error": "cleanup failed"},
    )


def test_per_call_cleanup_logging_error_does_not_replace_original_call_error(
    tmp_path: Path,
) -> None:
    harness = UpstreamHarness(tmp_path, {"task": _upstream("task", mode="per_call")})
    client = FakeStdioClient(
        call_exception=StdioUpstreamError("call failed"),
        stop_exception=RuntimeError("cleanup failed"),
    )
    harness.stdio_clients_to_create.append(client)
    harness.event_exception = RuntimeError("event log failed")

    with pytest.raises(BrokerToolError) as exc:
        harness._call_stdio_upstream("task", "run", {}, 9)

    assert exc.value.message == "call failed"


def test_per_call_cleanup_error_after_success_is_broker_error_and_retains_owner(
    tmp_path: Path,
) -> None:
    harness = UpstreamHarness(tmp_path, {"task": _upstream("task", mode="per_call")})
    client = FakeStdioClient(
        call_result={"content": [{"text": "done"}]},
        stop_exception=RuntimeError("cleanup failed"),
    )
    harness.stdio_clients_to_create.append(client)

    with pytest.raises(BrokerToolError, match="cleanup failed"):
        harness._call_stdio_upstream("task", "run", {}, 9)

    assert list(harness._active_per_call_upstreams.values()) == [("task", client)]


def test_per_call_cleanup_logging_error_does_not_replace_cleanup_error(
    tmp_path: Path,
) -> None:
    harness = UpstreamHarness(tmp_path, {"task": _upstream("task", mode="per_call")})
    client = FakeStdioClient(
        call_result={"content": [{"text": "done"}]},
        stop_exception=RuntimeError("cleanup failed"),
    )
    harness.stdio_clients_to_create.append(client)
    harness.event_exception = RuntimeError("event log failed")

    with pytest.raises(BrokerToolError) as exc:
        harness._call_stdio_upstream("task", "run", {}, 9)

    assert exc.value.message == "upstream cleanup failed: task: cleanup failed"


def test_per_call_cleanup_reports_surviving_processes(tmp_path: Path) -> None:
    harness = UpstreamHarness(tmp_path, {"task": _upstream("task", mode="per_call")})
    client = FakeStdioClient(
        call_result={"content": [{"text": "done"}]},
        stop_result=(777, 888),
    )
    harness.stdio_clients_to_create.append(client)

    result = harness._call_stdio_upstream("task", "run", {}, 9)

    assert result == {"content": [{"text": "done"}]}
    assert harness._active_per_call_upstreams == {}
    assert harness.events[-1] == (
        "upstream.stop_incomplete",
        "task",
        {"remaining_broker_processes": [777, 888]},
    )


def test_per_call_survivor_logging_error_does_not_replace_original_call_error(
    tmp_path: Path,
) -> None:
    harness = UpstreamHarness(tmp_path, {"task": _upstream("task", mode="per_call")})
    client = FakeStdioClient(
        call_exception=StdioUpstreamError("call failed"),
        stop_result=(777,),
    )
    harness.stdio_clients_to_create.append(client)
    harness.event_exception = RuntimeError("event log failed")

    with pytest.raises(BrokerToolError) as exc:
        harness._call_stdio_upstream("task", "run", {}, 9)

    assert exc.value.message == "call failed"


def test_per_call_survivor_logging_error_does_not_replace_successful_result(
    tmp_path: Path,
) -> None:
    harness = UpstreamHarness(tmp_path, {"task": _upstream("task", mode="per_call")})
    client = FakeStdioClient(
        call_result={"content": [{"text": "done"}]},
        stop_result=(777,),
    )
    harness.stdio_clients_to_create.append(client)
    harness.event_exception = RuntimeError("event log failed")

    result = harness._call_stdio_upstream("task", "run", {}, 9)

    assert result == {"content": [{"text": "done"}]}


def test_per_call_upstream_stops_after_tool_listing(tmp_path: Path) -> None:
    harness = UpstreamHarness(tmp_path, {"task": _upstream("task", mode="per_call")})
    client = FakeStdioClient(list_result=[{"name": "run"}])
    harness.stdio_clients_to_create.append(client)

    session_context = {"client_cwd": "/tmp/project"}
    result = harness._list_stdio_upstream(
        "task",
        13,
        session_context=session_context,
    )

    assert result == [{"name": "run"}]
    assert client.lists == [13]
    assert client.stop_calls == 1
    assert harness._stdio_upstreams == {}
    assert harness.stdio_creates[0]["session_context"] is session_context


def test_per_call_upstream_stops_after_tool_listing_error(tmp_path: Path) -> None:
    harness = UpstreamHarness(tmp_path, {"task": _upstream("task", mode="per_call")})
    client = FakeStdioClient(list_exception=StdioUpstreamError("list failed"))
    harness.stdio_clients_to_create.append(client)

    with pytest.raises(StdioUpstreamError, match="list failed"):
        harness._list_stdio_upstream("task", 13)

    assert client.stop_calls == 1
    assert harness._active_per_call_upstreams == {}


def test_per_call_tool_listing_preserves_list_error_when_cleanup_also_fails(
    tmp_path: Path,
) -> None:
    harness = UpstreamHarness(tmp_path, {"task": _upstream("task", mode="per_call")})
    client = FakeStdioClient(
        list_exception=StdioUpstreamError("list failed"),
        stop_exception=RuntimeError("cleanup failed"),
    )
    harness.stdio_clients_to_create.append(client)

    with pytest.raises(StdioUpstreamError, match="^list failed$"):
        harness._list_stdio_upstream("task", 13)


def test_per_call_tool_listing_reports_cleanup_error_after_success(
    tmp_path: Path,
) -> None:
    harness = UpstreamHarness(tmp_path, {"task": _upstream("task", mode="per_call")})
    client = FakeStdioClient(
        list_result=[{"name": "run"}],
        stop_exception=RuntimeError("cleanup failed"),
    )
    harness.stdio_clients_to_create.append(client)

    with pytest.raises(
        StdioUpstreamError,
        match="^upstream cleanup failed: task: cleanup failed$",
    ):
        harness._list_stdio_upstream("task", 13)


def test_per_call_upstream_resolves_session_environment(tmp_path: Path) -> None:
    upstream = _upstream(
        "task",
        mode="per_call",
        session_env={"PROJECT_DIR": "client_cwd"},
    )
    harness = UpstreamHarness(tmp_path, {"task": upstream})
    client = FakeStdioClient(call_result={"content": [{"text": "done"}]})
    harness.stdio_clients_to_create.append(client)

    harness._call_stdio_upstream(
        "task",
        "run",
        {},
        9,
        session_context={"client_cwd": "/tmp/project"},
    )

    assert harness.stdio_creates[0]["session_context"] == {
        "client_cwd": "/tmp/project"
    }


def test_per_call_cleanup_does_not_stop_client_owned_by_shutdown(tmp_path: Path) -> None:
    harness = UpstreamHarness(tmp_path, {"task": _upstream("task", mode="per_call")})
    client = FakeStdioClient()
    shutdown_client = FakeStdioClient()
    harness.stdio_clients_to_create.append(client)
    call_id, created = harness._per_call_stdio_client("task", session_context=None)
    harness._active_per_call_upstreams[call_id] = ("task", shutdown_client)

    harness._stop_per_call_stdio_client(
        call_id,
        "task",
        created,
        preserve_active_error=False,
    )

    assert client.stop_calls == 0
    assert harness._active_per_call_upstreams[call_id] == ("task", shutdown_client)


def test_per_call_upstream_refuses_creation_after_shutdown_started(tmp_path: Path) -> None:
    harness = UpstreamHarness(tmp_path, {"task": _upstream("task", mode="per_call")})
    harness._upstreams_shutdown = True
    harness.stdio_clients_to_create.append(FakeStdioClient())

    with pytest.raises(BrokerToolError) as exc:
        harness._call_stdio_upstream("task", "run", {}, 9)

    assert exc.value.message == "broker upstreams are shutting down"
    assert harness.stdio_creates == []


def test_shared_upstream_refuses_creation_after_shutdown_started(tmp_path: Path) -> None:
    harness = UpstreamHarness(tmp_path, {"task": _upstream("task")})
    harness._upstreams_shutdown = True
    harness.stdio_clients_to_create.append(FakeStdioClient())

    with pytest.raises(BrokerToolError) as exc:
        harness._call_stdio_upstream("task", "run", {}, 9)

    assert exc.value.message == "broker upstreams are shutting down"
    assert harness.stdio_creates == []


def test_per_call_cleanup_rejects_non_boolean_error_policy(tmp_path: Path) -> None:
    harness = UpstreamHarness(tmp_path, {"task": _upstream("task", mode="per_call")})
    client = FakeStdioClient()
    harness.stdio_clients_to_create.append(client)
    call_id, created = harness._per_call_stdio_client("task", session_context=None)

    with pytest.raises(
        TypeError,
        match="^preserve_active_error must be a bool$",
    ):
        harness._stop_per_call_stdio_client(
            call_id,
            "task",
            created,
            preserve_active_error=None,  # type: ignore[arg-type]
        )


def test_per_call_upstream_uses_one_client_for_auth_repair_then_stops(tmp_path: Path) -> None:
    repair = AuthRepairPolicy(
        tool="login",
        trigger_errors=("Not authenticated",),
        retry_original=True,
    )
    harness = UpstreamHarness(
        tmp_path,
        {"task": _upstream("task", mode="per_call", auth_repair=repair)},
    )
    client = FakeStdioClient(
        call_results=[
            _error_result("Not authenticated"),
            {"content": [{"type": "text", "text": "logged in"}]},
            {"content": [{"type": "text", "text": "done"}]},
        ]
    )
    harness.stdio_clients_to_create.append(client)

    result = harness._call_stdio_upstream("task", "run", {}, 9)

    assert result == {"content": [{"type": "text", "text": "done"}]}
    assert [call[0] for call in client.calls] == ["run", "login", "run"]
    assert client.stop_calls == 1
    assert len(harness.stdio_creates) == 1


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


class UpstreamHarness(BrokerDaemonUpstreamMixin):
    def __init__(self, tmp_path: Path, upstreams: dict[str, UpstreamConfig]) -> None:
        runtime = RuntimeConfig(
            root=tmp_path,
            socket_path=tmp_path / "broker.sock",
            log_dir=tmp_path / "logs",
            state_dir=tmp_path / "state",
            secrets_dir=tmp_path / "secrets",
        )
        self.broker_config = BrokerConfig(
            runtime=runtime,
            broker=BrokerSettings(),
            upstreams=upstreams,
        )
        self._paths = RuntimePaths.from_root(tmp_path)
        self._stdio_upstreams: dict[str | tuple[str, str], StdioUpstreamClientProtocol] = {}
        self._active_per_call_upstreams: dict[
            str, tuple[str, StdioUpstreamClientProtocol]
        ] = {}
        self._stdio_upstreams_lock = threading.Lock()
        self._upstreams_shutdown = False
        self._http_upstreams: dict[str, HttpUpstreamClientProtocol] = {}
        self.stdio_clients_to_create: list[FakeStdioClient] = []
        self.http_clients_to_create: list[FakeHttpClient] = []
        self.stdio_creates: list[dict[str, object]] = []
        self.http_creates: list[str] = []
        self.events: list[tuple[str, str, dict[str, object]]] = []
        self.auth_repair_events: list[tuple[str, str]] = []
        self.create_lock_states: list[bool] = []
        self.event_exception: Exception | None = None

    def _create_stdio_upstream_process(
        self,
        upstream: UpstreamConfig,
        **kwargs: object,
    ) -> FakeStdioClient:
        acquired = self._stdio_upstreams_lock.acquire(blocking=False)
        self.create_lock_states.append(not acquired)
        if acquired:
            self._stdio_upstreams_lock.release()
        self.stdio_creates.append({"upstream": upstream.name} | kwargs)
        return self.stdio_clients_to_create.pop(0)

    def _create_http_upstream_client(self, upstream: UpstreamConfig) -> FakeHttpClient:
        self.http_creates.append(upstream.name)
        return self.http_clients_to_create.pop(0)

    def _write_upstream_event(
        self,
        event: str,
        upstream_name: str,
        fields: dict[str, object],
    ) -> None:
        if self.event_exception is not None:
            raise self.event_exception
        self.events.append((event, upstream_name, fields))

    def _record_auth_repair_attempt(self, upstream_name: str) -> None:
        self.auth_repair_events.append(("attempt", upstream_name))

    def _record_auth_repair_success(self, upstream_name: str) -> None:
        self.auth_repair_events.append(("success", upstream_name))

    def _record_auth_repair_failure(self, upstream_name: str) -> None:
        self.auth_repair_events.append(("failure", upstream_name))


class FakeStdioClient:
    def __init__(
        self,
        *,
        call_result: dict[str, object] | None = None,
        call_results: list[dict[str, object]] | None = None,
        call_exception: Exception | None = None,
        list_result: list[dict[str, object]] | None = None,
        list_exception: Exception | None = None,
        stop_result: tuple[int, ...] = (),
        stop_exception: Exception | None = None,
        on_call: Callable[[], None] | None = None,
    ) -> None:
        self.call_result: dict[str, object] = (
            {"content": []} if call_result is None else call_result
        )
        self.call_results = [] if call_results is None else call_results
        self.call_exception = call_exception
        self.list_result = [] if list_result is None else list_result
        self.list_exception = list_exception
        self.calls: list[tuple[str, dict[str, object], int]] = []
        self.lists: list[int] = []
        self.stop_calls = 0
        self.stop_result = stop_result
        self.stop_exception = stop_exception
        self.on_call = on_call
        # Satisfies StdioUpstreamClientProtocol; tests do not exercise these.
        self.upstream = UpstreamConfig(name="fake", command="fake")

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: int,
    ) -> dict[str, object]:
        self.calls.append((tool_name, arguments, timeout_seconds))
        if self.on_call is not None:
            self.on_call()
        if self.call_exception is not None:
            raise self.call_exception
        if self.call_results:
            return self.call_results.pop(0)
        return self.call_result

    def list_tools(self, *, timeout_seconds: int) -> list[dict[str, object]]:
        self.lists.append(timeout_seconds)
        if self.list_exception is not None:
            raise self.list_exception
        return self.list_result

    def stop(self) -> tuple[int, ...]:
        self.stop_calls += 1
        if self.stop_exception is not None:
            raise self.stop_exception
        return self.stop_result

    def idle_seconds(self, *, now: float | None = None) -> float:
        return 0.0

    def health_snapshot(self) -> dict[str, object]:
        return {"state": "running"}

    def ensure_running(self) -> None:
        return None


class FakeHttpClient:
    def __init__(
        self,
        *,
        call_result: dict[str, object] | None = None,
        call_exception: Exception | None = None,
        list_result: list[dict[str, object]] | None = None,
    ) -> None:
        self.call_result: dict[str, object] = (
            {"content": []} if call_result is None else call_result
        )
        self.call_exception = call_exception
        self.list_result = [] if list_result is None else list_result
        self.calls: list[tuple[str, dict[str, object], int]] = []
        self.lists: list[int] = []

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: int,
    ) -> dict[str, object]:
        self.calls.append((tool_name, arguments, timeout_seconds))
        if self.call_exception is not None:
            raise self.call_exception
        return self.call_result

    def list_tools(self, *, timeout_seconds: int) -> list[dict[str, object]]:
        self.lists.append(timeout_seconds)
        return self.list_result

    def health_snapshot(self) -> dict[str, object]:
        return {"state": "running"}


def _upstream(
    name: str,
    *,
    mode: str = "shared",
    transport: str = "stdio",
    session_env: dict[str, str] | None = None,
    auth_repair: AuthRepairPolicy | None = None,
) -> UpstreamConfig:
    return UpstreamConfig(
        name=name,
        command="/bin/echo",
        mode=mode,
        transport=transport,
        session_env={} if session_env is None else session_env,
        auth_repair=auth_repair,
    )


def _error_result(message: str) -> dict[str, object]:
    return {
        "isError": True,
        "content": [{"type": "text", "text": f"Error: {message}"}],
    }
