import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


pytestmark = pytest.mark.journey

PROCESS_START_TIMEOUT_SECONDS = 5.0


def _wait_for_file_text(
    path: Path,
    expected: str,
    *,
    timeout_seconds: float = PROCESS_START_TIMEOUT_SECONDS,
) -> str:
    deadline = time.monotonic() + timeout_seconds
    last_text = ""
    while time.monotonic() < deadline:
        if path.exists():
            last_text = path.read_text(encoding="utf-8")
            if last_text == expected:
                return last_text
        time.sleep(0.01)
    return last_text


def _wait_for_file_int(
    path: Path,
    *,
    timeout_seconds: float = PROCESS_START_TIMEOUT_SECONDS,
) -> int:
    deadline = time.monotonic() + timeout_seconds
    last_text = ""
    while time.monotonic() < deadline:
        if path.exists():
            last_text = path.read_text(encoding="utf-8")
            try:
                return int(last_text)
            except ValueError:
                pass
        time.sleep(0.01)
    raise AssertionError(f"{path} did not contain an integer before timeout: {last_text!r}")


def test_upstream_process_lifecycle_starts_stops_and_restarts_real_process(
    tmp_path: Path,
) -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.upstream_state import UpstreamState
    from mcp_broker.upstream_process import UpstreamProcessSupervisor

    worker = tmp_path / "worker.py"
    ready_file = tmp_path / "ready.txt"
    worker.write_text(
        """
import signal
import sys
import time
from pathlib import Path

Path(sys.argv[1]).write_text("ready", encoding="utf-8")
signal.signal(signal.SIGTERM, lambda _signum, _frame: sys.exit(0))
while True:
    time.sleep(0.05)
""".strip(),
        encoding="utf-8",
    )
    runtime_state_dir = tmp_path / "runtime" / "state"
    upstream = UpstreamConfig(
        name="sleepy",
        command=sys.executable,
        args=[str(worker), str(ready_file)],
        mode="shared",
        enabled=True,
        state_dir="upstreams/sleepy",
        tool_prefix="sleepy",
    )
    supervisor = UpstreamProcessSupervisor(
        upstream=upstream,
        runtime_state_dir=runtime_state_dir,
    )

    try:
        assert supervisor.state == UpstreamState.CONFIGURED
        supervisor.start()
        first_pid = supervisor.pid

        assert supervisor.status == "running"
        assert supervisor.state == UpstreamState.RUNNING
        assert UpstreamState.STARTING in supervisor.state_history
        assert first_pid is not None
        assert _wait_for_file_text(ready_file, "ready") == "ready"
        assert supervisor.state_dir == runtime_state_dir / "upstreams" / "sleepy"
        assert supervisor.state_dir.is_dir()

        supervisor.restart()
        second_pid = supervisor.pid

        assert supervisor.status == "running"
        assert second_pid is not None
        assert second_pid != first_pid
        assert supervisor.restart_count == 1

        supervisor.stop()

        assert supervisor.status == "stopped"
        assert supervisor.state == UpstreamState.STOPPED
        assert UpstreamState.DRAINING in supervisor.state_history
        assert UpstreamState.STOPPING in supervisor.state_history
        assert supervisor.pid is None
        assert '"state": "stopped"' in supervisor.state_snapshot_path.read_text(encoding="utf-8")
    finally:
        supervisor.stop()


