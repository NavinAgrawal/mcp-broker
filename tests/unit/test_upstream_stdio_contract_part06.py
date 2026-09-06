import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from typing import Any, cast
import pytest
from mcp_broker.config import UpstreamConfig
from mcp_broker.upstream_process import (
    KILL_WAIT_SECONDS,
    PROCESS_GROUP_VERIFY_SECONDS,
    STOP_TIMEOUT_SECONDS,
)
from mcp_broker.upstream_stdio import (
    StdioUpstreamError,
    StdioUpstreamProcess,
    StdioUpstreamTimeout,
    _close_process_pipes,
    _format_response_error,
    _is_jsonrpc_notification,
    _process_group_id,
    _process_group_members,
    _read_stderr_chunk,
    _signal_process_group,
    _start_stderr_drainer,
    _wait_for_process_group_stop,
)
pytestmark = pytest.mark.unit
STDIO_HAPPY_PATH_TIMEOUT_SECONDS = 3
class ClosedPipeProcess:
    def __init__(self, *, stdin: object | None, stdout: object | None) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = None
class BrokenPipeStdin:
    def write(self, _payload: bytes) -> int:
        raise BrokenPipeError

    def flush(self) -> None:
        raise AssertionError("flush should not run after a broken pipe")
class RecordingStdin:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.flushes = 0

    def write(self, payload: bytes) -> int:
        self.writes.append(payload)
        return len(payload)

    def flush(self) -> None:
        self.flushes += 1
class FilenoOnly:
    def fileno(self) -> int:
        return 0
class ExitedProcessWithPipes:
    def __init__(self, *, stdin: object | None = io.BytesIO()) -> None:
        self.pid = 999999
        self.stdin = stdin
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()

    def poll(self) -> int:
        return 0
class RunningProcessForHealth:
    pid = 999998
    stdin = io.BytesIO()
    stdout = io.BytesIO()
    stderr = io.BytesIO()

    def poll(self) -> None:
        return None
class StubbornProcess:
    pid = 999999
    stdin = io.BytesIO()
    stdout = io.BytesIO()
    stderr = io.BytesIO()

    def __init__(self) -> None:
        self.waits = 0
        self.returncode: int | None = None
        self.wait_timeouts: list[float] = []

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, *, timeout: float) -> int:
        self.waits += 1
        self.wait_timeouts.append(timeout)
        if self.waits == 1:
            raise subprocess.TimeoutExpired("fake", timeout)
        self.returncode = 0
        return self.returncode
class NeverExitsProcess:
    pid = 999997
    stdin = io.BytesIO()
    stdout = io.BytesIO()
    stderr = io.BytesIO()

    def __init__(self) -> None:
        self.waits = 0
        self.wait_timeouts: list[float] = []

    def poll(self) -> None:
        return None

    def wait(self, *, timeout: float) -> int:
        self.waits += 1
        self.wait_timeouts.append(timeout)
        raise subprocess.TimeoutExpired("fake", timeout)

    def kill(self) -> None:
        return None
class RecordingDrainer:
    def __init__(self) -> None:
        self.join_timeouts: list[float] = []

    def join(self, *, timeout: float) -> None:
        self.join_timeouts.append(timeout)


def test_stdio_upstream_initial_state_is_explicit(tmp_path: Path) -> None:
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
    )

    assert isinstance(client._last_activity_monotonic, float)
    assert client._last_error is None
    assert client._initialized is False


def test_stdio_upstream_metadata_writer_requires_process_and_runtime_paths(
    tmp_path: Path,
) -> None:
    without_paths = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
    )
    without_paths._process = cast(Any, RunningProcessForHealth())
    without_paths._write_process_metadata()

    from mcp_broker.runtime_reaper import RuntimePaths

    without_process = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
        runtime_paths=RuntimePaths.from_root(tmp_path / "runtime"),
    )
    without_process._write_process_metadata()

    assert without_paths._process_metadata_path is None
    assert without_process._process_metadata_path is None


@pytest.mark.error_simulation
def test_stdio_upstream_stop_records_first_sigkill_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import upstream_stdio

    class ExitsAfterGroupCheck:
        pid = 999_996
        stdin = io.BytesIO()
        stdout = io.BytesIO()
        stderr = io.BytesIO()

        def __init__(self) -> None:
            self.exited = False

        def poll(self) -> int | None:
            return 0 if self.exited else None

        def wait(self, *, timeout: float) -> int:
            raise subprocess.TimeoutExpired("fake", timeout)

    process = ExitsAfterGroupCheck()
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
    )
    client._process = cast(Any, process)
    monkeypatch.setattr(upstream_stdio, "_signal_process_group", lambda *_args: None)
    monkeypatch.setattr(
        upstream_stdio,
        "_wait_for_process_group_stop",
        lambda _pgid: setattr(process, "exited", True) or (),
    )

    client.stop()

    assert client._last_error == "upstream did not exit after SIGKILL: fake"


