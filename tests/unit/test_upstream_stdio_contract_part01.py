import io
import json
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
def _script(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body.strip() + "\n", encoding="utf-8")
    return path

@pytest.mark.error_simulation
def test_stdio_upstream_finalizer_stops_live_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "runtime-state",
    )
    monkeypatch.setattr(
        StdioUpstreamProcess,
        "stop",
        lambda self: calls.append(self.upstream.name),
    )

    client.__del__()

    assert calls == ["fake"]

@pytest.mark.error_simulation
def test_stdio_upstream_finalizer_suppresses_stop_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "runtime-state",
    )

    def fail_stop(_self: StdioUpstreamProcess) -> None:
        raise RuntimeError("stop failed")

    monkeypatch.setattr(StdioUpstreamProcess, "stop", fail_stop)

    client.__del__()

def test_stdio_upstream_reuses_process_and_writes_stderr(tmp_path: Path) -> None:
    script = _script(
        tmp_path,
        "echo_worker.py",
        """
import json
import os
import sys

for line in sys.stdin:
    request = json.loads(line)
    print(f"stderr:{request['id']}", file=sys.stderr, flush=True)
    print(json.dumps({
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": {
            "cwd": os.getcwd(),
            "state": os.environ["MCP_BROKER_UPSTREAM_STATE_DIR"],
            "tool": request["params"]["name"],
            "arguments": request["params"]["arguments"],
        },
    }), flush=True)
""",
    )
    absolute_state_dir = tmp_path / "absolute-state"
    upstream = UpstreamConfig(
        name="fake",
        command=sys.executable,
        args=[str(script)],
        state_dir=str(absolute_state_dir),
    )
    client = StdioUpstreamProcess(upstream, runtime_state_dir=tmp_path / "runtime-state")

    try:
        first = client.call_tool(
            "fake.echo",
            {"message": "first"},
            timeout_seconds=STDIO_HAPPY_PATH_TIMEOUT_SECONDS,
        )
        pid = cast(subprocess.Popen[bytes], client._process).pid
        second = client.call_tool(
            "fake.echo",
            {"message": "second"},
            timeout_seconds=STDIO_HAPPY_PATH_TIMEOUT_SECONDS,
        )

        assert first["cwd"] == str(absolute_state_dir)
        assert first["state"] == str(absolute_state_dir)
        assert first["tool"] == "fake.echo"
        assert first["arguments"] == {"message": "first"}
        assert second["arguments"] == {"message": "second"}
        assert cast(subprocess.Popen[bytes], client._process).pid == pid
    finally:
        client.stop()

    assert "stderr:0" in (absolute_state_dir / "stderr.log").read_text(encoding="utf-8")

def test_stdio_upstream_emits_call_start_ready_and_stop_events(tmp_path: Path) -> None:
    script = _script(
        tmp_path,
        "event_worker.py",
        """
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": {"name": request["params"]["name"]},
    }), flush=True)
""",
    )
    events: list[dict[str, object]] = []
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable, args=[str(script)]),
        runtime_state_dir=tmp_path / "state",
        event_logger=lambda event, upstream, fields: events.append(
            {"event": event, "upstream": upstream} | fields
        ),
    )

    try:
        assert client.call_tool(
            "fake.echo",
            {},
            timeout_seconds=STDIO_HAPPY_PATH_TIMEOUT_SECONDS,
        ) == {"name": "fake.echo"}
        running_events = list(events)
    finally:
        client.stop()

    assert running_events[0] == {
        "event": "upstream.call",
        "upstream": "fake",
        "method": "tools/call",
        "tool_name": "fake.echo",
        "timeout_seconds": STDIO_HAPPY_PATH_TIMEOUT_SECONDS,
    }
    assert running_events[1] == {
        "event": "upstream.start",
        "upstream": "fake",
        "state": "starting",
    }
    assert running_events[2]["event"] == "upstream.ready"
    assert running_events[2]["upstream"] == "fake"
    assert running_events[2]["state"] == "running"
    assert isinstance(running_events[2]["pid"], int)
    assert events[-2] == {
        "event": "upstream.kill",
        "upstream": "fake",
        "signal": "SIGKILL",
        "reason": "final_cleanup",
    }
    assert events[-1] == {
        "event": "upstream.stop",
        "upstream": "fake",
        "state": "stopped",
    }

