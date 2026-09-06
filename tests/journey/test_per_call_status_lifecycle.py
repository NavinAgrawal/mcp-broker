"""Status observations during real per-call subprocess lifecycles."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import time

import pytest

from mcp_broker.broker import BrokerToolError
from tests.support.per_call_status import configured_daemon, status_facade, write_blocked_worker


pytestmark = pytest.mark.journey


def wait_for_file(path: Path) -> None:
    deadline = time.monotonic() + 5
    while not path.exists():
        if time.monotonic() >= deadline:
            pytest.fail(f"worker did not reach its call barrier: {path.name}")
        time.sleep(0.01)


@pytest.mark.parametrize("second_fails", [False, True])
def test_live_calls_report_two_then_one_then_zero_without_status_restarts(
    tmp_path: Path, second_fails: bool,
) -> None:
    daemon = configured_daemon(tmp_path)
    write_blocked_worker(tmp_path)
    facade = status_facade(daemon)
    snapshots = []
    facade_snapshots = []

    def capture() -> None:
        snapshots.append(daemon._upstream_health())
        payload = json.loads(facade.call_tool("broker.status", {})["content"][0]["text"])
        facade_snapshots.append(payload["upstreams"])

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(daemon._call_stdio_upstream, "task", "hold", {"token": "first"}, 8)
        second = executor.submit(
            daemon._call_stdio_upstream, "task", "hold", {"token": "second", "fail": second_fails}, 8,
        )
        try:
            wait_for_file(tmp_path / "first.started")
            wait_for_file(tmp_path / "second.started")
            started = (tmp_path / "starts.log").read_text(encoding="utf-8").splitlines()
            capture()
            capture()
            (tmp_path / "first.release").touch()
            first_result = first.result(timeout=5)
            capture()
            (tmp_path / "second.release").touch()
            second_result = second.result(timeout=5)
            capture()
        finally:
            (tmp_path / "first.release").touch()
            (tmp_path / "second.release").touch()

    assert [item["task"].get("active_call_count") for item in snapshots] == [2, 2, 1, 0]
    assert [item["task"].get("active_call_count") for item in facade_snapshots] == [2, 2, 1, 0]
    assert [item["task"]["state"] for item in snapshots] == ["running", "running", "running", "configured"]
    assert all(item["task"]["pid"] is None for item in snapshots + facade_snapshots)
    assert all(item["other-task"].get("active_call_count") == 0 for item in snapshots)
    assert all(item["task"]["restarts"] == 0 for item in snapshots)
    assert (tmp_path / "starts.log").read_text(encoding="utf-8").splitlines() == started
    returned = {first_result["content"][0]["text"], second_result["content"][0]["text"]}
    assert len(returned) == 2 and returned == set(started)
    assert second_result.get("isError", False) is second_fails
    assert daemon._active_per_call_upstreams == {}
    assert list(daemon._paths.upstream_pid_dir.glob("task.call.*.json")) == []
    for pid in returned:
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid), 0)


def test_restricted_status_hides_an_active_upstream_without_stopping_it(tmp_path: Path) -> None:
    daemon = configured_daemon(tmp_path)
    write_blocked_worker(tmp_path)
    facade = status_facade(daemon, profile_name="reader")
    with ThreadPoolExecutor(max_workers=2) as executor:
        visible = executor.submit(daemon._call_stdio_upstream, "task", "hold", {"token": "visible"}, 8)
        hidden = executor.submit(daemon._call_stdio_upstream, "other-task", "hold", {"token": "hidden"}, 8)
        try:
            wait_for_file(tmp_path / "visible.started")
            wait_for_file(tmp_path / "hidden.started")
            hidden_pid = int((tmp_path / "hidden.started").read_text())
            started = (tmp_path / "starts.log").read_text()
            payload = json.loads(facade.call_tool("broker.status", {})["content"][0]["text"])
            assert set(payload["upstreams"]) == {"task", "session-task"}
            assert payload["upstreams"]["task"]["active_call_count"] == 1
            assert daemon._upstream_health()["other-task"]["active_call_count"] == 1
            assert not hidden.done()
            os.kill(hidden_pid, 0)
            (tmp_path / "visible.release").touch()
            visible.result(timeout=5)
            after_visible = json.loads(facade.call_tool("broker.status", {})["content"][0]["text"])
            assert after_visible["upstreams"]["task"]["active_call_count"] == 0
            assert "other-task" not in after_visible["upstreams"]
            assert not hidden.done()
            os.kill(hidden_pid, 0)
            assert (tmp_path / "starts.log").read_text() == started
        finally:
            (tmp_path / "visible.release").touch()
            (tmp_path / "hidden.release").touch()
    assert hidden.result()["content"][0]["text"] == str(hidden_pid)
    assert daemon._active_per_call_upstreams == {}
    with pytest.raises(ProcessLookupError):
        os.kill(hidden_pid, 0)


def test_real_transport_timeout_clears_status_and_reaps_the_call_process(tmp_path: Path) -> None:
    daemon = configured_daemon(tmp_path)
    write_blocked_worker(tmp_path)
    facade = status_facade(daemon)
    with ThreadPoolExecutor(max_workers=1) as executor:
        call = executor.submit(daemon._call_stdio_upstream, "task", "hold", {"token": "timeout"}, 3)
        try:
            wait_for_file(tmp_path / "timeout.started")
            pid = int((tmp_path / "timeout.started").read_text())
            during = json.loads(facade.call_tool("broker.status", {})["content"][0]["text"])
            assert during["upstreams"]["task"]["active_call_count"] == 1
            with pytest.raises(BrokerToolError) as raised:
                call.result(timeout=8)
            assert raised.value.code == "upstream_timeout"
            assert raised.value.upstream_name == "task"
            assert raised.value.tool_name == "hold"
            after = json.loads(facade.call_tool("broker.status", {})["content"][0]["text"])
            assert after["upstreams"]["task"]["active_call_count"] == 0
            assert after["upstreams"]["task"]["state"] == "configured"
            assert after["upstreams"]["task"]["pid"] is None
            assert daemon._active_per_call_upstreams == {}
            assert list(daemon._paths.upstream_pid_dir.glob("task.call.*.json")) == []
            assert (tmp_path / "starts.log").read_text().splitlines() == [str(pid)]
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            (tmp_path / "timeout.release").touch()
