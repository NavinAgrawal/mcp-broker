import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any, cast

import pytest

from mcp_broker import __version__
from mcp_broker.config import UpstreamConfig
from mcp_broker.protocol import SUPPORTED_PROTOCOL_VERSIONS
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


def test_stdio_upstream_injects_configured_request_meta(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("request-token\n", encoding="utf-8")
    script = _script(
        tmp_path,
        "meta_worker.py",
        """
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": {
            "meta": request["params"].get("_meta", {}),
        },
    }), flush=True)
""",
    )
    upstream = UpstreamConfig(
        name="notebook",
        command=sys.executable,
        args=[str(script)],
        env_files={"NLMCP_AUTH_TOKEN": token_file},
        request_meta={"authToken": "NLMCP_AUTH_TOKEN"},
    )
    client = StdioUpstreamProcess(upstream, runtime_state_dir=tmp_path / "runtime-state")

    try:
        result = client.call_tool(
            "notebook.list_notebooks",
            {},
            timeout_seconds=STDIO_HAPPY_PATH_TIMEOUT_SECONDS,
        )
    finally:
        client.stop()

    assert result == {"meta": {"authToken": "request-token"}}


def test_stdio_upstream_request_meta_reads_configured_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _script(
        tmp_path,
        "env_meta_worker.py",
        """
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": {"meta": request["params"].get("_meta", {})},
    }), flush=True)
""",
    )
    monkeypatch.setenv("AUTH_SOURCE", "env-token")
    client = StdioUpstreamProcess(
        UpstreamConfig(
            name="fake",
            command=sys.executable,
            args=[str(script)],
            env={"AUTH_TARGET": "AUTH_SOURCE"},
            request_meta={"authToken": "AUTH_TARGET"},
        ),
        runtime_state_dir=tmp_path / "runtime-state",
    )

    try:
        result = client.call_tool(
            "fake.echo",
            {},
            timeout_seconds=STDIO_HAPPY_PATH_TIMEOUT_SECONDS,
        )
    finally:
        client.stop()

    assert result == {"meta": {"authToken": "env-token"}}


def test_stdio_upstream_injects_configured_session_environment(tmp_path: Path) -> None:
    script = _script(
        tmp_path,
        "session_env_worker.py",
        """
import json
import os
import sys

for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": {
            "client_cwd": os.environ["MCP_BROKER_CLIENT_CWD"],
            "project_dir": os.environ["PROJECT_DIR"],
        },
    }), flush=True)
""",
    )
    upstream = UpstreamConfig(
        name="session-tool",
        command=sys.executable,
        args=[str(script)],
        session_env={"PROJECT_DIR": "client_cwd"},
    )
    client = StdioUpstreamProcess(
        upstream,
        runtime_state_dir=tmp_path / "runtime-state",
        session_context={"client_cwd": str(tmp_path / "client-project")},
    )

    try:
        result = client.call_tool(
            "session.echo",
            {},
            timeout_seconds=STDIO_HAPPY_PATH_TIMEOUT_SECONDS,
        )
    finally:
        client.stop()

    assert result == {
        "client_cwd": str(tmp_path / "client-project"),
        "project_dir": str(tmp_path / "client-project"),
    }


def test_stdio_upstream_lists_tools(tmp_path: Path) -> None:
    script = _script(
        tmp_path,
        "tools_worker.py",
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
    assert request["method"] == "tools/list"
    print(json.dumps({
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": {
            "tools": [
                {"name": "echo", "description": "Echo input"}
            ]
        },
    }), flush=True)
""",
    )
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable, args=[str(script)]),
        runtime_state_dir=tmp_path / "state",
    )

    try:
        assert client.list_tools(timeout_seconds=STDIO_HAPPY_PATH_TIMEOUT_SECONDS) == [
            {"name": "echo", "description": "Echo input"}
        ]
    finally:
        client.stop()


def test_stdio_upstream_clears_last_error_after_successful_tools_list(tmp_path: Path) -> None:
    script = _script(
        tmp_path,
        "tools_success_worker.py",
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
        "result": {"tools": [{"name": "echo"}]},
    }), flush=True)
""",
    )
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable, args=[str(script)]),
        runtime_state_dir=tmp_path / "state",
    )
    client._last_error = "old failure"

    try:
        assert client.list_tools(timeout_seconds=STDIO_HAPPY_PATH_TIMEOUT_SECONDS) == [
            {"name": "echo"}
        ]
        assert client.health_snapshot()["last_error"] is None
    finally:
        client.stop()