@pytest.mark.error_simulation
def test_stdio_upstream_stop_records_final_sigkill_timeout_before_direct_kill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import upstream_stdio

    class ExitsAfterDirectKill:
        pid = 999_995
        stdin = io.BytesIO()
        stdout = io.BytesIO()
        stderr = io.BytesIO()

        def __init__(self) -> None:
            self.killed = False

        def poll(self) -> int | None:
            return 0 if self.killed else None

        def wait(self, *, timeout: float) -> int:
            if not self.killed:
                raise subprocess.TimeoutExpired("fake", timeout)
            return 0

        def kill(self) -> None:
            self.killed = True

    process = ExitsAfterDirectKill()
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
    )
    client._process = cast(Any, process)
    monkeypatch.setattr(upstream_stdio, "_signal_process_group", lambda *_args: None)
    monkeypatch.setattr(upstream_stdio, "_wait_for_process_group_stop", lambda _pgid: ())

    client.stop()

    assert client._last_error == "upstream did not exit after final SIGKILL: fake"
def _script(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body.strip() + "\n", encoding="utf-8")
    return path

@pytest.mark.error_simulation
def test_stdio_upstream_stop_removes_metadata_by_name_when_path_not_cached(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import upstream_stdio
    from mcp_broker.runtime_reaper import RuntimePaths, write_process_metadata

    paths = RuntimePaths.from_root(tmp_path / "runtime")
    metadata_path = write_process_metadata(
        paths,
        name="fake",
        pid=999_999,
        process_group_id=999_999,
        broker_pid=os.getpid(),
    )
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
        runtime_paths=paths,
    )
    client._process = cast(Any, StubbornProcess())
    monkeypatch.setattr(upstream_stdio.os, "killpg", lambda _pid, _sig: None)
    monkeypatch.setattr(upstream_stdio, "_wait_for_process_group_stop", lambda _pgid: ())

    client.stop()

    assert not metadata_path.exists()
    assert client._process is None

def test_stdio_upstream_writes_and_removes_cached_process_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import upstream_stdio
    from mcp_broker.runtime_reaper import RuntimePaths

    paths = RuntimePaths.from_root(tmp_path / "runtime")
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
        runtime_paths=paths,
    )
    client._process = cast(Any, RunningProcessForHealth())
    monkeypatch.setattr(upstream_stdio.os, "getpgid", lambda pid: pid + 1)
    monkeypatch.setattr(upstream_stdio.os, "getpid", lambda: 222)

    client._write_process_metadata()

    metadata_path = paths.upstream_pid_dir / "fake.json"
    assert client._process_metadata_path == metadata_path
    assert json.loads(metadata_path.read_text(encoding="utf-8")) == {
        "broker_pid": 222,
        "name": "fake",
        "owner": "mcp-broker",
        "pid": 999998,
        "process_group_id": 999999,
    }

    client._remove_process_metadata()

    assert not metadata_path.exists()
    assert client._process_metadata_path is None

def test_stdio_upstream_remove_metadata_prefers_cached_path_over_runtime_name(
    tmp_path: Path,
) -> None:
    from mcp_broker.runtime_reaper import RuntimePaths

    paths = RuntimePaths.from_root(tmp_path / "runtime")
    paths.ensure()
    cached_path = tmp_path / "custom-metadata.json"
    fallback_path = paths.upstream_pid_dir / "fake.json"
    cached_path.write_text("cached", encoding="utf-8")
    fallback_path.write_text("fallback", encoding="utf-8")
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
        runtime_paths=paths,
    )
    client._process_metadata_path = cached_path

    client._remove_process_metadata()

    assert not cached_path.exists()
    assert fallback_path.read_text(encoding="utf-8") == "fallback"
    assert client._process_metadata_path is None

def test_stdio_upstream_remove_metadata_tolerates_missing_cached_path(
    tmp_path: Path,
) -> None:
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
    )
    client._process_metadata_path = tmp_path / "already-gone.json"

    client._remove_process_metadata()

    assert client._process_metadata_path is None

@pytest.mark.error_simulation
def test_stdio_upstream_stop_handles_parent_that_survives_sigkill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import upstream_stdio

    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
    )
    process = NeverExitsProcess()
    monkeypatch.setattr(
        upstream_stdio.os,
        "getpgid",
        lambda _pid: (_ for _ in ()).throw(ProcessLookupError),
    )
    client._process = cast(Any, process)

    assert client.stop() == ()

    assert process.waits == 4
    assert process.wait_timeouts == [
        STOP_TIMEOUT_SECONDS,
        max(STOP_TIMEOUT_SECONDS, KILL_WAIT_SECONDS),
        KILL_WAIT_SECONDS,
        KILL_WAIT_SECONDS,
    ]
    assert client.health_snapshot()["last_error"] == "upstream did not exit after direct SIGKILL: fake"
    assert client._process is None

