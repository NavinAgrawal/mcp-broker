import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid

import pytest


pytestmark = pytest.mark.live


def test_broker_daemon_creates_socket_and_answers_health(tmp_path: Path) -> None:
    from mcp_broker.daemon import BrokerDaemon

    runtime_root = tmp_path / "runtime"
    socket_path = _socket_path(tmp_path)
    daemon = BrokerDaemon(runtime_root=runtime_root, socket_path=socket_path)

    daemon.start()
    try:
        response = _request(socket_path, {"method": "broker/health", "id": "health-1"})

        assert socket_path.exists()
        assert (runtime_root / "run" / "broker.lock").exists()
        assert response["id"] == "health-1"
        assert response["result"]["status"] == "ok"
        assert response["result"]["pid"] == os.getpid()
        assert response["result"]["socket_path"] == str(socket_path)
    finally:
        daemon.stop()


def test_broker_daemon_accepts_attachment_sized_request_over_live_socket(tmp_path: Path) -> None:
    from mcp_broker.daemon import BrokerDaemon

    runtime_root = tmp_path / "runtime"
    socket_path = _socket_path(tmp_path)
    daemon = BrokerDaemon(runtime_root=runtime_root, socket_path=socket_path)

    daemon.start()
    try:
        response = _request(
            socket_path,
            {
                "method": "broker/health",
                "id": "attachment-sized-live",
                "attachment_probe": "x" * 818_504,
            },
        )
    finally:
        daemon.stop()

    assert response["id"] == "attachment-sized-live"
    assert response["result"]["status"] == "ok"


def test_broker_daemon_survives_client_disconnect_before_response(tmp_path: Path) -> None:
    from mcp_broker.daemon import BrokerDaemon

    request_started = threading.Event()
    release_response = threading.Event()

    class BlockingDaemon(BrokerDaemon):
        def _handle_request(self, request: dict[str, object]) -> dict[str, object] | None:
            if request.get("id") == "disconnect-before-response":
                request_started.set()
                release_response.wait(timeout=2)
            return super()._handle_request(request)

    runtime_root = tmp_path / "runtime"
    socket_path = _socket_path(tmp_path)
    daemon = BlockingDaemon(runtime_root=runtime_root, socket_path=socket_path)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

    daemon.start()
    try:
        client.connect(str(socket_path))
        client.sendall(
            b'{"jsonrpc":"2.0","id":"disconnect-before-response","method":"broker/health"}\n'
        )
        assert request_started.wait(timeout=1)
        client.close()
        release_response.set()

        response = _request(socket_path, {"method": "broker/health", "id": "after-disconnect"})
    finally:
        release_response.set()
        client.close()
        daemon.stop()

    assert response["id"] == "after-disconnect"
    assert response["result"]["status"] == "ok"


def test_broker_daemon_closes_stalled_live_socket_after_configured_timeout(
    tmp_path: Path,
) -> None:
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig
    from mcp_broker.daemon import BrokerDaemon

    runtime_root = tmp_path / "runtime"
    socket_path = _socket_path(tmp_path)
    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=runtime_root,
            socket_path=socket_path,
            log_dir=runtime_root / "logs",
            state_dir=runtime_root / "state",
            secrets_dir=runtime_root / "secrets",
        ),
        broker=BrokerSettings(socket_read_timeout_seconds=1),
        upstreams={},
    )
    daemon = BrokerDaemon(
        runtime_root=runtime_root,
        socket_path=socket_path,
        broker_config=config,
    )

    daemon.start()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stalled_client:
            stalled_client.settimeout(3)
            stalled_client.connect(str(socket_path))
            assert stalled_client.recv(1) == b""
        response = _request(socket_path, {"method": "broker/health", "id": "after-timeout"})
    finally:
        daemon.stop()

    assert response["id"] == "after-timeout"
    assert response["result"]["status"] == "ok"