def test_stdio_upstream_emits_tools_list_and_initialization_contract(
    tmp_path: Path,
) -> None:
    requests_path = tmp_path / "requests.jsonl"
    script = _script(
        tmp_path,
        "handshake_worker.py",
        f"""
import json
from pathlib import Path
import sys

requests_path = Path({str(requests_path)!r})

for line in sys.stdin:
    request = json.loads(line)
    with requests_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(request, sort_keys=True) + "\\n")
    if request["method"] == "initialize":
        print(json.dumps({{
            "jsonrpc": "2.0",
            "method": "notifications/progress",
        }}), flush=True)
        print(json.dumps({{
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {{
                "protocolVersion": request["params"]["protocolVersion"],
                "capabilities": {{"tools": {{}}}},
                "serverInfo": {{"name": "fake", "version": "0.0.1"}},
            }},
        }}), flush=True)
        continue
    if request["method"] == "notifications/initialized":
        continue
    print(json.dumps({{
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": {{"tools": [{{"name": "echo"}}]}},
    }}), flush=True)
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
        assert client.list_tools(timeout_seconds=STDIO_HAPPY_PATH_TIMEOUT_SECONDS) == [
            {"name": "echo"}
        ]
        assert client.list_tools(timeout_seconds=STDIO_HAPPY_PATH_TIMEOUT_SECONDS) == [
            {"name": "echo"}
        ]
    finally:
        client.stop()

    requests = [
        json.loads(line) for line in requests_path.read_text(encoding="utf-8").splitlines()
    ]
    assert events[0] == {
        "event": "upstream.call",
        "upstream": "fake",
        "method": "tools/list",
        "timeout_seconds": STDIO_HAPPY_PATH_TIMEOUT_SECONDS,
    }
    assert requests[0] == {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": SUPPORTED_PROTOCOL_VERSIONS[0],
            "capabilities": {},
            "clientInfo": {"name": "mcp-broker", "version": __version__},
        },
    }
    assert requests[1] == {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    }
    assert requests[2] == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
    }
    assert requests[3] == {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
    }
    assert [request["method"] for request in requests].count("initialize") == 1


def test_stdio_upstream_retries_tool_call_after_not_initialized_with_same_request_contract(
    tmp_path: Path,
) -> None:
    requests_path = tmp_path / "retry-requests.jsonl"
    script = _script(
        tmp_path,
        "retry_worker.py",
        f"""
import json
from pathlib import Path
import sys

requests_path = Path({str(requests_path)!r})
tool_calls = 0

for line in sys.stdin:
    request = json.loads(line)
    with requests_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(request, sort_keys=True) + "\\n")
    if request["method"] == "tools/call":
        tool_calls += 1
        if tool_calls == 1:
            print(json.dumps({{
                "jsonrpc": "2.0",
                "id": request["id"],
                "error": {{"code": -32002, "message": "Server not initialized"}},
            }}), flush=True)
            continue
        print(json.dumps({{"jsonrpc": "2.0", "method": "notifications/progress"}}), flush=True)
        print(json.dumps({{
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {{"ok": request["params"]}},
        }}), flush=True)
        continue
    if request["method"] == "initialize":
        print(json.dumps({{
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {{
                "protocolVersion": request["params"]["protocolVersion"],
                "capabilities": {{"tools": {{}}}},
                "serverInfo": {{"name": "fake", "version": "0.0.1"}},
            }},
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
        ) == {
            "ok": {
                "name": "fake.echo",
                "arguments": {"value": "hello"},
            }
        }
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
            "method": "initialize",
            "params": {
                "protocolVersion": SUPPORTED_PROTOCOL_VERSIONS[0],
                "capabilities": {},
                "clientInfo": {"name": "mcp-broker", "version": __version__},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "fake.echo",
                "arguments": {"value": "hello"},
            },
        },
    ]


def test_stdio_upstream_logs_tools_list_timeout_event(tmp_path: Path) -> None:
    script = _script(
        tmp_path,
        "slow_tools_worker.py",
        """
import json
import sys
import time

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
    time.sleep(2)
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
        with pytest.raises(StdioUpstreamTimeout, match="upstream timed out: fake"):
            client.list_tools(timeout_seconds=1)
    finally:
        client.stop()

    assert {
        "event": "upstream.timeout",
        "upstream": "fake",
        "method": "tools/list",
        "timeout_seconds": 1,
    } in events
    assert client.health_snapshot()["last_error"] == "upstream timed out: fake"


def test_stdio_upstream_logs_tool_call_timeout_event_and_restart(tmp_path: Path) -> None:
    script = _script(
        tmp_path,
        "slow_call_worker.py",
        """
import sys
import time

for _line in sys.stdin:
    time.sleep(2)
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
        with pytest.raises(StdioUpstreamTimeout, match="upstream timed out: fake"):
            client.call_tool("fake.echo", {"value": "late"}, timeout_seconds=1)
    finally:
        client.stop()

    assert {
        "event": "upstream.timeout",
        "upstream": "fake",
        "method": "tools/call",
        "tool_name": "fake.echo",
        "timeout_seconds": 1,
    } in events
    assert {
        "event": "upstream.restart",
        "upstream": "fake",
        "restart_count": 1,
        "reason": "timeout",
    } in events
    assert client.health_snapshot()["last_error"] == "upstream timed out: fake"


def test_stdio_upstream_timeout_reset_clears_protocol_state(tmp_path: Path) -> None:
    events: list[dict[str, object]] = []
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable),
        runtime_state_dir=tmp_path / "state",
        event_logger=lambda event, upstream, fields: events.append(
            {"event": event, "upstream": upstream} | fields
        ),
    )
    client._initialized = True
    client._stdout_buffer = b'{"late": true}\n'
    client._restart_count = 2
    client._last_error = "upstream timed out: fake"

    client._reset_after_timeout_locked()

    assert client._initialized is False
    assert client._stdout_buffer == b""
    assert client.health_snapshot()["restarts"] == 3
    assert client.health_snapshot()["last_error"] == "upstream timed out: fake"
    assert events == [
        {
            "event": "upstream.restart",
            "upstream": "fake",
            "restart_count": 3,
            "reason": "timeout",
        }
    ]


