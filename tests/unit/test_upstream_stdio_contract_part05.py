import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
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

@pytest.mark.parametrize(
    ("body", "error_type", "message"),
    [
        (
            """
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({"jsonrpc": "2.0", "id": "wrong", "result": {}}), flush=True)
""",
            StdioUpstreamError,
            "upstream response id mismatch: fake: expected 0, received id='wrong'",
        ),
        (
            """
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"]}), flush=True)
""",
            StdioUpstreamError,
            "upstream response missing result: fake",
        ),
        (
            """
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    print(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request["id"],
                "error": {
                    "code": -32000,
                    "message": "Auth token missing",
                    "data": {"hint": "run login"},
                },
            }
        ),
        flush=True,
    )
""",
            StdioUpstreamError,
            "upstream error from fake: -32000 Auth token missing data={'hint': 'run login'}",
        ),
        (
            """
import sys

for line in sys.stdin:
    print("[]", flush=True)
""",
            StdioUpstreamError,
            "upstream response must be an object: fake",
        ),
        (
            """
import sys

for line in sys.stdin:
    sys.exit(0)
""",
            StdioUpstreamError,
            "upstream exited without response: fake",
        ),
        (
            """
import time

time.sleep(2)
""",
            StdioUpstreamTimeout,
            "upstream timed out: fake",
        ),
    ],
)
def test_stdio_upstream_rejects_bad_responses(
    tmp_path: Path,
    body: str,
    error_type: type[Exception],
    message: str,
) -> None:
    script = _script(tmp_path, "bad_worker.py", body)
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable, args=[str(script)]),
        runtime_state_dir=tmp_path / "state",
    )

    try:
        timeout_seconds = (
            0
            if error_type is StdioUpstreamTimeout
            else STDIO_HAPPY_PATH_TIMEOUT_SECONDS
        )
        with pytest.raises(error_type, match=message):
            client.call_tool("fake.echo", {}, timeout_seconds=timeout_seconds)
        assert client.health_snapshot()["last_error"] == message
    finally:
        client.stop()

def test_stdio_upstream_rejects_closed_pipes(tmp_path: Path) -> None:
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
    )

    with pytest.raises(StdioUpstreamError, match="upstream stdin closed: fake"):
        client._write_request(cast(Any, ClosedPipeProcess(stdin=None, stdout=io.BytesIO())), {})

    with pytest.raises(StdioUpstreamError, match="upstream stdout closed: fake"):
        client._read_response(
            cast(Any, ClosedPipeProcess(stdin=io.BytesIO(), stdout=None)),
            timeout_seconds=0,
        )

    with pytest.raises(StdioUpstreamError, match="upstream stdin closed: fake"):
        client._write_request(cast(Any, ClosedPipeProcess(stdin=BrokenPipeStdin(), stdout=None)), {})

def test_stdio_write_request_sorts_keys_appends_newline_and_flushes(tmp_path: Path) -> None:
    stdin = RecordingStdin()
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
    )

    client._write_request(
        cast(Any, ClosedPipeProcess(stdin=stdin, stdout=io.BytesIO())),
        {"z": 2, "a": 1},
    )

    assert stdin.writes == [b'{"a": 1, "z": 2}\n']
    assert stdin.flushes == 1

def test_stdio_read_response_reports_non_object_payload_directly(tmp_path: Path) -> None:
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
    )
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"[]\n")
    os.close(write_fd)

    with os.fdopen(read_fd, "rb") as stdout:
        process = ClosedPipeProcess(stdin=io.BytesIO(), stdout=stdout)
        with pytest.raises(
            StdioUpstreamError,
            match="upstream response must be an object: fake",
        ):
            client._read_response(cast(Any, process), timeout_seconds=1)

def test_stdio_jsonrpc_payload_ids_increment_and_omit_absent_params(tmp_path: Path) -> None:
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
    )

    first_id, first_payload = client._jsonrpc_payload("tools/list", None)
    second_id, second_payload = client._jsonrpc_payload("tools/call", {})
    third_id, third_payload = client._jsonrpc_payload(
        "tools/call",
        {"name": "fake.echo"},
    )

    assert first_id == 0
    assert first_payload == {"jsonrpc": "2.0", "id": 0, "method": "tools/list"}
    assert second_id == 1
    assert second_payload == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {},
    }
    assert third_id == 2
    assert third_payload == {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "fake.echo"},
    }
    assert client._next_id == 3

