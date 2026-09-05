import io
from pathlib import Path
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

def test_stdio_upstream_rejects_bad_tools_list_response(tmp_path: Path) -> None:
    script = _script(
        tmp_path,
        "bad_tools_worker.py",
        """
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    if request["method"] == "initialize":
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "protocolVersion": request["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "0.0.1"},
            },
        }), flush=True)
        continue
    if request["method"] == "notifications/initialized":
        continue
    print(json.dumps({
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": {"tools": "bad"}
    }), flush=True)
""",
    )
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable, args=[str(script)]),
        runtime_state_dir=tmp_path / "state",
    )

    try:
        with pytest.raises(
            StdioUpstreamError,
            match="upstream tools/list response invalid: fake",
        ):
            client.list_tools(timeout_seconds=1)
        assert (
            client.health_snapshot()["last_error"]
            == "upstream tools/list response invalid: fake"
        )
    finally:
        client.stop()

def test_stdio_upstream_rejects_non_object_tools_list_entries(tmp_path: Path) -> None:
    script = _script(
        tmp_path,
        "bad_tools_entry_worker.py",
        """
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    if request["method"] == "initialize":
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "protocolVersion": request["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "0.0.1"},
            },
        }), flush=True)
        continue
    if request["method"] == "notifications/initialized":
        continue
    print(json.dumps({
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": {"tools": [{"name": "good"}, "bad"]}
    }), flush=True)
""",
    )
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable, args=[str(script)]),
        runtime_state_dir=tmp_path / "state",
    )

    try:
        with pytest.raises(
            StdioUpstreamError,
            match="upstream tools/list response invalid: fake",
        ):
            client.list_tools(timeout_seconds=1)
    finally:
        client.stop()

def test_stdio_upstream_reports_health_snapshot(tmp_path: Path) -> None:
    script = _script(
        tmp_path,
        "health_worker.py",
        """
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {}}), flush=True)
""",
    )
    upstream = UpstreamConfig(name="fake", command=sys.executable, args=[str(script)])
    client = StdioUpstreamProcess(upstream, runtime_state_dir=tmp_path / "state")

    assert client.health_snapshot() == {
        "state": "configured",
        "pid": None,
        "cpu_percent": None,
        "memory_mb": None,
        "restarts": 0,
        "last_error": None,
    }
    try:
        client.call_tool("fake.echo", {}, timeout_seconds=STDIO_HAPPY_PATH_TIMEOUT_SECONDS)
        snapshot = client.health_snapshot()

        assert client.status == "running"
        assert snapshot["state"] == "running"
        assert isinstance(snapshot["pid"], int)
        assert isinstance(snapshot["cpu_percent"], float)
        assert snapshot["memory_mb"] is None or isinstance(snapshot["memory_mb"], float)
        assert snapshot["restarts"] == 0
        assert snapshot["last_error"] is None
    finally:
        client.stop()

def test_stdio_upstream_health_samples_the_running_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import upstream_stdio

    sampled_cpu_groups: list[int] = []
    sampled_memory_groups: list[int] = []
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
    )
    client._process = cast(Any, RunningProcessForHealth())
    monkeypatch.setattr(upstream_stdio.os, "getpgid", lambda _pid: 1234)
    monkeypatch.setattr(
        upstream_stdio,
        "sample_process_group_cpu_percent",
        lambda pgid: sampled_cpu_groups.append(pgid) or 12.5,
    )
    monkeypatch.setattr(
        upstream_stdio,
        "sample_process_group_memory_mb",
        lambda pgid: sampled_memory_groups.append(pgid) or 64.0,
    )

    assert client.health_snapshot() == {
        "state": "running",
        "pid": 999998,
        "cpu_percent": 12.5,
        "memory_mb": 64.0,
        "restarts": 0,
        "last_error": None,
    }
    assert sampled_cpu_groups == [1234]
    assert sampled_memory_groups == [1234]