def test_stdio_upstream_restarts_after_timeout_before_next_request(tmp_path: Path) -> None:
    script = _script(
        tmp_path,
        "late_response_worker.py",
        """
import json
import os
from pathlib import Path
import sys
import time

marker = Path(os.environ["MCP_BROKER_UPSTREAM_STATE_DIR"]) / "timed-out-once"

for line in sys.stdin:
    request = json.loads(line)
    if not marker.exists():
        marker.write_text("yes", encoding="utf-8")
        time.sleep(1.25)
    print(json.dumps({
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": {
            "content": [{"type": "text", "text": request["params"]["arguments"]["value"]}],
        },
    }), flush=True)
""",
    )
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable, args=[str(script)]),
        runtime_state_dir=tmp_path / "state",
    )

    try:
        with pytest.raises(StdioUpstreamTimeout, match="upstream timed out: fake"):
            client.call_tool("fake.echo", {"value": "first"}, timeout_seconds=1)

        result = client.call_tool(
            "fake.echo",
            {"value": "second"},
            timeout_seconds=STDIO_HAPPY_PATH_TIMEOUT_SECONDS,
        )
    finally:
        client.stop()

    assert result == {"content": [{"type": "text", "text": "second"}]}
    assert client.health_snapshot()["restarts"] == 1


