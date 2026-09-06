import json
from pathlib import Path

import pytest

from tests.support.per_call_status import configured_daemon, status_facade


pytestmark = pytest.mark.unit


def test_idle_per_call_health_reports_zero_without_starting_process(tmp_path: Path) -> None:
    daemon = configured_daemon(tmp_path)
    snapshot = daemon._upstream_health()["task"]

    assert snapshot.get("active_call_count") == 0
    assert snapshot["state"] == "configured"
    assert snapshot["pid"] is None
    assert daemon._active_per_call_upstreams == {}
    assert not daemon._paths.upstream_pid_dir.exists()


def test_health_counts_registered_calls_per_upstream_and_after_cleanup(tmp_path: Path) -> None:
    daemon = configured_daemon(tmp_path)
    first_id, first = daemon._per_call_stdio_client("task", session_context=None)
    second_id, second = daemon._per_call_stdio_client("task", session_context=None)
    other_id, other = daemon._per_call_stdio_client("other-task", session_context=None)
    try:
        two = daemon._upstream_health()
        daemon._stop_per_call_stdio_client(first_id, "task", first, preserve_active_error=False)
        one = daemon._upstream_health()
        daemon._stop_per_call_stdio_client(second_id, "task", second, preserve_active_error=False)
        zero = daemon._upstream_health()

        assert [item["task"].get("active_call_count") for item in (two, one, zero)] == [2, 1, 0]
        assert [item["other-task"].get("active_call_count") for item in (two, one, zero)] == [1, 1, 1]
        assert all(item["task"]["pid"] is None for item in (two, one, zero))
        assert first.pid is None and second.pid is None and other.pid is None
        assert daemon._stdio_upstreams == {}
    finally:
        daemon._stop_per_call_stdio_client(first_id, "task", first, preserve_active_error=False)
        daemon._stop_per_call_stdio_client(second_id, "task", second, preserve_active_error=False)
        daemon._stop_per_call_stdio_client(other_id, "other-task", other, preserve_active_error=False)


@pytest.mark.parametrize("count", [0, 1, 2])
def test_broker_facade_preserves_per_call_count(tmp_path: Path, count: int) -> None:
    daemon = configured_daemon(tmp_path)
    clients = [daemon._per_call_stdio_client("task", session_context=None) for _ in range(count)]
    try:
        payload = json.loads(status_facade(daemon).call_tool("broker.status", {})["content"][0]["text"])
        snapshot = payload["upstreams"]["task"]

        assert snapshot.get("active_call_count") == count
        assert snapshot["pid"] is None
        assert snapshot["mode"] == "per_call"
        assert payload["upstreams"]["other-task"].get("active_call_count") == 0
        assert all(client.pid is None for _, client in clients)
    finally:
        for call_id, client in clients:
            daemon._stop_per_call_stdio_client(call_id, "task", client, preserve_active_error=False)


def test_per_session_counts_stay_independent_of_per_call_activity(tmp_path: Path) -> None:
    daemon = configured_daemon(tmp_path)
    first_session = daemon._stdio_client("session-task", session_id="session-one")
    second_session = daemon._stdio_client("session-task", session_id="session-two")
    facade = status_facade(daemon)
    health = []
    exposed = []

    def capture() -> None:
        health.append(daemon._upstream_health())
        exposed.append(json.loads(facade.call_tool("broker.status", {})["content"][0]["text"])["upstreams"])

    capture()
    call_id, client = daemon._per_call_stdio_client("task", session_context=None)
    try:
        capture()
        daemon._shutdown_session_upstreams("session-one")
        capture()
        daemon._stop_per_call_stdio_client(call_id, "task", client, preserve_active_error=False)
        capture()

        assert [item["session-task"]["sessions"] for item in health] == [2, 2, 1, 1]
        assert [item["session-task"]["session_count"] for item in exposed] == [2, 2, 1, 1]
        assert [item["task"].get("active_call_count") for item in health] == [0, 1, 1, 0]
        assert all(item["task"]["session_count"] == 0 for item in exposed)
        assert all("active_call_count" not in item["session-task"] for item in health)
        assert daemon._stdio_upstreams == {("session-task", "session-two"): second_session}
        assert first_session.pid is None and second_session.pid is None
    finally:
        daemon._stop_per_call_stdio_client(call_id, "task", client, preserve_active_error=False)
        daemon._shutdown_session_upstreams("session-one")
        daemon._shutdown_session_upstreams("session-two")