def test_stdio_tools_list_initializes_before_first_roundtrip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
    )
    fake_process = object()
    client._process = cast(Any, fake_process)
    calls: list[str] = []

    monkeypatch.setattr(client, "_start", lambda: calls.append("start"))

    def initialize(process: object, *, timeout_seconds: int) -> None:
        assert process is fake_process
        assert timeout_seconds == 7
        calls.append("initialize")
        client._initialized = True

    def payload(method: str, params: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
        assert method == "tools/list"
        assert params is None
        calls.append("payload")
        return 41, {"jsonrpc": "2.0", "id": 41, "method": method}

    def roundtrip(
        process: object,
        request: dict[str, Any],
        *,
        timeout_seconds: int,
        expected_id: int,
    ) -> dict[str, Any]:
        assert process is fake_process
        assert request == {"jsonrpc": "2.0", "id": 41, "method": "tools/list"}
        assert timeout_seconds == 7
        assert expected_id == 41
        calls.append("roundtrip")
        return {"result": {"tools": []}}

    def result(response: dict[str, Any], request_id: int) -> dict[str, Any]:
        assert response == {"result": {"tools": []}}
        assert request_id == 41
        calls.append("result")
        return {"tools": []}

    monkeypatch.setattr(client, "_initialize_upstream", initialize)
    monkeypatch.setattr(client, "_jsonrpc_payload", payload)
    monkeypatch.setattr(client, "_roundtrip", roundtrip)
    monkeypatch.setattr(client, "_result_from_response", result)

    assert client._jsonrpc_request_locked("tools/list", None, timeout_seconds=7) == {
        "tools": []
    }
    assert calls == ["start", "initialize", "payload", "roundtrip", "result"]

def test_stdio_tool_call_initializes_before_first_roundtrip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable, strict_initialization=True),
        runtime_state_dir=tmp_path / "state",
    )
    fake_process = object()
    client._process = cast(Any, fake_process)
    calls: list[str] = []

    monkeypatch.setattr(client, "_start", lambda: calls.append("start"))

    def initialize(process: object, *, timeout_seconds: int) -> None:
        assert process is fake_process
        assert timeout_seconds == 11
        calls.append("initialize")
        client._initialized = True

    def payload(method: str, params: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
        assert method == "tools/call"
        assert params == {"name": "js", "arguments": {"code": "1 + 1"}}
        calls.append("payload")
        return 42, {"jsonrpc": "2.0", "id": 42, "method": method, "params": params}

    def roundtrip(
        process: object,
        request: dict[str, Any],
        *,
        timeout_seconds: int,
        expected_id: int,
    ) -> dict[str, Any]:
        assert process is fake_process
        assert timeout_seconds == 11
        assert expected_id == 42
        calls.append("roundtrip")
        return {"jsonrpc": "2.0", "id": 42, "result": {"content": []}}

    monkeypatch.setattr(client, "_initialize_upstream", initialize)
    monkeypatch.setattr(client, "_jsonrpc_payload", payload)
    monkeypatch.setattr(client, "_roundtrip", roundtrip)

    result = client._jsonrpc_request_locked(
        "tools/call",
        {"name": "js", "arguments": {"code": "1 + 1"}},
        timeout_seconds=11,
    )

    assert result == {"content": []}
    assert calls == ["start", "initialize", "payload", "roundtrip"]

def test_stdio_read_stdout_line_returns_buffered_line_without_select(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import upstream_stdio

    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
    )
    client._stdout_buffer = b'{"first": true}\n{"second": true}\n'
    monkeypatch.setattr(
        upstream_stdio.select,
        "select",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("select not needed")),
    )

    assert client._read_stdout_line(io.BytesIO(), deadline=time.monotonic()) == b'{"first": true}'
    assert client._stdout_buffer == b'{"second": true}\n'

def test_stdio_read_stdout_line_reads_from_pipe_and_preserves_extra_bytes(
    tmp_path: Path,
) -> None:
    read_fd, write_fd = os.pipe()
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
    )
    os.write(write_fd, b'{"first": true}\n{"second": true}')
    os.close(write_fd)

    with os.fdopen(read_fd, "rb") as stdout:
        assert (
            client._read_stdout_line(stdout, deadline=time.monotonic() + 1)
            == b'{"first": true}'
        )
        assert client._stdout_buffer == b'{"second": true}'

