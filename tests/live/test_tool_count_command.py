import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import yaml

from tests.support.json_report import report_from_stdout


pytestmark = pytest.mark.live

ROOT = Path(__file__).resolve().parents[2]


def test_make_tools_count_reports_exact_namespaced_tool_counts(tmp_path: Path) -> None:
    from mcp_broker.tool_count import main as tool_count_main

    runtime_root = tmp_path / "runtime"
    socket_path = Path("/tmp") / f"mcp-broker-tool-count-{uuid.uuid4().hex}.sock"
    worker = _tool_listing_worker(tmp_path)
    config_path = tmp_path / "broker.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "runtime": {
                    "root": str(runtime_root),
                    "socket_path": str(socket_path),
                },
                "profiles": {
                    "manual-test": {
                        "max_tools": 80,
                        "compact_tools_enabled": False,
                    }
                },
                "upstreams": {
                    "fake": {
                        "enabled": True,
                        "mode": "shared",
                        "transport": "stdio",
                        "tool_prefix": "fake",
                        "command": sys.executable,
                        "args": [str(worker)],
                        "state_dir": "upstreams/fake",
                        "profiles": ["manual-test"],
                    }
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "make",
            "tools-count",
            "PROFILE=manual-test",
            f"CONFIG_PATH={config_path}",
            f"RUNTIME_ROOT={runtime_root}",
            f"SOCKET_PATH={socket_path}",
        ],
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    report = report_from_stdout(result.stdout, label="tools count")

    assert report["profile"] == "manual-test"
    assert report["total_tools"] == 2
    assert report["upstream_counts"] == {"fake": 2}
    assert report["tools"] == ["fake.echo", "fake.search"]
    assert not (runtime_root / "run" / "upstreams" / "fake.json").exists()

    assert tool_count_main(["--config", str(config_path), "--profile", "manual-test"]) == 0


def test_make_tools_count_reuses_running_broker_without_stopping_it(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig
    from mcp_broker.daemon import BrokerDaemon

    runtime_root = tmp_path / "runtime"
    socket_path = Path("/tmp") / f"mcp-broker-tool-count-running-{uuid.uuid4().hex}.sock"
    config_path = tmp_path / "broker.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "runtime": {
                    "root": str(runtime_root),
                    "socket_path": str(socket_path),
                },
                "profiles": {
                    "llm-profile": {
                        "max_tools": 80,
                        "compact_tools_enabled": True,
                    }
                },
                "upstreams": {
                    "fake": {
                        "enabled": True,
                        "mode": "shared",
                        "transport": "stdio",
                        "tool_prefix": "fake",
                        "command": sys.executable,
                        "args": [str(_tool_listing_worker(tmp_path))],
                        "state_dir": "upstreams/fake",
                        "profiles": ["llm-profile"],
                    }
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    config = BrokerConfig.from_file(config_path)
    daemon = BrokerDaemon(
        runtime_root=config.runtime.root,
        socket_path=config.runtime.socket_path,
        broker_config=config,
    )
    daemon.start()
    try:
        result = subprocess.run(
            [
                "make",
                "tools-count",
                "PROFILE=llm-profile",
                f"CONFIG_PATH={config_path}",
                f"RUNTIME_ROOT={runtime_root}",
                f"SOCKET_PATH={socket_path}",
            ],
            cwd=ROOT,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        report = report_from_stdout(result.stdout, label="tools count")

        assert report["profile"] == "llm-profile"
        assert report["total_tools"] == 5
        assert report["upstream_counts"] == {"broker": 5}
        assert report["tools"] == [
            "broker.call_tool",
            "broker.close_session",
            "broker.describe_tool",
            "broker.search_tools",
            "broker.status",
        ]
        assert daemon._thread is not None
        assert daemon._thread.is_alive()
        assert not (runtime_root / "run" / "upstreams" / "fake.json").exists()
    finally:
        daemon.stop()


def _tool_listing_worker(tmp_path: Path) -> Path:
    path = tmp_path / "tool_listing_worker.py"
    path.write_text(
        """
import json
import sys


for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": request.get("params", {}).get("protocolVersion", "2025-11-25"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "0.0.1"},
        }
    elif method == "tools/list":
        result = {"tools": [{"name": "echo"}, {"name": "search"}]}
    else:
        result = {"content": [{"type": "text", "text": method}]}
    print(json.dumps({"jsonrpc": "2.0", "id": request.get("id"), "result": result}), flush=True)
""".strip(),
        encoding="utf-8",
    )
    return path