def test_upstream_process_lifecycle_emits_management_events(tmp_path: Path) -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.upstream_process import UpstreamProcessSupervisor

    events: list[dict[str, object]] = []

    def record(event: str, upstream_name: str, fields: dict[str, object]) -> None:
        events.append({"event": event, "upstream": upstream_name} | fields)

    disabled = UpstreamProcessSupervisor(
        upstream=UpstreamConfig(
            name="event-disabled",
            command=sys.executable,
            args=["-c", "raise SystemExit(0)"],
            mode="disabled",
            enabled=True,
            state_dir="upstreams/event-disabled",
            tool_prefix="event-disabled",
        ),
        runtime_state_dir=tmp_path / "runtime" / "state",
        event_logger=record,
    )

    with pytest.raises(ValueError, match="upstream disabled: event-disabled"):
        disabled.start()

    worker = tmp_path / "event_worker.py"
    ready_file = tmp_path / "event-ready.txt"
    worker.write_text(
        """
import signal
import sys
import time
from pathlib import Path

Path(sys.argv[1]).write_text("ready", encoding="utf-8")
signal.signal(signal.SIGTERM, lambda _signum, _frame: sys.exit(0))
while True:
    time.sleep(0.05)
""".strip(),
        encoding="utf-8",
    )
    supervisor = UpstreamProcessSupervisor(
        upstream=UpstreamConfig(
            name="evented",
            command=sys.executable,
            args=[str(worker), str(ready_file)],
            mode="shared",
            enabled=True,
            state_dir="upstreams/evented",
            tool_prefix="evented",
        ),
        runtime_state_dir=tmp_path / "runtime" / "state",
        event_logger=record,
    )

    try:
        supervisor.start(
            ready_check=lambda: ready_file.exists(),
            poll_interval_seconds=0.01,
        )
        supervisor.restart()
    finally:
        supervisor.stop()

    stubborn = tmp_path / "event_stubborn.py"
    stubborn_ready = tmp_path / "event-stubborn-ready.txt"
    stubborn.write_text(
        """
import signal
import sys
import time
from pathlib import Path

signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(sys.argv[1]).write_text("ready", encoding="utf-8")
while True:
    time.sleep(0.05)
""".strip(),
        encoding="utf-8",
    )
    stubborn_supervisor = UpstreamProcessSupervisor(
        upstream=UpstreamConfig(
            name="event-stubborn",
            command=sys.executable,
            args=[str(stubborn), str(stubborn_ready)],
            mode="shared",
            enabled=True,
            state_dir="upstreams/event-stubborn",
            tool_prefix="event-stubborn",
        ),
        runtime_state_dir=tmp_path / "runtime" / "state",
        event_logger=record,
    )

    try:
        stubborn_supervisor.start(
            ready_check=lambda: stubborn_ready.exists(),
            poll_interval_seconds=0.01,
        )
        stubborn_supervisor.stop(timeout_seconds=0.01)
    finally:
        stubborn_supervisor.stop(timeout_seconds=0.01)

    missing = UpstreamProcessSupervisor(
        upstream=UpstreamConfig(
            name="event-missing",
            command=str(tmp_path / "missing-command"),
            args=[],
            mode="shared",
            enabled=True,
            state_dir="upstreams/event-missing",
            tool_prefix="event-missing",
        ),
        runtime_state_dir=tmp_path / "runtime" / "state",
        event_logger=record,
    )

    with pytest.raises(FileNotFoundError):
        missing.start()

    event_names = [str(event["event"]) for event in events]

    assert "upstream.disabled" in event_names
    assert "upstream.start" in event_names
    assert "upstream.ready" in event_names
    assert "upstream.stop" in event_names
    assert "upstream.restart" in event_names
    assert "upstream.kill" in event_names
    assert "upstream.backoff" in event_names
    assert any(
        event["event"] == "upstream.ready"
        and event["upstream"] == "evented"
        and event["state"] == "running"
        for event in events
    )


def test_upstream_process_lifecycle_handles_disabled_absolute_and_exited_states(
    tmp_path: Path,
) -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.upstream_state import UpstreamState
    from mcp_broker.upstream_process import UpstreamProcessSupervisor

    disabled = UpstreamProcessSupervisor(
        upstream=UpstreamConfig(
            name="disabled",
            command=sys.executable,
            args=["-c", "raise SystemExit(0)"],
            mode="disabled",
            enabled=True,
            state_dir=str(tmp_path / "absolute-state"),
            tool_prefix="disabled",
        ),
        runtime_state_dir=tmp_path / "runtime" / "state",
    )

    assert disabled.state_dir == tmp_path / "absolute-state"
    assert disabled.state == UpstreamState.DISABLED
    disabled.stop()
    with pytest.raises(ValueError, match="upstream disabled: disabled"):
        disabled.start()

    exited = UpstreamProcessSupervisor(
        upstream=UpstreamConfig(
            name="exited",
            command=sys.executable,
            args=["-c", "raise SystemExit(0)"],
            mode="shared",
            enabled=True,
            state_dir="upstreams/exited",
            tool_prefix="exited",
        ),
        runtime_state_dir=tmp_path / "runtime" / "state",
    )

    exited.start()
    deadline = time.monotonic() + 2
    while exited.status == "running" and time.monotonic() < deadline:
        time.sleep(0.01)

    assert exited.status == "exited"
    assert exited.state == UpstreamState.FAILED
    assert exited.pid is None
    exited.stop()
    assert exited.status == "stopped"


