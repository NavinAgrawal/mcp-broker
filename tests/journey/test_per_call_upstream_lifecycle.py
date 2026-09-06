from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest

from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig
from mcp_broker.daemon import BrokerDaemon


pytestmark = pytest.mark.journey


def test_per_call_upstream_uses_and_reaps_one_real_process_per_operation(
    tmp_path: Path,
) -> None:
    worker = tmp_path / "worker.py"
    starts = tmp_path / "starts.log"
    worker.write_text(
        """
import json
import os
from pathlib import Path
import sys

starts = Path(sys.argv[1])
with starts.open("a", encoding="utf-8") as stream:
    stream.write(f"{os.getpid()}\\n")

for line in sys.stdin:
    request = json.loads(line)
    request_id = request.get("id")
    if request_id is None:
        continue
    method = request.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "per-call-worker", "version": "1"},
        }
    elif method == "tools/list":
        result = {"tools": [{"name": "pid", "inputSchema": {"type": "object"}}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": str(os.getpid())}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}), flush=True)
""".strip(),
        encoding="utf-8",
    )
    runtime = RuntimeConfig(
        root=tmp_path / "runtime",
        socket_path=tmp_path / "runtime" / "broker.sock",
        log_dir=tmp_path / "runtime" / "logs",
        state_dir=tmp_path / "runtime" / "state",
        secrets_dir=tmp_path / "runtime" / "secrets",
    )
    upstream = UpstreamConfig(
        name="task",
        command=sys.executable,
        args=[str(worker), str(starts)],
        mode="per_call",
        tool_prefix="task",
    )
    config = BrokerConfig(
        runtime=runtime,
        broker=BrokerSettings(),
        upstreams={"task": upstream},
    )
    daemon = BrokerDaemon(
        runtime_root=runtime.root,
        socket_path=runtime.socket_path,
        broker_config=config,
    )

    first = daemon._call_stdio_upstream("task", "pid", {}, 5)
    second = daemon._call_stdio_upstream("task", "pid", {}, 5)
    tools = daemon._list_stdio_upstream("task", 5)

    returned_pids = {
        int(first["content"][0]["text"]),
        int(second["content"][0]["text"]),
    }
    started_pids = {int(line) for line in starts.read_text(encoding="utf-8").splitlines()}
    assert len(returned_pids) == 2
    assert len(started_pids) == 3
    assert returned_pids < started_pids
    assert tools == [{"name": "pid", "inputSchema": {"type": "object"}}]
    assert daemon._stdio_upstreams == {}
    assert list(daemon._paths.upstream_pid_dir.glob("task.call.*.json")) == []
    for pid in started_pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