def test_broker_daemon_rejects_over_limit_live_socket_request_and_stays_healthy(
    tmp_path: Path,
) -> None:
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig
    from mcp_broker.daemon import BrokerDaemon

    runtime_root = tmp_path / "runtime"
    socket_path = _socket_path(tmp_path)
    max_request_bytes = 128
    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=runtime_root,
            socket_path=socket_path,
            log_dir=runtime_root / "logs",
            state_dir=runtime_root / "state",
            secrets_dir=runtime_root / "secrets",
        ),
        broker=BrokerSettings(socket_max_request_bytes=max_request_bytes),
        upstreams={},
    )
    daemon = BrokerDaemon(
        runtime_root=runtime_root,
        socket_path=socket_path,
        broker_config=config,
    )

    daemon.start()
    try:
        rejected = _request(
            socket_path,
            {
                "method": "broker/health",
                "id": "over-limit",
                "attachment_probe": "x" * max_request_bytes,
            },
        )
        response = _request(socket_path, {"method": "broker/health", "id": "after-limit"})
    finally:
        daemon.stop()

    assert rejected == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {
            "code": -32600,
            "message": f"Request exceeds {max_request_bytes} bytes",
        },
    }
    assert response["id"] == "after-limit"
    assert response["result"]["status"] == "ok"


def test_broker_daemon_writes_structured_jsonl_lifecycle_logs(tmp_path: Path) -> None:
    from mcp_broker.daemon import BrokerDaemon

    runtime_root = tmp_path / "runtime"
    socket_path = _socket_path(tmp_path)
    daemon = BrokerDaemon(runtime_root=runtime_root, socket_path=socket_path)

    daemon.start()
    response = _request(socket_path, {"method": "broker/health", "id": "health-log"})
    daemon.stop()

    log_path = runtime_root / "logs" / "broker.jsonl"
    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    events = [entry["event"] for entry in entries]
    health_entry = next(
        entry
        for entry in entries
        if entry["event"] == "request.handled" and entry["method"] == "broker/health"
    )

    assert response["result"]["status"] == "ok"
    assert "daemon.started" in events
    assert "request.handled" in events
    assert "daemon.stopped" in events
    assert all({"event", "level", "pid", "ts"} <= set(entry) for entry in entries)
    assert health_entry["request_id"] == "health-log"
    assert health_entry["status"] == "ok"


def test_broker_daemon_writes_runtime_metrics_snapshot(tmp_path: Path) -> None:
    from mcp_broker.daemon import BrokerDaemon

    runtime_root = tmp_path / "runtime"
    socket_path = _socket_path(tmp_path)
    snapshot_path = runtime_root / "state" / "broker-status.json"
    daemon = BrokerDaemon(runtime_root=runtime_root, socket_path=socket_path)

    daemon.start()
    try:
        started = json.loads(snapshot_path.read_text(encoding="utf-8"))
        health_response = _request(socket_path, {"method": "broker/health", "id": "metrics"})
        _request(socket_path, {"method": "missing/method", "id": "missing"})
        running = _wait_for_snapshot_request_total(snapshot_path, 2)
    finally:
        daemon.stop()

    stopped = json.loads(snapshot_path.read_text(encoding="utf-8"))

    assert health_response["result"]["status"] == "ok"
    assert started["status"] == "running"
    assert started["pid"] == os.getpid()
    assert started["socket_path"] == str(socket_path)
    assert started["requests_total"] == 0
    assert started["request_errors_total"] == 0
    assert started["upstreams"] == {}
    assert running["status"] == "running"
    assert running["requests_total"] == 2
    assert running["request_errors_total"] == 1
    assert running["last_request_method"] == "missing/method"
    assert running["last_request_status"] == "error"
    assert stopped["status"] == "stopped"
    assert stopped["requests_total"] >= 2
    assert stopped["request_errors_total"] >= 1
    assert stopped["updated_at"] >= running["updated_at"]


def test_broker_daemon_refuses_second_live_daemon_for_same_runtime(tmp_path: Path) -> None:
    from mcp_broker.daemon import BrokerDaemon, BrokerDaemonError

    runtime_root = tmp_path / "runtime"
    socket_path = _socket_path(tmp_path)
    first = BrokerDaemon(runtime_root=runtime_root, socket_path=socket_path)
    second = BrokerDaemon(runtime_root=runtime_root, socket_path=socket_path)

    first.start()
    try:
        with pytest.raises(BrokerDaemonError, match="broker daemon already running"):
            second.start()
    finally:
        first.stop()


