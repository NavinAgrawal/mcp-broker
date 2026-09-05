import io
import json
from pathlib import Path
import subprocess
import sys
import pytest
from mcp_broker import __version__
from mcp_broker.config import UpstreamConfig
from mcp_broker.protocol import SUPPORTED_PROTOCOL_VERSIONS
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