def test_upstream_process_lifecycle_injects_configured_environment_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.upstream_process import UpstreamProcessSupervisor

    worker = tmp_path / "env_worker.py"
    output = tmp_path / "env.txt"
    worker.write_text(
        """
import os
import signal
import sys
import time
from pathlib import Path

Path(sys.argv[1]).write_text(os.environ["TARGET_TOKEN"], encoding="utf-8")
signal.signal(signal.SIGTERM, lambda _signum, _frame: sys.exit(0))
while True:
    time.sleep(0.05)
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("SOURCE_TOKEN", "from-host-env")
    supervisor = UpstreamProcessSupervisor(
        upstream=UpstreamConfig(
            name="env-worker",
            command=sys.executable,
            args=[str(worker), str(output)],
            mode="shared",
            enabled=True,
            state_dir="upstreams/env-worker",
            tool_prefix="env-worker",
            env={"TARGET_TOKEN": "SOURCE_TOKEN"},
        ),
        runtime_state_dir=tmp_path / "runtime" / "state",
    )

    try:
        supervisor.start()

        assert _wait_for_file_text(output, "from-host-env") == "from-host-env"
    finally:
        supervisor.stop()


def test_upstream_process_lifecycle_records_backoff_when_start_fails(
    tmp_path: Path,
) -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.upstream_state import UpstreamState
    from mcp_broker.upstream_process import UpstreamProcessSupervisor

    supervisor = UpstreamProcessSupervisor(
        upstream=UpstreamConfig(
            name="missing",
            command=str(tmp_path / "missing-command"),
            args=[],
            mode="shared",
            enabled=True,
            state_dir="upstreams/missing",
            tool_prefix="missing",
        ),
        runtime_state_dir=tmp_path / "runtime" / "state",
    )

    with pytest.raises(FileNotFoundError):
        supervisor.start()

    assert supervisor.state == UpstreamState.BACKOFF
    assert UpstreamState.STARTING in supervisor.state_history
    assert UpstreamState.FAILED in supervisor.state_history
    assert '"state": "backoff"' in supervisor.state_snapshot_path.read_text(encoding="utf-8")


def test_upstream_process_lifecycle_start_is_idempotent_and_stop_kills_stubborn_process(
    tmp_path: Path,
) -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.upstream_process import UpstreamProcessSupervisor

    worker = tmp_path / "stubborn.py"
    ready_file = tmp_path / "stubborn-ready.txt"
    worker.write_text(
        """
import signal
import sys
import time
from pathlib import Path

signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(sys.argv[1]).write_text("ready", encoding="utf-8")
while True:
    time.sleep(0.05)
""".strip(),
        encoding="utf-8",
    )
    supervisor = UpstreamProcessSupervisor(
        upstream=UpstreamConfig(
            name="stubborn",
            command=sys.executable,
            args=[str(worker), str(ready_file)],
            mode="shared",
            enabled=True,
            state_dir="upstreams/stubborn",
            tool_prefix="stubborn",
        ),
        runtime_state_dir=tmp_path / "runtime" / "state",
    )

    try:
        supervisor.start()
        first_pid = supervisor.pid
        assert _wait_for_file_text(ready_file, "ready") == "ready"
        supervisor.start()

        assert supervisor.pid == first_pid
        assert supervisor.status == "running"

        supervisor.stop(timeout_seconds=0.01)

        assert supervisor.status == "stopped"
        assert supervisor.pid is None
    finally:
        supervisor.stop(timeout_seconds=0.01)


@pytest.mark.error_simulation
def test_upstream_process_lifecycle_marks_failed_when_kill_wait_times_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import upstream_process
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.upstream_process import UpstreamProcessSupervisor
    from mcp_broker.upstream_state import UpstreamState

    worker = tmp_path / "kill_timeout.py"
    ready_file = tmp_path / "kill-timeout-ready.txt"
    worker.write_text(
        """
import signal
import sys
import time
from pathlib import Path

signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(sys.argv[1]).write_text("ready", encoding="utf-8")
while True:
    time.sleep(0.05)
""".strip(),
        encoding="utf-8",
    )
    supervisor = UpstreamProcessSupervisor(
        upstream=UpstreamConfig(
            name="kill-timeout",
            command=sys.executable,
            args=[str(worker), str(ready_file)],
            mode="shared",
            enabled=True,
            state_dir="upstreams/kill-timeout",
            tool_prefix="kill-timeout",
        ),
        runtime_state_dir=tmp_path / "runtime" / "state",
    )

    try:
        supervisor.start()
        assert _wait_for_file_text(ready_file, "ready") == "ready"
        process = supervisor._process
        assert process is not None
        wait_timeouts: list[float | None] = []

        def timeout_wait(timeout: float | None = None) -> int:
            wait_timeouts.append(timeout)
            raise subprocess.TimeoutExpired(process.args, timeout)

        with monkeypatch.context() as patch:
            patch.setattr(upstream_process, "KILL_WAIT_SECONDS", 0.0)
            patch.setattr(process, "wait", timeout_wait)
            with pytest.raises(subprocess.TimeoutExpired):
                supervisor.stop(timeout_seconds=0.0)

        assert wait_timeouts == [0.0, 0.0]
        assert supervisor.state == UpstreamState.FAILED
        assert UpstreamState.FAILED in supervisor.state_history
        assert '"state": "failed"' in supervisor.state_snapshot_path.read_text(encoding="utf-8")
    finally:
        supervisor.stop(timeout_seconds=0.1)


def test_upstream_process_lifecycle_stop_cleans_up_child_process_group(
    tmp_path: Path,
) -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.upstream_process import UpstreamProcessSupervisor, _process_group_members

    worker = tmp_path / "parent.py"
    child_pid_file = tmp_path / "child.pid"
    ready_file = tmp_path / "group-ready.txt"
    worker.write_text(
        """
import signal
import subprocess
import sys
import time
from pathlib import Path

child = subprocess.Popen([
    sys.executable,
    "-c",
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
])
Path(sys.argv[1]).write_text(str(child.pid), encoding="utf-8")
Path(sys.argv[2]).write_text("ready", encoding="utf-8")
signal.signal(signal.SIGTERM, lambda _signum, _frame: sys.exit(0))
while True:
    time.sleep(0.05)
""".strip(),
        encoding="utf-8",
    )
    supervisor = UpstreamProcessSupervisor(
        upstream=UpstreamConfig(
            name="grouped",
            command=sys.executable,
            args=[str(worker), str(child_pid_file), str(ready_file)],
            mode="shared",
            enabled=True,
            state_dir="upstreams/grouped",
            tool_prefix="grouped",
        ),
        runtime_state_dir=tmp_path / "runtime" / "state",
    )
    child_pid: int | None = None
    process_group_id: int | None = None

    try:
        supervisor.start()
        assert _wait_for_file_text(ready_file, "ready") == "ready"
        child_pid = _wait_for_file_int(child_pid_file)
        assert supervisor.pid is not None
        process_group_id = os.getpgid(supervisor.pid)

        supervisor.stop(timeout_seconds=0.05)

        deadline = time.monotonic() + 2
        while _process_exists(child_pid) and time.monotonic() < deadline:
            time.sleep(0.01)

        assert supervisor.status == "stopped"
        assert not _process_exists(child_pid)
        assert process_group_id is not None
        assert _process_group_members(process_group_id) == ()
    finally:
        supervisor.stop(timeout_seconds=0.05)
        if child_pid is not None and _process_exists(child_pid):
            os.kill(child_pid, signal.SIGKILL)


def test_upstream_process_lifecycle_fails_if_process_group_verification_finds_members(
    tmp_path: Path,
) -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.upstream_process import UpstreamProcessSupervisor
    from mcp_broker.upstream_state import UpstreamState

    supervisor = UpstreamProcessSupervisor(
        upstream=UpstreamConfig(
            name="survivor",
            command=sys.executable,
            args=["-c", "raise SystemExit(0)"],
            mode="shared",
            enabled=True,
            state_dir="upstreams/survivor",
            tool_prefix="survivor",
        ),
        runtime_state_dir=tmp_path / "runtime" / "state",
    )

    with pytest.raises(RuntimeError, match="process group still has members after stop"):
        supervisor._verify_process_group_stopped(os.getpgid(os.getpid()))

    assert supervisor.state == UpstreamState.FAILED


def test_upstream_process_lifecycle_reads_current_process_group_members() -> None:
    from mcp_broker.upstream_process import _process_group_members

    assert os.getpid() in _process_group_members(os.getpgid(os.getpid()))


def test_upstream_process_lifecycle_covers_drainer_and_group_edge_branches(
    tmp_path: Path,
) -> None:
    from mcp_broker.config import UpstreamConfig
    from mcp_broker.upstream_process import (
        UpstreamProcessSupervisor,
        _parse_process_group_members,
    )

    class JoinedDrainer:
        def __init__(self) -> None:
            self.joined = False

        def join(self, *, timeout: float) -> None:
            assert timeout > 0
            self.joined = True

    supervisor = UpstreamProcessSupervisor(
        upstream=UpstreamConfig(
            name="edge-branches",
            command=sys.executable,
            mode="shared",
            enabled=True,
            state_dir="upstreams/edge-branches",
            tool_prefix="edge-branches",
        ),
        runtime_state_dir=tmp_path / "runtime" / "state",
    )
    drainer = JoinedDrainer()
    supervisor._stdout_drainer = drainer
    supervisor._stderr_drainer = None

    supervisor._join_output_drainers()

    assert drainer.joined is True
    assert supervisor._stdout_drainer is None
    assert supervisor._stderr_drainer is None

    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(0.01)"],
        start_new_session=True,
    )
    process_group_id = os.getpgid(process.pid)
    process.wait(timeout=2)

    supervisor._verify_process_group_stopped(process_group_id)
    supervisor._verify_process_group_stopped(process_group_id, verify_seconds=0.0)

    assert _parse_process_group_members("\n 111\n\n 222\n") == (111, 222)


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