def test_broker_daemon_stop_removes_socket_lock_and_metadata(tmp_path: Path) -> None:
    from mcp_broker.daemon import BrokerDaemon

    runtime_root = tmp_path / "runtime"
    socket_path = _socket_path(tmp_path)
    daemon = BrokerDaemon(runtime_root=runtime_root, socket_path=socket_path)

    daemon.start()
    response = _request(socket_path, {"method": "broker/stop", "id": "stop-1"})
    daemon.join(timeout=2)

    assert response == {
        "id": "stop-1",
        "result": {
            "stopping": True,
            "stopped_upstreams": [],
            "remaining_broker_processes": [],
        },
    }
    assert not socket_path.exists()
    assert not (runtime_root / "run" / "broker.lock").exists()
    assert not (runtime_root / "run" / "sockets" / f"{socket_path.name}.json").exists()


def test_broker_daemon_stop_verifies_upstream_process_group_shutdown(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig
    from mcp_broker.daemon import BrokerDaemon

    runtime_root = tmp_path / "runtime"
    socket_path = _socket_path(tmp_path)
    worker = _stubborn_child_worker(tmp_path)
    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=runtime_root,
            socket_path=socket_path,
            log_dir=runtime_root / "logs",
            state_dir=runtime_root / "state",
            secrets_dir=runtime_root / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={
            "fake": UpstreamConfig(
                name="fake",
                command=sys.executable,
                args=[str(worker)],
                mode="shared",
                enabled=True,
                state_dir="upstreams/fake",
                tool_prefix="fake",
            )
        },
    )
    daemon = BrokerDaemon(
        runtime_root=runtime_root,
        socket_path=socket_path,
        broker_config=config,
    )
    child_pid: int | None = None

    daemon.start()
    try:
        response = _request(
            socket_path,
            {
                "jsonrpc": "2.0",
                "id": "call-1",
                "method": "tools/call",
                "params": {"name": "fake.spawn", "arguments": {}},
            },
        )
        child_pid = response["result"]["child_pid"]
        process_metadata_path = runtime_root / "run" / "upstreams" / "fake.json"
        process_metadata = json.loads(process_metadata_path.read_text(encoding="utf-8"))

        assert isinstance(child_pid, int)
        assert _process_exists(child_pid)
        assert process_metadata["owner"] == "mcp-broker"
        assert process_metadata["name"] == "fake"
        assert isinstance(process_metadata["pid"], int)
        assert _process_exists(process_metadata["pid"])
        assert process_metadata["process_group_id"] == os.getpgid(process_metadata["pid"])
        assert process_metadata["broker_pid"] == os.getpid()

        stop_response = _request(socket_path, {"method": "broker/stop", "id": "stop-verified"})
        daemon.join(timeout=2)
        child_running_after_stop = _wait_for_process_active(child_pid)
    finally:
        if child_pid is not None and _process_exists(child_pid):
            os.kill(child_pid, signal.SIGKILL)
        daemon.stop()

    assert stop_response == {
        "id": "stop-verified",
        "result": {
            "stopping": True,
            "stopped_upstreams": ["fake"],
            "remaining_broker_processes": [],
        },
    }
    assert child_running_after_stop is False
    assert not socket_path.exists()
    assert not (runtime_root / "run" / "broker.lock").exists()
    assert not (runtime_root / "run" / "upstreams" / "fake.json").exists()