def test_stdio_read_stdout_line_passes_remaining_deadline_to_select(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import upstream_stdio

    class Readable:
        def fileno(self) -> int:
            return 41

    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
    )
    stdout = Readable()

    def fake_select(
        readers: list[object],
        _writers: list[object],
        _errors: list[object],
        timeout: float,
    ) -> tuple[list[object], list[object], list[object]]:
        assert readers == [stdout]
        assert timeout == pytest.approx(2.5)
        return [stdout], [], []

    def fake_read(fd: int, size: int) -> bytes:
        assert fd == 41
        assert size == 4096
        return b'{"ok": true}\n{"next": true}'

    monkeypatch.setattr(upstream_stdio.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(upstream_stdio.select, "select", fake_select)
    monkeypatch.setattr(upstream_stdio.os, "read", fake_read)

    assert client._read_stdout_line(cast(Any, stdout), deadline=12.5) == b'{"ok": true}'
    assert client._stdout_buffer == b'{"next": true}'

def test_stdio_read_stdout_line_clamps_expired_deadline_to_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import upstream_stdio

    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
    )

    def fake_select(
        _readers: list[object],
        _writers: list[object],
        _errors: list[object],
        timeout: float,
    ) -> tuple[list[object], list[object], list[object]]:
        assert timeout == 0
        return [], [], []

    monkeypatch.setattr(upstream_stdio.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(upstream_stdio.select, "select", fake_select)

    with pytest.raises(StdioUpstreamTimeout, match="upstream timed out: fake"):
        client._read_stdout_line(FilenoOnly(), deadline=9.0)

def test_stdio_read_stdout_line_appends_chunks_before_splitting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import upstream_stdio

    class Readable:
        def fileno(self) -> int:
            return 41

    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
    )
    chunks = iter([b'{"ok": ', b'true}\n{"next": true}'])

    monkeypatch.setattr(upstream_stdio.select, "select", lambda *_args, **_kwargs: ([Readable()], [], []))
    monkeypatch.setattr(upstream_stdio.os, "read", lambda _fd, _size: next(chunks))

    assert client._read_stdout_line(cast(Any, Readable()), deadline=time.monotonic() + 1) == b'{"ok": true}'
    assert client._stdout_buffer == b'{"next": true}'

def test_stdio_read_stdout_line_times_out_when_pipe_is_not_readable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import upstream_stdio

    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
    )
    monkeypatch.setattr(upstream_stdio.select, "select", lambda *_args, **_kwargs: ([], [], []))

    with pytest.raises(StdioUpstreamTimeout, match="upstream timed out: fake"):
        client._read_stdout_line(FilenoOnly(), deadline=time.monotonic() + 1)

@pytest.mark.error_simulation
def test_stdio_read_response_reports_exited_process_when_pipe_is_not_readable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import upstream_stdio

    class Stdout:
        def fileno(self) -> int:
            return 41

    class ExitedProcess:
        stdout = Stdout()

        def poll(self) -> int:
            return 0

    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
    )
    monkeypatch.setattr(upstream_stdio.select, "select", lambda *_args, **_kwargs: ([], [], []))

    with pytest.raises(StdioUpstreamError, match="upstream exited without response: fake"):
        client._read_response(cast(Any, ExitedProcess()), timeout_seconds=1)

@pytest.mark.error_simulation
def test_stdio_read_stdout_line_reports_eof_before_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import upstream_stdio

    class Readable:
        def fileno(self) -> int:
            return 43

    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
    )
    reads = 0

    def read_eof(fd: int, size: int) -> bytes:
        nonlocal reads
        assert fd == 43
        assert size == 4096
        reads += 1
        if reads > 1:
            raise AssertionError("EOF must stop after one read")
        return b""

    monkeypatch.setattr(upstream_stdio.select, "select", lambda *_args, **_kwargs: ([Readable()], [], []))
    monkeypatch.setattr(upstream_stdio.os, "read", read_eof)

    with pytest.raises(StdioUpstreamError, match="upstream exited without response: fake"):
        client._read_stdout_line(cast(Any, Readable()), deadline=time.monotonic() + 1)

    assert reads == 1