def test_stdio_upstream_initializes_before_first_tools_list_request(tmp_path: Path) -> None:
    script = _script(
        tmp_path,
        "strict_initialization_worker.py",
        """
import json
import sys

initialized = False
poisoned = False

for line in sys.stdin:
    request = json.loads(line)
    if not initialized and request["method"] != "initialize":
        poisoned = True
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": request.get("id"),
            "error": {
                "code": -32600,
                "message": "Received request before initialization was complete",
            },
        }), flush=True)
        continue
    if request["method"] == "initialize":
        initialized = True
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "protocolVersion": request["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "0.0.1"},
            },
        }), flush=True)
    elif request["method"] == "notifications/initialized":
        continue
    elif poisoned:
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": request["id"],
            "error": {
                "code": -32600,
                "message": "Received request before initialization was complete",
            },
        }), flush=True)
    else:
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {"first_method_after_init": request["method"], "tools": [{"name": "strict"}]},
        }), flush=True)
""",
    )
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable, args=[str(script)]),
        runtime_state_dir=tmp_path / "state",
    )

    try:
        assert client.list_tools(timeout_seconds=STDIO_HAPPY_PATH_TIMEOUT_SECONDS) == [
            {"name": "strict"}
        ]
    finally:
        client.stop()


def test_stdio_upstream_ignores_notifications_while_waiting_for_response(
    tmp_path: Path,
) -> None:
    script = _script(
        tmp_path,
        "notification_worker.py",
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
    elif request["method"] == "notifications/initialized":
        print(json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/message",
            "params": {"level": "info", "data": "ready"},
        }), flush=True)
    elif request["method"] == "tools/list":
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {"tools": [{"name": "search_graph"}]},
        }), flush=True)
""",
    )
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable, args=[str(script)]),
        runtime_state_dir=tmp_path / "state",
    )

    try:
        assert client.list_tools(timeout_seconds=STDIO_HAPPY_PATH_TIMEOUT_SECONDS) == [
            {"name": "search_graph"}
        ]
    finally:
        client.stop()


def test_stdio_upstream_initializes_when_required_before_listing_tools(tmp_path: Path) -> None:
    script = _script(
        tmp_path,
        "initializing_tools_worker.py",
        """
import json
import sys

initialized = False

for line in sys.stdin:
    request = json.loads(line)
    if request["method"] == "initialize":
        initialized = True
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "protocolVersion": request["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "0.0.1"},
            },
        }), flush=True)
    elif request["method"] == "notifications/initialized":
        continue
    elif not initialized:
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": request["id"],
            "error": {"code": -32002, "message": "Server not initialized"},
        }), flush=True)
    else:
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {"tools": [{"name": "echo"}]},
        }), flush=True)
""",
    )
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable, args=[str(script)]),
        runtime_state_dir=tmp_path / "state",
    )

    try:
        assert client.list_tools(timeout_seconds=STDIO_HAPPY_PATH_TIMEOUT_SECONDS) == [
            {"name": "echo"}
        ]
    finally:
        client.stop()


def test_stdio_upstream_initializes_when_server_reports_initialization_incomplete(
    tmp_path: Path,
) -> None:
    script = _script(
        tmp_path,
        "initialization_incomplete_tools_worker.py",
        """
import json
import sys

initialized = False

for line in sys.stdin:
    request = json.loads(line)
    if request["method"] == "initialize":
        initialized = True
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "protocolVersion": request["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "0.0.1"},
            },
        }), flush=True)
    elif request["method"] == "notifications/initialized":
        continue
    elif not initialized:
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": request["id"],
            "error": {
                "code": -32600,
                "message": "Received request before initialization was complete",
            },
        }), flush=True)
    else:
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {"tools": [{"name": "kb_health"}]},
        }), flush=True)
""",
    )
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable, args=[str(script)]),
        runtime_state_dir=tmp_path / "state",
    )

    try:
        assert client.list_tools(timeout_seconds=STDIO_HAPPY_PATH_TIMEOUT_SECONDS) == [
            {"name": "kb_health"}
        ]
    finally:
        client.stop()


def test_stdio_upstream_retries_tool_call_after_initialization_error(tmp_path: Path) -> None:
    script = _script(
        tmp_path,
        "initializing_call_worker.py",
        """