@pytest.mark.error_simulation
def test_stdio_stop_waits_after_final_process_group_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import mcp_broker.upstream_stdio as upstream_stdio_module

    class SlowProcess:
        pid = 12345

        def __init__(self) -> None:
            self.wait_timeouts: list[float] = []
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float) -> int:
            self.wait_timeouts.append(timeout)
            if len(self.wait_timeouts) < 3:
                raise subprocess.TimeoutExpired(["fake"], timeout)
            self.returncode = -signal.SIGKILL
            return self.returncode

    process = SlowProcess()
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
    )
    client._process = cast(Any, process)
    monkeypatch.setattr(upstream_stdio_module, "_process_group_id", lambda _pid: 12345)
    monkeypatch.setattr(upstream_stdio_module, "_wait_for_process_group_stop", lambda _pgid: ())
    monkeypatch.setattr(upstream_stdio_module, "_signal_process_group", lambda _pid, _sig: None)
    monkeypatch.setattr(upstream_stdio_module, "_close_process_pipes", lambda _process, include_stderr: None)

    assert client.stop() == ()
    assert process.returncode == -signal.SIGKILL
    assert process.wait_timeouts == [
        STOP_TIMEOUT_SECONDS,
        max(STOP_TIMEOUT_SECONDS, KILL_WAIT_SECONDS),
        KILL_WAIT_SECONDS,
    ]
    assert client.health_snapshot()["last_error"] == "upstream did not exit after SIGKILL: fake"

@pytest.mark.error_simulation
def test_stdio_stop_directly_kills_parent_after_group_cleanup_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import mcp_broker.upstream_stdio as upstream_stdio_module

    class GroupKillMissesParentProcess:
        pid = 12345

        def __init__(self) -> None:
            self.kill_calls = 0
            self.returncode: int | None = None
            self.wait_timeouts: list[float] = []

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float) -> int:
            self.wait_timeouts.append(timeout)
            if len(self.wait_timeouts) < 4:
                raise subprocess.TimeoutExpired(["fake"], timeout)
            self.returncode = -signal.SIGKILL
            return self.returncode

        def kill(self) -> None:
            self.kill_calls += 1

    process = GroupKillMissesParentProcess()
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
    )
    client._process = cast(Any, process)
    monkeypatch.setattr(upstream_stdio_module, "_process_group_id", lambda _pid: 12345)
    monkeypatch.setattr(upstream_stdio_module, "_wait_for_process_group_stop", lambda _pgid: ())
    monkeypatch.setattr(upstream_stdio_module, "_signal_process_group", lambda _pid, _sig: None)
    monkeypatch.setattr(upstream_stdio_module, "_close_process_pipes", lambda _process, include_stderr: None)

    assert client.stop() == ()
    assert process.kill_calls == 1
    assert process.returncode == -signal.SIGKILL
    assert process.wait_timeouts == [
        STOP_TIMEOUT_SECONDS,
        max(STOP_TIMEOUT_SECONDS, KILL_WAIT_SECONDS),
        KILL_WAIT_SECONDS,
        KILL_WAIT_SECONDS,
    ]
    assert client.health_snapshot()["last_error"] == (
        "upstream did not exit after final SIGKILL: fake"
    )

@pytest.mark.error_simulation
def test_stdio_stop_final_cleanup_uses_cached_process_group_after_parent_exits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import mcp_broker.upstream_stdio as upstream_stdio_module

    class ParentExitsAfterSigtermProcess:
        pid = 12345
        stdin = io.BytesIO()
        stdout = io.BytesIO()
        stderr = io.BytesIO()

        def __init__(self) -> None:
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, *, timeout: float) -> int:
            self.returncode = -signal.SIGTERM
            return self.returncode

    process = ParentExitsAfterSigtermProcess()
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
    )
    process_group_ids = iter([4321, 4321, None])
    killpg_calls: list[tuple[int, signal.Signals]] = []
    client._process = cast(Any, process)
    monkeypatch.setattr(
        upstream_stdio_module,
        "_process_group_id",
        lambda _pid: next(process_group_ids),
    )
    monkeypatch.setattr(
        upstream_stdio_module.os,
        "killpg",
        lambda pgid, sig: killpg_calls.append((pgid, sig)),
    )
    monkeypatch.setattr(upstream_stdio_module, "_wait_for_process_group_stop", lambda _pgid: ())
    monkeypatch.setattr(upstream_stdio_module, "_close_process_pipes", lambda _process, include_stderr: None)

    assert client.stop() == ()
    assert killpg_calls == [
        (4321, upstream_stdio_module.signal.SIGTERM),
        (4321, upstream_stdio_module.signal.SIGKILL),
    ]

def test_stdio_upstream_sends_exact_tool_call_payload(tmp_path: Path) -> None:
    requests_path = tmp_path / "call-requests.jsonl"
    script = _script(
        tmp_path,
        "call_payload_worker.py",
        f"""
import json
from pathlib import Path
import sys

requests_path = Path({str(requests_path)!r})

for line in sys.stdin:
    request = json.loads(line)
    with requests_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(request, sort_keys=True) + "\\n")
    print(json.dumps({{
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": {{"ok": True}},
    }}), flush=True)
""",
    )
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable, args=[str(script)]),
        runtime_state_dir=tmp_path / "state",
    )

    try:
        assert client.call_tool(
            "fake.echo",
            {"value": "hello"},
            timeout_seconds=STDIO_HAPPY_PATH_TIMEOUT_SECONDS,
        ) == {"ok": True}
        assert client.call_tool(
            "fake.echo",
            {"value": "again"},
            timeout_seconds=STDIO_HAPPY_PATH_TIMEOUT_SECONDS,
        ) == {"ok": True}
    finally:
        client.stop()

    requests = [
        json.loads(line) for line in requests_path.read_text(encoding="utf-8").splitlines()
    ]
    assert requests == [
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "tools/call",
            "params": {
                "name": "fake.echo",
                "arguments": {"value": "hello"},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "fake.echo",
                "arguments": {"value": "again"},
            },
        },
    ]