def test_stdio_read_stdout_line_reports_eof_without_spinning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import upstream_stdio

    class Readable:
        def fileno(self) -> int:
            return 41

    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
    )
    reads = 0

    def fake_read(fd: int, size: int) -> bytes:
        nonlocal reads
        assert fd == 41
        assert size == 4096
        reads += 1
        if reads > 1:
            raise AssertionError("EOF must stop after one read")
        return b""

    monkeypatch.setattr(upstream_stdio.select, "select", lambda *_args, **_kwargs: ([Readable()], [], []))
    monkeypatch.setattr(upstream_stdio.os, "read", fake_read)

    with pytest.raises(StdioUpstreamError, match="upstream exited without response: fake"):
        client._read_stdout_line(cast(Any, Readable()), deadline=time.monotonic() + 1)

    assert reads == 1

@pytest.mark.error_simulation
def test_stdio_upstream_stop_handles_missing_and_stubborn_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import upstream_stdio

    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
    )
    client.stop()
    process = StubbornProcess()
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(upstream_stdio, "_process_group_id", lambda _pid: process.pid)
    monkeypatch.setattr(upstream_stdio.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    client._process = cast(Any, process)

    client.stop()

    assert process.waits == 2
    assert signals == [
        (process.pid, upstream_stdio.signal.SIGTERM),
        (process.pid, upstream_stdio.signal.SIGKILL),
        (process.pid, upstream_stdio.signal.SIGKILL),
    ]
    assert client._process is None

@pytest.mark.error_simulation
def test_stdio_upstream_stop_uses_process_group_and_cleanup_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import upstream_stdio

    events: list[dict[str, object]] = []
    close_calls: list[bool] = []
    waited_groups: list[int | None] = []
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
        event_logger=lambda event, upstream, fields: events.append(
            {"event": event, "upstream": upstream} | fields
        ),
    )
    process = StubbornProcess()
    drainer = RecordingDrainer()
    client._process = cast(Any, process)
    client._stderr_drainer = drainer
    metadata_path = tmp_path / "fake.json"
    metadata_path.write_text("metadata", encoding="utf-8")
    client._process_metadata_path = metadata_path

    monkeypatch.setattr(upstream_stdio, "_process_group_id", lambda pid: 1234)
    monkeypatch.setattr(upstream_stdio, "_signal_process_group", lambda _pid, _sig: None)
    monkeypatch.setattr(
        upstream_stdio,
        "_wait_for_process_group_stop",
        lambda pgid: waited_groups.append(pgid) or (777,),
    )
    monkeypatch.setattr(
        upstream_stdio,
        "_close_process_pipes",
        lambda _process, *, include_stderr: close_calls.append(include_stderr),
    )

    assert client.stop() == (777,)

    assert process.wait_timeouts == [
        STOP_TIMEOUT_SECONDS,
        max(STOP_TIMEOUT_SECONDS, KILL_WAIT_SECONDS),
    ]
    assert waited_groups == [1234]
    assert close_calls == [False, True]
    assert drainer.join_timeouts == [KILL_WAIT_SECONDS]
    assert client._stderr_drainer is None
    assert metadata_path.exists()
    assert client._process_metadata_path == metadata_path
    assert events == [
        {
            "event": "upstream.kill",
            "upstream": "fake",
            "signal": "SIGKILL",
            "reason": "stop_timeout",
        },
        {
            "event": "upstream.kill",
            "upstream": "fake",
            "signal": "SIGKILL",
            "reason": "final_cleanup",
        },
        {"event": "upstream.stop", "upstream": "fake", "state": "stopped"},
    ]

@pytest.mark.error_simulation
def test_stdio_upstream_stop_handles_process_group_signal_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import upstream_stdio

    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
    )
    process = StubbornProcess()
    attempts = 0

    def fail_signal(_pid: int, _sig: signal.Signals) -> None:
        nonlocal attempts
        attempts += 1
        raise PermissionError

    monkeypatch.setattr(upstream_stdio, "_process_group_id", lambda _pid: process.pid)
    monkeypatch.setattr(upstream_stdio.os, "killpg", fail_signal)
    client._process = cast(Any, process)

    client.stop()

    assert attempts == 3
    assert process.waits == 2
    assert client._process is None