import json
import sys

initialized = False

for line in sys.stdin:
    request = json.loads(line)
    if request["method"] == "initialize":
        initialized = True
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "protocolVersion": request["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "0.0.1"},
            },
        }), flush=True)
    elif request["method"] == "notifications/initialized":
        continue
    elif not initialized:
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": request["id"],
            "error": {"code": -32002, "message": "Server not initialized"},
        }), flush=True)
    else:
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "content": [{"type": "text", "text": request["params"]["name"]}],
            },
        }), flush=True)
""",
    )
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable, args=[str(script)]),
        runtime_state_dir=tmp_path / "state",
    )

    try:
        assert client.call_tool(
            "fake.echo",
            {},
            timeout_seconds=STDIO_HAPPY_PATH_TIMEOUT_SECONDS,
        ) == {
            "content": [{"type": "text", "text": "fake.echo"}],
        }
    finally:
        client.stop()


def test_stdio_upstream_retries_tool_call_after_invalid_params_initialization_error(
    tmp_path: Path,
) -> None:
    script = _script(
        tmp_path,
        "invalid_params_before_init_worker.py",
        """
import json
import sys

initialized = False

for line in sys.stdin:
    request = json.loads(line)
    if request["method"] == "initialize":
        initialized = True
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "protocolVersion": request["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "0.0.1"},
            },
        }), flush=True)
    elif request["method"] == "notifications/initialized":
        continue
    elif not initialized:
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": request["id"],
            "error": {"code": -32602, "message": "Invalid request parameters", "data": ""},
        }), flush=True)
    else:
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "content": [{"type": "text", "text": request["params"]["name"]}],
            },
        }), flush=True)
""",
    )
    client = StdioUpstreamProcess(
        UpstreamConfig(name="fake", command=sys.executable, args=[str(script)]),
        runtime_state_dir=tmp_path / "state",
    )

    try:
        assert client.call_tool(
            "fake.health",
            {},
            timeout_seconds=STDIO_HAPPY_PATH_TIMEOUT_SECONDS,
        ) == {
            "content": [{"type": "text", "text": "fake.health"}],
        }
    finally:
        client.stop()


def test_response_is_not_initialized_rejects_non_initialization_errors() -> None:
    from mcp_broker import upstream_stdio

    assert upstream_stdio._format_response_error({}) == "{}"
    assert upstream_stdio._response_is_not_initialized({"result": {}}) is False
    assert upstream_stdio._response_is_not_initialized({"error": {"message": 123}}) is False
    assert upstream_stdio._response_identity({"id": 0, "method": "roots/list"}) == (
        "id=0, method='roots/list'"
    )
    assert (
        upstream_stdio._response_is_not_initialized(
            {"error": {"code": -32600, "message": "different failure"}}
        )
        is False
    )
    assert (
        upstream_stdio._response_is_not_initialized(
            {"error": {"code": -32602, "message": "Invalid request parameters", "data": ""}}
        )
        is True
    )


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"error": {"code": -32002}}, True),
        ({"error": {"code": -32002, "message": "anything"}}, True),
        ({"error": {"code": -32600, "message": "Server not initialized"}}, True),
        (
            {
                "error": {
                    "code": -32600,
                    "message": "Received request before initialization was complete",
                }
            },
            True,
        ),
        (
            {"error": {"code": -32602, "message": "Invalid request parameters", "data": "x"}},
            False,
        ),
        (
            {"error": {"code": -32602, "message": "Wrong message", "data": ""}},
            False,
        ),
        ({"error": {"code": -32600, "message": "initialized already"}}, False),
    ],
)
def test_response_is_not_initialized_truth_table(
    response: dict[str, Any],
    expected: bool,
) -> None:
    from mcp_broker import upstream_stdio

    assert upstream_stdio._response_is_not_initialized(response) is expected


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
        timeout_seconds = 0 if error_type is StdioUpstreamTimeout else 1
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
