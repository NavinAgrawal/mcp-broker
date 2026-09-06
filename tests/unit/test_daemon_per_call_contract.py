"""Per-call ownership, cleanup, shutdown, and authentication contracts."""

from pathlib import Path

import pytest

from mcp_broker.broker import BrokerToolError
from mcp_broker.schema import AuthRepairPolicy
from mcp_broker.upstream_stdio import StdioUpstreamError
from tests.support.daemon_upstreams import FakeStdioClient, UpstreamHarness, _error_result, _upstream


pytestmark = pytest.mark.unit


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