def test_stdio_upstream_reports_exited_status_and_restarts(tmp_path: Path) -> None:
    script = _script(
        tmp_path,
        "one_shot_worker.py",
        """
import json
import sys

request = json.loads(sys.stdin.readline())
print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {}}), flush=True)
""",
    )
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable, args=[str(script)]),
        runtime_state_dir=tmp_path / "state",
    )

    try:
        client.call_tool("fake.echo", {}, timeout_seconds=STDIO_HAPPY_PATH_TIMEOUT_SECONDS)
        process = cast(subprocess.Popen[bytes], client._process)
        process.wait(timeout=1)

        assert client.status == "exited"

        client.call_tool("fake.echo", {}, timeout_seconds=STDIO_HAPPY_PATH_TIMEOUT_SECONDS)

        assert client.health_snapshot()["restarts"] == 1
    finally:
        client.stop()

def test_stdio_upstream_ensure_running_restarts_exited_process(tmp_path: Path) -> None:
    script = _script(
        tmp_path,
        "long_running_worker.py",
        """
import time

time.sleep(30)
""",
    )
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable, args=[str(script)]),
        runtime_state_dir=tmp_path / "state",
    )

    try:
        client.ensure_running()
        first_process = cast(subprocess.Popen[bytes], client._process)
        first_process.terminate()
        first_process.wait(timeout=2)

        client.ensure_running()

        assert client.health_snapshot()["state"] == "running"
        assert client.health_snapshot()["restarts"] == 1
    finally:
        client.stop()

def test_stdio_upstream_start_uses_subprocess_contract_and_session_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import upstream_stdio

    calls: list[dict[str, object]] = []

    class StartedProcess:
        pid = 999998
        stdin = io.BytesIO()
        stdout = io.BytesIO()
        stderr = io.BytesIO()

        def poll(self) -> None:
            return None

    def fake_popen(args: list[str], **kwargs: object) -> StartedProcess:
        calls.append({"args": args, **kwargs})
        return StartedProcess()

    monkeypatch.setattr(upstream_stdio.subprocess, "Popen", fake_popen)
    monkeypatch.setenv("CONFIGURED_VALUE", "configured")
    client = StdioUpstreamProcess(
        UpstreamConfig(
            name="fake",
            command="/bin/fake",
            args=["--serve"],
            working_dir=tmp_path / "work",
            env={"STATIC_ENV": "CONFIGURED_VALUE"},
            session_env={"PROJECT_DIR": "client_cwd"},
        ),
        runtime_state_dir=tmp_path / "state",
        session_context={"client_cwd": str(tmp_path / "project")},
    )
    client._initialized = True
    client._stdout_buffer = b'{"stale": true}\n'

    client.ensure_running()

    assert len(calls) == 1
    call = calls[0]
    env = cast(dict[str, str], call["env"])
    assert call["args"] == ["/bin/fake", "--serve"]
    assert call["cwd"] == tmp_path / "work"
    assert call["stdin"] is subprocess.PIPE
    assert call["stdout"] is subprocess.PIPE
    assert call["stderr"] is subprocess.PIPE
    assert call["start_new_session"] is True
    assert env["STATIC_ENV"] == "configured"
    assert env["PROJECT_DIR"] == str(tmp_path / "project")
    assert env["MCP_BROKER_CLIENT_CWD"] == str(tmp_path / "project")
    assert env["MCP_BROKER_UPSTREAM_STATE_DIR"] == str(tmp_path / "state" / "upstreams" / "fake")
    assert (tmp_path / "state" / "upstreams" / "fake").is_dir()
    assert client._initialized is False
    assert client._stdout_buffer == b""

def test_stdio_upstream_start_defaults_missing_client_cwd_to_empty_string(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import upstream_stdio

    calls: list[dict[str, object]] = []

    class StartedProcess:
        pid = 999998
        stdin = io.BytesIO()
        stdout = io.BytesIO()
        stderr = io.BytesIO()

        def poll(self) -> None:
            return None

    def fake_popen(args: list[str], **kwargs: object) -> StartedProcess:
        calls.append({"args": args, **kwargs})
        return StartedProcess()

    monkeypatch.setattr(upstream_stdio.subprocess, "Popen", fake_popen)
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command="/bin/fake"),
        runtime_state_dir=tmp_path / "state",
    )

    client.ensure_running()

    env = cast(dict[str, str], calls[0]["env"])
    assert env["MCP_BROKER_CLIENT_CWD"] == ""