def test_stdio_upstream_initial_internal_state_contract(tmp_path: Path) -> None:
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
    )

    assert client._last_error is None
    assert client._initialized is False

def test_stdio_stderr_drainer_stops_when_stream_is_already_closed(tmp_path: Path) -> None:
    stream = io.BytesIO(b"")
    stream.close()
    log_path = tmp_path / "stderr.log"

    thread = _start_stderr_drainer(stream, log_path)
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert log_path.read_bytes() == b""

def test_stdio_stderr_drainer_writes_stream_and_uses_upstream_name(tmp_path: Path) -> None:
    stream = io.BytesIO(b"first\nsecond\n")
    log_path = tmp_path / "upstream-alpha" / "stderr.log"
    log_path.parent.mkdir()

    thread = _start_stderr_drainer(stream, log_path)
    thread.join(timeout=1)

    assert thread.name == "mcp-broker-stderr-upstream-alpha"
    assert thread.daemon is True
    assert not thread.is_alive()
    assert log_path.read_bytes() == b"first\nsecond\n"

def test_stdio_stderr_drainer_reads_fixed_chunks_and_flushes_each_write(
    tmp_path: Path,
) -> None:
    class Stream:
        def __init__(self) -> None:
            self.sizes: list[int] = []
            self.reads = iter([b"first", b"second", b""])

        def read(self, size: int) -> bytes:
            self.sizes.append(size)
            return next(self.reads)

    stream = Stream()
    log_path = tmp_path / "upstream-alpha" / "stderr.log"
    log_path.parent.mkdir()

    thread = _start_stderr_drainer(cast(Any, stream), log_path)
    thread.join(timeout=1)

    assert stream.sizes == [4096, 4096, 4096]
    assert log_path.read_bytes() == b"firstsecond"

def test_stdio_stderr_chunk_reader_returns_empty_for_closed_stream() -> None:
    stream = io.BytesIO(b"")
    stream.close()

    assert _read_stderr_chunk(stream) == b""

def test_stdio_stderr_chunk_reader_returns_empty_for_missing_stream() -> None:
    assert _read_stderr_chunk(None) == b""

def test_close_process_pipes_closes_stderr_by_default() -> None:
    process = cast(
        subprocess.Popen[bytes],
        type(
            "FakeProcess",
            (),
            {
                "stdin": io.BytesIO(),
                "stdout": io.BytesIO(),
                "stderr": io.BytesIO(),
            },
        )(),
    )

    _close_process_pipes(process, include_stderr=True)

    assert process.stdin.closed
    assert process.stdout.closed
    assert process.stderr.closed

def test_close_process_pipes_can_leave_stderr_open_for_drainer() -> None:
    process = cast(
        subprocess.Popen[bytes],
        type(
            "FakeProcess",
            (),
            {
                "stdin": io.BytesIO(),
                "stdout": io.BytesIO(),
                "stderr": io.BytesIO(),
            },
        )(),
    )

    _close_process_pipes(process, include_stderr=False)

    assert process.stdin.closed
    assert process.stdout.closed
    assert not process.stderr.closed

@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ({}, False),
        ({"jsonrpc": "2.0", "method": "notifications/progress"}, True),
        ({"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}, True),
        ({"jsonrpc": "2.0", "id": None, "method": "notifications/progress"}, False),
        ({"jsonrpc": "2.0", "id": 0, "method": "notifications/progress"}, False),
        ({"jsonrpc": "2.0", "method": 123}, False),
    ],
)
def test_jsonrpc_notification_detection_truth_table(
    message: dict[str, Any],
    expected: bool,
) -> None:
    assert _is_jsonrpc_notification(message) is expected

@pytest.mark.parametrize(
    ("error", "formatted"),
    [
        ({}, "{}"),
        ({"code": -32000}, "-32000"),
        ({"message": "Auth token missing"}, "Auth token missing"),
        ({"data": False}, "data=False"),
        ({"data": {"hint": "login"}}, "data={'hint': 'login'}"),
        (
            {"code": -32000, "message": "Auth token missing", "data": {"hint": "login"}},
            "-32000 Auth token missing data={'hint': 'login'}",
        ),
    ],
)
def test_response_error_formatting_truth_table(
    error: dict[str, Any],
    formatted: str,
) -> None:
    assert _format_response_error(error) == formatted
