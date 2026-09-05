import io
from pathlib import Path
import subprocess
import sys
from typing import Any, cast
import pytest
from mcp_broker.config import UpstreamConfig
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
