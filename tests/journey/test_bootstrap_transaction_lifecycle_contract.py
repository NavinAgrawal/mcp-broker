from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
import subprocess
import zipfile

import pytest

from mcp_broker.runtime_install import RuntimeInstallStore
from tests.support.repo_paths import make_command, repo_root


pytestmark = pytest.mark.journey

ROOT = repo_root()


def test_plugin_bootstrap_preflight_verifies_metadata_without_state_writes(
    tmp_path: Path,
) -> None:
    metadata_path = _write_runtime_package(tmp_path / "package")
    runtime_root = tmp_path / "runtime-root"

    result = _run_make(
        "plugin-bootstrap-preflight",
        f"BOOTSTRAP_METADATA={metadata_path}",
        f"RUNTIME_ROOT={runtime_root}",
    )

    assert '"status": "preflight_passed"' in result.stdout
    assert not (runtime_root / "state" / "bootstrap-transactions").exists()
    assert not (runtime_root / "state" / "runtime-install").exists()


def test_plugin_bootstrap_apply_status_rollback_and_uninstall_lifecycle(
    tmp_path: Path,
) -> None:
    metadata_path = _write_runtime_package(tmp_path / "package", version="candidate-runtime")
    runtime_root = tmp_path / "runtime-root"
    state_dir = runtime_root / "state"
    old_runtime = _write_runtime_dir(tmp_path / "old-runtime")
    old_manifest = RuntimeInstallStore(state_dir).record_installed_runtime(
        version="previous-runtime",
        runtime_path=old_runtime,
        entrypoint="bin/mcp-broker",
        artifact_digest=f"sha256:{'1' * 64}",
    )

    denied_apply = subprocess.run(
        make_command(
            "plugin-bootstrap-apply",
            f"BOOTSTRAP_METADATA={metadata_path}",
            f"RUNTIME_ROOT={runtime_root}",
        ),
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert denied_apply.returncode == 2
    assert "BOOTSTRAP_APPROVED=1 is required" in denied_apply.stderr

    applied = _run_make(
        "plugin-bootstrap-apply",
        f"BOOTSTRAP_METADATA={metadata_path}",
        f"RUNTIME_ROOT={runtime_root}",
        "BOOTSTRAP_APPROVED=1",
    )
    applied_json = _last_json(applied.stdout)
    assert applied_json["status"] == "applied"
    assert applied_json["active_runtime_id"] != old_manifest["runtime_id"]
    assert applied_json["previous_runtime_id"] == old_manifest["runtime_id"]

    status = _run_make("plugin-bootstrap-status", f"RUNTIME_ROOT={runtime_root}")
    status_json = _last_json(status.stdout)
    assert status_json["latest_transaction"]["status"] == "applied"

    rolled_back = _run_make(
        "plugin-bootstrap-rollback",
        f"RUNTIME_ROOT={runtime_root}",
        "BOOTSTRAP_APPROVED=1",
    )
    rolled_back_json = _last_json(rolled_back.stdout)
    assert rolled_back_json["status"] == "rolled_back"
    assert rolled_back_json["active_runtime_id"] == old_manifest["runtime_id"]

    uninstalled = _run_make(
        "plugin-bootstrap-uninstall",
        f"RUNTIME_ROOT={runtime_root}",
        "BOOTSTRAP_APPROVED=1",
    )
    assert _last_json(uninstalled.stdout)["status"] == "uninstalled"
    assert not (state_dir / "runtime-install" / "active-runtime.json").exists()


def _run_make(*args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        make_command(*args),
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert result.returncode == 0, (
        f"make {' '.join(args)} failed with {result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    return result


def _last_json(output: str) -> dict[str, object]:
    for line in reversed(output.splitlines()):
        if line.startswith("{") and line.endswith("}"):
            loaded = json.loads(line)
            assert isinstance(loaded, dict)
            return loaded
    raise AssertionError(f"no JSON object in output: {output}")


def _write_runtime_package(package_dir: Path, *, version: str = "candidate-runtime") -> Path:
    package_dir.mkdir(parents=True, exist_ok=True)
    runtime_path = _write_runtime_dir(package_dir / "runtime")
    artifact_path = package_dir / "runtime.zip"
    with zipfile.ZipFile(artifact_path, "w") as archive:
        info = zipfile.ZipInfo("bin/mcp-broker")
        info.external_attr = (stat.S_IFREG | 0o755) << 16
        archive.writestr(info, "#!/bin/sh\nexit 0\n")
    metadata_path = package_dir / "runtime-metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "artifact_digest": f"sha256:{_sha256(artifact_path)}",
                "artifact_path": "runtime.zip",
                "entrypoint": "bin/mcp-broker",
                "runtime_path": "runtime",
                "version": version,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return metadata_path


def _write_runtime_dir(runtime_path: Path) -> Path:
    entrypoint = runtime_path / "bin" / "mcp-broker"
    entrypoint.parent.mkdir(parents=True, exist_ok=True)
    entrypoint.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    entrypoint.chmod(0o755)
    return runtime_path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