def test_stdio_upstream_start_fails_when_stderr_pipe_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import upstream_stdio

    class StartedProcess:
        pid = 999998
        stdin = io.BytesIO()
        stdout = io.BytesIO()
        stderr = None

        def poll(self) -> None:
            return None

    def fake_popen(args: list[str], **kwargs: object) -> StartedProcess:
        return StartedProcess()

    monkeypatch.setattr(upstream_stdio.subprocess, "Popen", fake_popen)
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command="/bin/fake"),
        runtime_state_dir=tmp_path / "state",
    )

    with pytest.raises(StdioUpstreamError, match="upstream stderr closed: fake"):
        client.ensure_running()

    assert client._stderr_drainer is None

def test_stdio_upstream_restart_emits_restart_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import upstream_stdio

    events: list[dict[str, object]] = []
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
        event_logger=lambda event, upstream, fields: events.append(
            {"event": event, "upstream": upstream} | fields
        ),
    )
    client._process = cast(Any, ExitedProcessWithPipes())
    monkeypatch.setattr(
        upstream_stdio.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("spawn failed")),
    )

    with pytest.raises(StdioUpstreamError, match="upstream failed to start: fake: spawn failed"):
        client._start()

    assert {
        "event": "upstream.restart",
        "upstream": "fake",
        "restart_count": 1,
    } in events
    assert {
        "event": "upstream.backoff",
        "upstream": "fake",
        "state": "backoff",
    } in events

def test_stdio_upstream_restart_preserves_incremental_restart_count_after_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import upstream_stdio

    events: list[dict[str, object]] = []
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
        event_logger=lambda event, upstream, fields: events.append(
            {"event": event, "upstream": upstream} | fields
        ),
    )
    drainer = RecordingDrainer()
    client._process = cast(Any, ExitedProcessWithPipes())
    client._stderr_drainer = drainer
    client._restart_count = 4
    monkeypatch.setattr(
        upstream_stdio.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("spawn failed")),
    )

    with pytest.raises(StdioUpstreamError, match="upstream failed to start: fake: spawn failed"):
        client._start()

    assert drainer.join_timeouts == [KILL_WAIT_SECONDS]
    assert client._stderr_drainer is None
    assert client.health_snapshot()["restarts"] == 5
    assert {
        "event": "upstream.restart",
        "upstream": "fake",
        "restart_count": 5,
    } in events

def test_stdio_upstream_ensure_running_maps_unexpected_restart_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import upstream_stdio

    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
    )
    monkeypatch.setattr(
        upstream_stdio.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("spawn bug")),
    )

    with pytest.raises(StdioUpstreamError, match="upstream failed to restart: fake: spawn bug"):
        client.ensure_running()

    assert client.health_snapshot()["last_error"] == "spawn bug"

def test_stdio_upstream_ensure_running_clears_prior_error_on_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import upstream_stdio

    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
    )
    client._last_error = "old failure"
    monkeypatch.setattr(upstream_stdio.StdioUpstreamProcess, "_start", lambda self: None)

    client.ensure_running()

    assert client.health_snapshot()["last_error"] is None

def test_stdio_upstream_ensure_running_records_start_failure(tmp_path: Path) -> None:
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=str(tmp_path / "missing-command")),
        runtime_state_dir=tmp_path / "state",
    )

    with pytest.raises(StdioUpstreamError, match="upstream failed to start: fake"):
        client.ensure_running()

    assert "upstream failed to start: fake" in str(client.health_snapshot()["last_error"])

def test_stdio_upstream_maps_start_failure(tmp_path: Path) -> None:
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=str(tmp_path / "missing-command")),
        runtime_state_dir=tmp_path / "state",
    )

    with pytest.raises(StdioUpstreamError, match="upstream failed to start: fake"):
        client.call_tool("fake.echo", {}, timeout_seconds=1)

    assert "upstream failed to start: fake" in str(client.health_snapshot()["last_error"])

@pytest.mark.error_simulation
def test_stdio_upstream_health_handles_stale_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import upstream_stdio

    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
    )
    client._process = cast(Any, RunningProcessForHealth())
    monkeypatch.setattr(
        upstream_stdio.os,
        "getpgid",
        lambda _pid: (_ for _ in ()).throw(ProcessLookupError),
    )

    assert client.health_snapshot() == {
        "state": "exited",
        "pid": None,
        "cpu_percent": None,
        "memory_mb": None,
        "restarts": 0,
        "last_error": None,
    }