def test_broker_daemon_writes_upstream_event_logs(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig
    from mcp_broker.daemon import BrokerDaemon
    from mcp_broker.schema import HealthPolicy

    runtime_root = tmp_path / "runtime"
    socket_path = _socket_path(tmp_path)
    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=runtime_root,
            socket_path=socket_path,
            log_dir=runtime_root / "logs",
            state_dir=runtime_root / "state",
            secrets_dir=runtime_root / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={
            "fast": UpstreamConfig(
                name="fast",
                command=sys.executable,
                args=[str(_fast_tool_worker(tmp_path))],
                mode="shared",
                enabled=True,
                state_dir="upstreams/fast",
                tool_prefix="fast",
            ),
            "slow": UpstreamConfig(
                name="slow",
                command=sys.executable,
                args=[str(_slow_tool_worker(tmp_path))],
                mode="shared",
                enabled=True,
                state_dir="upstreams/slow",
                tool_prefix="slow",
                health=HealthPolicy(call_timeout_seconds=1),
            ),
        },
    )
    daemon = BrokerDaemon(
        runtime_root=runtime_root,
        socket_path=socket_path,
        broker_config=config,
    )

    daemon.start()
    try:
        fast_response = _request(
            socket_path,
            {
                "jsonrpc": "2.0",
                "id": "fast-call",
                "method": "tools/call",
                "params": {"name": "fast.echo", "arguments": {"value": "ok"}},
            },
        )
        timeout_response = _request(
            socket_path,
            {
                "jsonrpc": "2.0",
                "id": "slow-call",
                "method": "tools/call",
                "params": {"name": "slow.sleep", "arguments": {}},
            },
        )
    finally:
        daemon.stop()

    entries = [
        json.loads(line)
        for line in (runtime_root / "logs" / "broker.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    upstream_events = [
        entry for entry in entries if str(entry.get("event", "")).startswith("upstream.")
    ]
    event_names = [entry["event"] for entry in upstream_events]

    assert fast_response["id"] == "fast-call"
    assert "result" in fast_response
    assert timeout_response["error"]["message"] == "upstream timed out: slow"
    assert "upstream.start" in event_names
    assert "upstream.ready" in event_names
    assert "upstream.call" in event_names
    assert "upstream.timeout" in event_names
    assert "upstream.stop" in event_names
    assert "upstream.kill" in event_names
    assert all(entry["upstream"] in {"fast", "slow"} for entry in upstream_events)
    assert any(
        entry["event"] == "upstream.call"
        and entry["upstream"] == "fast"
        and entry["tool_name"] == "echo"
        for entry in upstream_events
    )


def test_broker_daemon_recovers_upstream_after_timed_out_response(
    tmp_path: Path,
) -> None:
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig
    from mcp_broker.daemon import BrokerDaemon
    from mcp_broker.schema import HealthPolicy

    runtime_root = tmp_path / "runtime"
    socket_path = _socket_path(tmp_path)
    config = BrokerConfig(
        runtime=RuntimeConfig(
            root=runtime_root,
            socket_path=socket_path,
            log_dir=runtime_root / "logs",
            state_dir=runtime_root / "state",
            secrets_dir=runtime_root / "secrets",
        ),
        broker=BrokerSettings(),
        upstreams={
            "fake": UpstreamConfig(
                name="fake",
                command=sys.executable,
                args=[str(_late_response_worker(tmp_path))],
                mode="shared",
                enabled=True,
                state_dir="upstreams/fake",
                tool_prefix="fake",
                health=HealthPolicy(call_timeout_seconds=1),
            )
        },
    )
    daemon = BrokerDaemon(
        runtime_root=runtime_root,
        socket_path=socket_path,
        broker_config=config,
    )

    daemon.start()
    try:
        timeout_response = _request(
            socket_path,
            {
                "jsonrpc": "2.0",
                "id": "slow-call",
                "method": "tools/call",
                "params": {"name": "fake.echo", "arguments": {"value": "first"}},
            },
        )
        fast_response = _request(
            socket_path,
            {
                "jsonrpc": "2.0",
                "id": "fast-call",
                "method": "tools/call",
                "params": {"name": "fake.echo", "arguments": {"value": "second"}},
            },
        )
        health_response = _request(socket_path, {"method": "broker/health", "id": "health"})
    finally:
        daemon.stop()

    assert timeout_response["error"]["message"] == "upstream timed out: fake"
    assert fast_response["result"]["content"] == [{"type": "text", "text": "second"}]
    upstream_health = health_response["result"]["upstreams"]["fake"]
    assert upstream_health["last_error"] is None
    assert upstream_health["restarts"] == 1


def test_broker_daemon_reports_invalid_json_and_ignores_empty_requests(tmp_path: Path) -> None:
    from mcp_broker.daemon import BrokerDaemon

    runtime_root = tmp_path / "runtime"
    socket_path = _socket_path(tmp_path)
    daemon = BrokerDaemon(runtime_root=runtime_root, socket_path=socket_path)

    daemon.start()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(socket_path))

        response = _raw_request(socket_path, b"{bad-json\n")

        assert response == {
            "error": {"code": -32700, "message": "Parse error"},
            "id": None,
            "jsonrpc": "2.0",
        }
        assert _request(socket_path, {"method": "broker/health", "id": "after-empty"})[
            "result"
        ]["status"] == "ok"
    finally:
        daemon.stop()


def test_broker_daemon_cleans_lock_after_bind_failure(tmp_path: Path) -> None:
    from mcp_broker.daemon import BrokerDaemon

    runtime_root = tmp_path / "runtime"
    socket_path = tmp_path / ("x" * 120)
    daemon = BrokerDaemon(runtime_root=runtime_root, socket_path=socket_path)

    with pytest.raises(OSError, match="AF_UNIX path too long"):
        daemon.start()

    assert not (runtime_root / "run" / "broker.lock").exists()


