from __future__ import annotations

import importlib.util
import plistlib
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _load_runtime_sync_module() -> object:
    script = ROOT / "scripts" / "runtime-sync-check.py"
    assert script.is_file()
    spec = importlib.util.spec_from_file_location("runtime_sync_check", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_runtime_sync_check_accepts_the_canonical_config_contract(tmp_path: Path) -> None:
    module = _load_runtime_sync_module()
    runtime_root = tmp_path / "runtime"
    config_path = runtime_root / "config" / "broker.private.yaml"
    working_directory = tmp_path / "public-runtime"
    plist_path = tmp_path / "com.mcp-broker.agent.plist"
    plist_path.write_bytes(
        plistlib.dumps(
            {
                "WorkingDirectory": str(working_directory),
                "ProgramArguments": [
                    "python",
                    "-m",
                    "mcp_broker.daemon",
                    "serve",
                    "--runtime-root",
                    str(runtime_root),
                    "--config",
                    str(config_path),
                ],
            }
        )
    )

    report = module.check_runtime_sync(
        plist_path=plist_path,
        runtime_root=runtime_root,
        config_path=config_path,
        working_directory=working_directory,
    )

    assert report.is_clean
    assert report.expected_config_path == config_path
    assert report.observed_config_path == config_path
    assert report.findings == []