@pytest.mark.error_simulation
def test_stdio_process_group_wait_reports_remaining_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import upstream_stdio

    times = iter([0.0, 0.0, 1.0])
    monkeypatch.setattr(upstream_stdio.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(upstream_stdio, "_process_group_members", lambda _pgid: (111, 222))

    assert upstream_stdio._wait_for_process_group_stop(999) == (111, 222)

def test_stdio_process_group_wait_uses_strict_deadline_and_final_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import upstream_stdio

    times = iter([0.0, 0.0, PROCESS_GROUP_VERIFY_SECONDS])
    groups: list[int | None] = []

    def fake_members(process_group_id: int | None) -> tuple[int, ...]:
        groups.append(process_group_id)
        return (process_group_id or 0,)

    monkeypatch.setattr(upstream_stdio.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(upstream_stdio, "_process_group_members", fake_members)

    assert _wait_for_process_group_stop(999) == (999,)
    assert groups == [999, 999]

def test_stdio_process_group_wait_polls_until_group_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import upstream_stdio

    times = iter([0.0, 0.0, 0.1])
    seen_groups: list[int] = []
    waits: list[float] = []
    members = iter([(111,), ()])

    class Pause:
        def wait(self, *, timeout: float) -> bool:
            waits.append(timeout)
            return False

    def fake_members(process_group_id: int) -> tuple[int, ...]:
        seen_groups.append(process_group_id)
        return next(members)

    monkeypatch.setattr(upstream_stdio.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(upstream_stdio.threading, "Event", Pause)
    monkeypatch.setattr(upstream_stdio, "_process_group_members", fake_members)

    assert _wait_for_process_group_stop(999) == ()
    assert seen_groups == [999, 999]
    assert waits == [0.01]

def test_stdio_process_group_members_parses_ps_output(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp_broker import upstream_stdio

    class Completed:
        stdout = " 111\n\n 222\n"

    monkeypatch.setattr(upstream_stdio.subprocess, "run", lambda *_, **__: Completed())

    assert upstream_stdio._process_group_members(999) == (111, 222)

def test_stdio_process_group_members_uses_non_throwing_ps_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    class Completed:
        stdout = " 333 \n444\n"

    def fake_run(args: list[str], **kwargs: object) -> Completed:
        calls.append((args, kwargs))
        return Completed()

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert _process_group_members(777) == (333, 444)
    assert calls == [
        (
            ["ps", "-o", "pid=", "-g", "777"],
            {"check": False, "capture_output": True, "text": True},
        )
    ]

def test_stdio_process_group_members_returns_empty_tuple_for_empty_ps_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Completed:
        stdout = "\n   \n"

    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: Completed())

    assert _process_group_members(777) == ()

@pytest.mark.error_simulation
def test_stdio_process_group_helpers_ignore_vanished_or_forbidden_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import upstream_stdio

    signals: list[signal.Signals] = []

    def missing_group(_pid: int) -> int:
        raise ProcessLookupError

    def forbidden_signal(_pid: int, sig: signal.Signals) -> None:
        signals.append(sig)
        raise PermissionError

    monkeypatch.setattr(upstream_stdio.os, "getpgid", missing_group)
    monkeypatch.setattr(upstream_stdio.os, "killpg", forbidden_signal)

    assert _process_group_id(999) is None
    _signal_process_group(999, signal.SIGTERM)
    assert signals == [signal.SIGTERM]

def test_stdio_process_group_helpers_call_success_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    groups: list[int] = []
    signals: list[tuple[int, signal.Signals]] = []

    monkeypatch.setattr(os, "getpgid", lambda pid: groups.append(pid) or 1234)
    monkeypatch.setattr(os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    assert _process_group_id(999) == 1234
    _signal_process_group(1234, signal.SIGKILL)

    assert groups == [999]
    assert signals == [(1234, signal.SIGKILL)]

def test_stdio_process_group_wait_returns_empty_for_missing_group() -> None:
    from mcp_broker import upstream_stdio

    assert upstream_stdio._wait_for_process_group_stop(None) == ()

@pytest.mark.error_simulation
def test_stdio_start_restarts_without_stderr_drainer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import upstream_stdio

    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
    )
    client._process = cast(Any, ExitedProcessWithPipes())
    client._stderr_drainer = None
    monkeypatch.setattr(
        upstream_stdio.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("spawn failed")),
    )

    with pytest.raises(StdioUpstreamError, match="upstream failed to start: fake: spawn failed"):
        client._start()

    assert client.health_snapshot()["restarts"] == 1

def test_stdio_close_process_pipes_skips_missing_stream_and_optional_stderr() -> None:
    process = ExitedProcessWithPipes(stdin=None)

    _close_process_pipes(cast(Any, process), include_stderr=False)

    assert process.stdout.closed is True
    assert process.stderr.closed is False

def test_stdio_close_process_pipes_closes_stderr_by_default() -> None:
    process = ExitedProcessWithPipes()

    _close_process_pipes(cast(Any, process), include_stderr=True)

    assert process.stdin.closed is True
    assert process.stdout.closed is True
    assert process.stderr.closed is True