def test_broker_daemon_removes_stale_lock_before_start(tmp_path: Path) -> None:
    from mcp_broker.daemon import BrokerDaemon

    runtime_root = tmp_path / "runtime"
    socket_path = _socket_path(tmp_path)
    daemon = BrokerDaemon(runtime_root=runtime_root, socket_path=socket_path)
    lock_path = runtime_root / "run" / "broker.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(json.dumps({"owner": "mcp-broker", "pid": 99_999_999}), encoding="utf-8")

    daemon.start()
    try:
        assert _request(socket_path, {"method": "broker/health", "id": "stale-lock"})[
            "result"
        ]["status"] == "ok"
    finally:
        daemon.stop()


def test_broker_daemon_handles_closed_server_and_missing_wake_socket(tmp_path: Path) -> None:
    from mcp_broker.daemon import BrokerDaemon

    runtime_root = tmp_path / "runtime"
    socket_path = _socket_path(tmp_path)
    daemon = BrokerDaemon(runtime_root=runtime_root, socket_path=socket_path)

    daemon._serve_loop()
    daemon._wake_server()
    socket_path.write_text("not a socket", encoding="utf-8")
    try:
        daemon._wake_server()
    finally:
        socket_path.unlink(missing_ok=True)

    closed_server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    closed_server.close()
    daemon._server = closed_server
    daemon._serve_loop()


def _request(socket_path: Path, payload: dict[str, object]) -> dict[str, object]:
    return _raw_request(socket_path, json.dumps(payload).encode("utf-8") + b"\n")


def _raw_request(socket_path: Path, payload: bytes) -> dict[str, object]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(socket_path))
        client.sendall(payload)
        chunks = []
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            if chunk.endswith(b"\n"):
                break
        raw = b"".join(chunks)
    return json.loads(raw.decode("utf-8"))


def _wait_for_snapshot_request_total(snapshot_path: Path, expected: int) -> dict[str, object]:
    deadline = time.monotonic() + 2
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    while time.monotonic() < deadline:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        if snapshot["requests_total"] >= expected:
            return snapshot
        time.sleep(0.01)
    return snapshot


def _socket_path(tmp_path: Path) -> Path:
    return Path("/tmp") / f"mcp-broker-{uuid.uuid4().hex}.sock"


def _stubborn_child_worker(tmp_path: Path) -> Path:
    path = tmp_path / "stubborn_child_worker.py"
    child_code = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(60)\n"
    )
    path.write_text(
        f"""
import json
import subprocess
import sys

child = subprocess.Popen([sys.executable, "-c", {child_code!r}])

for line in sys.stdin:
    request = json.loads(line)
    print(
        json.dumps(
            {{
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {{
                    "content": [{{"type": "text", "text": str(child.pid)}}],
                    "child_pid": child.pid,
                }},
            }}
        ),
        flush=True,
    )
""".lstrip(),
        encoding="utf-8",
    )
    return path


def _fast_tool_worker(tmp_path: Path) -> Path:
    path = tmp_path / "fast_tool_worker.py"
    path.write_text(
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
                "result": {"content": [{"type": "text", "text": "ok"}]},
            }
        ),
        flush=True,
    )
""".lstrip(),
        encoding="utf-8",
    )
    return path


def _slow_tool_worker(tmp_path: Path) -> Path:
    path = tmp_path / "slow_tool_worker.py"
    path.write_text(
        """
import json
import sys
import time

for line in sys.stdin:
    json.loads(line)
    time.sleep(10)
""".lstrip(),
        encoding="utf-8",
    )
    return path


def _late_response_worker(tmp_path: Path) -> Path:
    path = tmp_path / "late_response_worker.py"
    path.write_text(
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
    print(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": request["params"]["arguments"]["value"],
                        }
                    ]
                },
            }
        ),
        flush=True,
    )
""".lstrip(),
        encoding="utf-8",
    )
    return path


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    state = _process_state(pid)
    if state is not None and state.startswith("Z"):
        return False
    return True


def _wait_for_process_active(pid: int, timeout_seconds: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _process_exists(pid):
            return False
        time.sleep(0.01)
    return _process_exists(pid)


def _process_state(pid: int) -> str | None:
    result = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        check=False,
        capture_output=True,
        text=True,
    )
    state = result.stdout.strip()
    if result.returncode != 0 or not state:
        return None
    return state
