import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import yaml

from tests.support.json_report import report_from_stdout


pytestmark = pytest.mark.live

ROOT = Path(__file__).resolve().parents[2]


def test_facade_smoke_report_parser_ignores_make_directory_noise() -> None:
    report = report_from_stdout(
        "\n".join(
            [
                "make[1]: Entering directory '/tmp/repo'",
                '{"profile": "codex", "called_tool": "fake.echo"}',
                "make[1]: Leaving directory '/tmp/repo'",
            ]
        ),
        label="facade smoke",
    )

    assert report == {"profile": "codex", "called_tool": "fake.echo"}


def test_make_codex_facade_smoke_uses_client_shim_and_calls_upstream(
    tmp_path: Path,
) -> None:
    from mcp_broker.facade_smoke import main as facade_smoke_main

    runtime_root = tmp_path / "runtime"
    socket_path = Path("/tmp") / f"mcp-broker-codex-smoke-{uuid.uuid4().hex}.sock"
    config_path = tmp_path / "broker.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "runtime": {
                    "root": str(runtime_root),
                    "socket_path": str(socket_path),
                },
                "profiles": {
                    "codex": {
                        "max_tools": 80,
                        "compact_tools_enabled": True,
                    }
                },
                "upstreams": {
                    "fake": {
                        "enabled": True,
                        "mode": "shared",
                        "transport": "stdio",
                        "purpose": "Fake upstream for Codex facade smoke.",
                        "tags": ["fake", "smoke"],
                        "tool_prefix": "fake",
                        "command": sys.executable,
                        "args": [str(_facade_worker(tmp_path))],
                        "state_dir": "upstreams/fake",
                        "profiles": ["codex"],
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
            "codex-facade-smoke",
            f"CONFIG_PATH={config_path}",
            f"RUNTIME_ROOT={runtime_root}",
            f"SOCKET_PATH={socket_path}",
            "PROFILE=codex",
            "FACADE_QUERY=echo",
            "FACADE_CALL_TOOL=fake.echo",
            'FACADE_CALL_ARGS={"message":"hello"}',
        ],
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    report = report_from_stdout(result.stdout, label="facade smoke")

    assert report["profile"] == "codex"
    assert report["advertised_tools"] == [
        "broker.call_tool",
        "broker.close_session",
        "broker.describe_tool",
        "broker.search_tools",
        "broker.status",
    ]
    assert report["described_tool"] == "fake.echo"
    assert report["called_tool"] == "fake.echo"
    assert report["call_text"] == '{"message": "hello"}'
    assert not (runtime_root / "run" / "upstreams" / "fake.json").exists()

    assert (
        facade_smoke_main(
            [
                "--config",
                str(config_path),
                "--profile",
                "codex",
                "--query",
                "echo",
                "--call-tool",
                "fake.echo",
                "--call-args",
                '{"message":"hello"}',
            ]
        )
        == 0
    )


def test_make_claude_facade_smoke_uses_claude_profile_without_wiring(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    socket_path = Path("/tmp") / f"mcp-broker-claude-smoke-{uuid.uuid4().hex}.sock"
    config_path = tmp_path / "broker.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "runtime": {
                    "root": str(runtime_root),
                    "socket_path": str(socket_path),
                },
                "profiles": {
                    "claude": {
                        "max_tools": 80,
                        "compact_tools_enabled": True,
                    }
                },
                "upstreams": {
                    "fake": {
                        "enabled": True,
                        "mode": "shared",
                        "transport": "stdio",
                        "purpose": "Fake upstream for Claude facade smoke.",
                        "tags": ["fake", "smoke"],
                        "tool_prefix": "fake",
                        "command": sys.executable,
                        "args": [str(_facade_worker(tmp_path))],
                        "state_dir": "upstreams/fake",
                        "profiles": ["claude"],
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
            "claude-facade-smoke",
            f"CONFIG_PATH={config_path}",
            f"RUNTIME_ROOT={runtime_root}",
            f"SOCKET_PATH={socket_path}",
            "FACADE_QUERY=echo",
            "FACADE_CALL_TOOL=fake.echo",
            'FACADE_CALL_ARGS={"message":"hello"}',
        ],
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    report = report_from_stdout(result.stdout, label="facade smoke")

    assert report["profile"] == "claude"
    assert report["advertised_tools"] == [
        "broker.call_tool",
        "broker.close_session",
        "broker.describe_tool",
        "broker.search_tools",
        "broker.status",
    ]
    assert report["described_tool"] == "fake.echo"
    assert report["called_tool"] == "fake.echo"
    assert report["call_text"] == '{"message": "hello"}'
    assert not (runtime_root / "run" / "upstreams" / "fake.json").exists()


def _facade_worker(tmp_path: Path) -> Path:
    path = tmp_path / "facade_worker.py"
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
        result = {
            "tools": [
                {
                    "name": "echo",
                    "description": "Echo a provided message.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"message": {"type": "string"}},
                    },
                }
            ]
        }
    else:
        text = json.dumps(request.get("params", {}).get("arguments", {}), sort_keys=True)
        result = {"content": [{"type": "text", "text": text}]}
    print(json.dumps({"jsonrpc": "2.0", "id": request.get("id"), "result": result}), flush=True)
""".strip(),
        encoding="utf-8",
    )
    return path
