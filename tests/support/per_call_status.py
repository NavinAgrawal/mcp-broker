"""Real config and subprocess support for per-call status contracts."""

from pathlib import Path
import sys

import yaml

from mcp_broker.catalog import BrokerCatalogFacade
from mcp_broker.config import BrokerConfig
from mcp_broker.daemon import BrokerDaemon


def configured_daemon(tmp_path: Path) -> BrokerDaemon:
    config_path = tmp_path / "broker.yaml"
    config_path.write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "runtime": {"root": str(tmp_path / "runtime")},
            "profiles": {"reader": {"max_tools": 4}, "operator": {"max_tools": 4}},
            "upstreams": {
                "task": {
                    "command": sys.executable,
                    "args": [str(tmp_path / "worker.py"), str(tmp_path)],
                    "mode": "per_call",
                    "profiles": ["reader"],
                },
                "other-task": {
                    "command": sys.executable,
                    "args": [str(tmp_path / "worker.py"), str(tmp_path)],
                    "mode": "per_call",
                    "profiles": ["operator"],
                },
                "session-task": {
                    "command": sys.executable,
                    "mode": "per_session",
                    "profiles": ["reader"],
                },
            },
        }),
        encoding="utf-8",
    )
    config = BrokerConfig.from_file(config_path)
    return BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )


def status_facade(daemon: BrokerDaemon, *, profile_name: str | None = None) -> BrokerCatalogFacade:
    return BrokerCatalogFacade(
        broker_config=daemon.broker_config,
        profile=daemon.broker_config.profiles[profile_name] if profile_name is not None else None,
        list_upstream=daemon._list_stdio_upstream,
        call_upstream=daemon._call_stdio_upstream,
        call_locks={},
        status_provider=daemon._upstream_health_for_status,
    )


def write_blocked_worker(tmp_path: Path) -> None:
    (tmp_path / "worker.py").write_text(
        '''import asyncio
import json
import os
from pathlib import Path
import sys

root = Path(sys.argv[1])
with (root / "starts.log").open("a", encoding="utf-8") as stream:
    stream.write(str(os.getpid()) + "\\n")
async def respond(request):
    if request["method"] == "initialize":
        result = {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "status-worker", "version": "1"},
        }
    else:
        arguments = request["params"]["arguments"]
        token = arguments["token"]
        (root / (token + ".started")).write_text(str(os.getpid()), encoding="utf-8")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10
        while not (root / (token + ".release")).exists():
            if loop.time() >= deadline:
                raise RuntimeError("test controller did not release call")
            await asyncio.sleep(0.01)
        result = {"content": [{"type": "text", "text": str(os.getpid())}]}
        if arguments.get("fail"):
            result["isError"] = True
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}) + "\\n")
    sys.stdout.flush()


async def main():
    while line := await asyncio.to_thread(sys.stdin.readline):
        request = json.loads(line)
        if "id" in request:
            await respond(request)


asyncio.run(main())
''',
        encoding="utf-8",
    )
