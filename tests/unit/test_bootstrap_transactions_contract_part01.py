from __future__ import annotations
import hashlib
import io
import json
from pathlib import Path
import stat
import tarfile
import zipfile
import pytest
pytestmark = pytest.mark.unit
def _write_runtime_package(
    package_dir: Path,
    *,
    version: str,
    archive_script: str = "#!/bin/sh\nexit 0\n",
    unpacked_script: str = "#!/bin/sh\nexit 0\n",
) -> Path:
    package_dir.mkdir(parents=True, exist_ok=True)
    runtime_path = _write_runtime_dir(package_dir / "runtime", script=unpacked_script)
    artifact_path = package_dir / "runtime.zip"
    with zipfile.ZipFile(artifact_path, "w") as archive:
        info = zipfile.ZipInfo("bin/mcp-broker")
        info.external_attr = (stat.S_IFREG | 0o755) << 16
        archive.writestr(info, archive_script)
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
def _write_runtime_tar_package(
    package_dir: Path,
    *,
    version: str,
    archive_script: str = "#!/bin/sh\nexit 0\n",
    unpacked_script: str = "#!/bin/sh\nexit 0\n",
) -> Path:
    package_dir.mkdir(parents=True, exist_ok=True)
    _write_runtime_dir(package_dir / "runtime", script=unpacked_script)
    artifact_path = package_dir / "runtime.tar"
    with tarfile.open(artifact_path, "w") as archive:
        payload = archive_script.encode("utf-8")
        info = tarfile.TarInfo("bin/mcp-broker")
        info.size = len(payload)
        info.mode = 0o755
        archive.addfile(info, io.BytesIO(payload))
    metadata_path = package_dir / "runtime-metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "artifact_digest": f"sha256:{_sha256(artifact_path)}",
                "artifact_path": "runtime.tar",
                "entrypoint": "bin/mcp-broker",
                "runtime_path": "runtime",
                "version": version,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return metadata_path
def _write_runtime_dir(runtime_path: Path, *, script: str = "#!/bin/sh\nexit 0\n") -> Path:
    entrypoint = runtime_path / "bin" / "mcp-broker"
    entrypoint.parent.mkdir(parents=True, exist_ok=True)
    entrypoint.write_text(script, encoding="utf-8")
    entrypoint.chmod(0o755)
    return runtime_path
def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))
def _write_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return path
def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def test_bootstrap_apply_requires_approval_without_changing_active_runtime(tmp_path: Path) -> None:
    from mcp_broker.bootstrap_transactions import BootstrapTransactionError, BootstrapTransactionStore
    from mcp_broker.runtime_install import RuntimeInstallStore

    state_dir = tmp_path / "state"
    old_runtime = _write_runtime_dir(tmp_path / "old-runtime")
    old_manifest = RuntimeInstallStore(state_dir).record_installed_runtime(
        version="previous-runtime",
        runtime_path=old_runtime,
        entrypoint="bin/mcp-broker",
        artifact_digest=f"sha256:{'1' * 64}",
    )
    metadata_path = _write_runtime_package(tmp_path / "package", version="candidate-runtime")

    with pytest.raises(BootstrapTransactionError, match="approval"):
        BootstrapTransactionStore(state_dir).apply(metadata_path=metadata_path, approved=False)

    active = _read_json(state_dir / "runtime-install" / "active-runtime.json")
    assert active["runtime_id"] == old_manifest["runtime_id"]

def test_bootstrap_apply_preserves_active_runtime_when_smoke_fails(tmp_path: Path) -> None:
    from mcp_broker.bootstrap_transactions import BootstrapTransactionStore
    from mcp_broker.runtime_install import RuntimeInstallStore

    state_dir = tmp_path / "state"
    old_runtime = _write_runtime_dir(tmp_path / "old-runtime")
    old_manifest = RuntimeInstallStore(state_dir).record_installed_runtime(
        version="previous-runtime",
        runtime_path=old_runtime,
        entrypoint="bin/mcp-broker",
        artifact_digest=f"sha256:{'1' * 64}",
    )
    metadata_path = _write_runtime_package(tmp_path / "package", version="candidate-runtime")

    result = BootstrapTransactionStore(state_dir).apply(
        metadata_path=metadata_path,
        approved=True,
        smoke=lambda _plan: False,
    )

    active = _read_json(state_dir / "runtime-install" / "active-runtime.json")
    assert result["status"] == "failed"
    assert result["active_runtime_id"] == old_manifest["runtime_id"]
    assert active["runtime_id"] == old_manifest["runtime_id"]

def test_bootstrap_preflight_and_plan_report_runtime_state_intent(tmp_path: Path) -> None:
    from mcp_broker.bootstrap_transactions import BootstrapTransactionStore

    state_dir = tmp_path / "state"
    metadata_path = _write_runtime_package(tmp_path / "package", version="candidate-runtime")
    store = BootstrapTransactionStore(state_dir)

    preflight = store.preflight(metadata_path)
    plan = store.plan(metadata_path)

    assert preflight["status"] == "preflight_passed"
    assert preflight["would_change_runtime_state"] is False
    assert plan["status"] == "planned"
    assert plan["would_change_runtime_state"] is True
    assert plan["transaction_id"] == preflight["transaction_id"]

def test_bootstrap_apply_activates_only_verified_extracted_archive_contents(
    tmp_path: Path,
) -> None:
    from mcp_broker.bootstrap_transactions import BootstrapTransactionStore

    state_dir = tmp_path / "state"
    metadata_path = _write_runtime_package(
        tmp_path / "package",
        version="candidate-runtime",
        archive_script="#!/bin/sh\n# verified archive\nexit 0\n",
        unpacked_script="#!/bin/sh\n# tampered unpacked directory\nexit 0\n",
    )

    result = BootstrapTransactionStore(state_dir).apply(
        metadata_path=metadata_path,
        approved=True,
    )

    active = _read_json(state_dir / "runtime-install" / "active-runtime.json")
    manifest = _read_json(Path(str(active["manifest_path"])))
    activated_entrypoint = Path(str(manifest["runtime_path"])) / str(manifest["entrypoint"])
    assert result["status"] == "applied"
    assert state_dir / "runtime-install" / "extracted-runtimes" in activated_entrypoint.parents
    assert "verified archive" in activated_entrypoint.read_text(encoding="utf-8")
    assert "tampered unpacked directory" not in activated_entrypoint.read_text(encoding="utf-8")

def test_bootstrap_apply_activates_verified_tar_archive_contents(
    tmp_path: Path,
) -> None:
    from mcp_broker.bootstrap_transactions import BootstrapTransactionStore

    state_dir = tmp_path / "state"
    metadata_path = _write_runtime_tar_package(
        tmp_path / "package",
        version="candidate-runtime",
        archive_script="#!/bin/sh\n# tar archive\nexit 0\n",
    )

    result = BootstrapTransactionStore(state_dir).apply(
        metadata_path=metadata_path,
        approved=True,
    )

    active = _read_json(state_dir / "runtime-install" / "active-runtime.json")
    manifest = _read_json(Path(str(active["manifest_path"])))
    activated_entrypoint = Path(str(manifest["runtime_path"])) / str(
        manifest["entrypoint"]
    )
    assert result["status"] == "applied"
    assert "tar archive" in activated_entrypoint.read_text(encoding="utf-8")

def test_bootstrap_apply_runs_entrypoint_smoke_before_activation(tmp_path: Path) -> None:
    from mcp_broker.bootstrap_transactions import BootstrapTransactionStore
    from mcp_broker.runtime_install import RuntimeInstallStore

    state_dir = tmp_path / "state"
    old_runtime = _write_runtime_dir(tmp_path / "old-runtime")
    old_manifest = RuntimeInstallStore(state_dir).record_installed_runtime(
        version="previous-runtime",
        runtime_path=old_runtime,
        entrypoint="bin/mcp-broker",
        artifact_digest=f"sha256:{'1' * 64}",
    )
    metadata_path = _write_runtime_package(
        tmp_path / "package",
        version="candidate-runtime",
        archive_script="#!/bin/sh\nexit 42\n",
    )

    result = BootstrapTransactionStore(state_dir).apply(
        metadata_path=metadata_path,
        approved=True,
    )

    active = _read_json(state_dir / "runtime-install" / "active-runtime.json")
    assert result["status"] == "failed"
    assert result["active_runtime_id"] == old_manifest["runtime_id"]
    assert active["runtime_id"] == old_manifest["runtime_id"]

def test_bootstrap_reapply_same_metadata_does_not_delete_active_runtime_when_smoke_fails(
    tmp_path: Path,
) -> None:
    from mcp_broker.bootstrap_transactions import BootstrapTransactionStore

    state_dir = tmp_path / "state"
    metadata_path = _write_runtime_package(
        tmp_path / "package",
        version="candidate-runtime",
    )
    store = BootstrapTransactionStore(state_dir)
    first_apply = store.apply(metadata_path=metadata_path, approved=True)
    active_before = _read_json(state_dir / "runtime-install" / "active-runtime.json")
    manifest_before = _read_json(Path(str(active_before["manifest_path"])))
    entrypoint_before = Path(str(manifest_before["runtime_path"])) / str(
        manifest_before["entrypoint"]
    )

    second_apply = store.apply(
        metadata_path=metadata_path,
        approved=True,
        smoke=lambda _plan: False,
    )

    active_after = _read_json(state_dir / "runtime-install" / "active-runtime.json")
    assert first_apply["active_runtime_id"] == second_apply["active_runtime_id"]
    assert active_after["runtime_id"] == active_before["runtime_id"]
    assert entrypoint_before.is_file()

def test_bootstrap_apply_activates_runtime_after_artifact_and_smoke_pass(
    tmp_path: Path,
) -> None:
    from mcp_broker.bootstrap_transactions import BootstrapTransactionStore
    from mcp_broker.runtime_install import RuntimeInstallStore

    state_dir = tmp_path / "state"
    old_runtime = _write_runtime_dir(tmp_path / "old-runtime")
    old_manifest = RuntimeInstallStore(state_dir).record_installed_runtime(
        version="previous-runtime",
        runtime_path=old_runtime,
        entrypoint="bin/mcp-broker",
        artifact_digest=f"sha256:{'1' * 64}",
    )
    metadata_path = _write_runtime_package(tmp_path / "package", version="candidate-runtime")

    result = BootstrapTransactionStore(state_dir).apply(
        metadata_path=metadata_path,
        approved=True,
        smoke=lambda plan: plan["version"] == "candidate-runtime",
    )

    active = _read_json(state_dir / "runtime-install" / "active-runtime.json")
    previous = _read_json(state_dir / "runtime-install" / "previous-runtime.json")
    assert result["status"] == "applied"
    assert result["active_runtime_id"] == active["runtime_id"]
    assert active["runtime_id"] != old_manifest["runtime_id"]
    assert previous["runtime_id"] == old_manifest["runtime_id"]

def test_bootstrap_rollback_requires_approval_and_restores_previous_runtime(
    tmp_path: Path,
) -> None:
    from mcp_broker.bootstrap_transactions import BootstrapTransactionError, BootstrapTransactionStore
    from mcp_broker.runtime_install import RuntimeInstallStore

    state_dir = tmp_path / "state"
    old_runtime = _write_runtime_dir(tmp_path / "old-runtime")
    old_manifest = RuntimeInstallStore(state_dir).record_installed_runtime(
        version="previous-runtime",
        runtime_path=old_runtime,
        entrypoint="bin/mcp-broker",
        artifact_digest=f"sha256:{'1' * 64}",
    )
    metadata_path = _write_runtime_package(tmp_path / "package", version="candidate-runtime")
    store = BootstrapTransactionStore(state_dir)
    store.apply(metadata_path=metadata_path, approved=True)

    with pytest.raises(BootstrapTransactionError, match="approval"):
        store.rollback(approved=False)

    rollback = store.rollback(approved=True)
    active = _read_json(state_dir / "runtime-install" / "active-runtime.json")
    assert rollback["status"] == "rolled_back"
    assert rollback["active_runtime_id"] == old_manifest["runtime_id"]
    assert active["runtime_id"] == old_manifest["runtime_id"]

def test_bootstrap_rollback_validates_previous_runtime_before_swap(
    tmp_path: Path,
) -> None:
    from mcp_broker.bootstrap_transactions import BootstrapTransactionError, BootstrapTransactionStore
    from mcp_broker.runtime_install import RuntimeInstallStore

    state_dir = tmp_path / "state"
    old_runtime = _write_runtime_dir(tmp_path / "old-runtime")
    old_manifest = RuntimeInstallStore(state_dir).record_installed_runtime(
        version="previous-runtime",
        runtime_path=old_runtime,
        entrypoint="bin/mcp-broker",
        artifact_digest=f"sha256:{'1' * 64}",
    )
    metadata_path = _write_runtime_package(tmp_path / "package", version="candidate-runtime")
    store = BootstrapTransactionStore(state_dir)
    applied = store.apply(metadata_path=metadata_path, approved=True)
    Path(str(old_manifest["runtime_path"])).joinpath("bin", "mcp-broker").unlink()

    with pytest.raises(BootstrapTransactionError, match="previous runtime"):
        store.rollback(approved=True)

    active = _read_json(state_dir / "runtime-install" / "active-runtime.json")
    assert active["runtime_id"] == applied["active_runtime_id"]

def test_bootstrap_rollback_rejects_missing_active_pointer(tmp_path: Path) -> None:
    from mcp_broker.bootstrap_transactions import BootstrapTransactionError, BootstrapTransactionStore

    store = BootstrapTransactionStore(tmp_path / "state")

    with pytest.raises(BootstrapTransactionError, match="active runtime pointer"):
        store.rollback(approved=True)

def test_bootstrap_rollback_rejects_missing_previous_pointer(tmp_path: Path) -> None:
    from mcp_broker.bootstrap_transactions import BootstrapTransactionError, BootstrapTransactionStore
    from mcp_broker.runtime_install import RuntimeInstallStore

    state_dir = tmp_path / "state"
    RuntimeInstallStore(state_dir).record_installed_runtime(
        version="active-runtime",
        runtime_path=_write_runtime_dir(tmp_path / "active-runtime"),
        entrypoint="bin/mcp-broker",
        artifact_digest=f"sha256:{'1' * 64}",
    )

    with pytest.raises(BootstrapTransactionError, match="previous runtime pointer"):
        BootstrapTransactionStore(state_dir).rollback(approved=True)

def test_bootstrap_rollback_rejects_previous_manifest_outside_install_root(
    tmp_path: Path,
) -> None:
    from mcp_broker.bootstrap_transactions import BootstrapTransactionError, BootstrapTransactionStore
    from mcp_broker.runtime_install import RuntimeInstallStore

    state_dir = tmp_path / "state"
    active = RuntimeInstallStore(state_dir).record_installed_runtime(
        version="active-runtime",
        runtime_path=_write_runtime_dir(tmp_path / "active-runtime"),
        entrypoint="bin/mcp-broker",
        artifact_digest=f"sha256:{'1' * 64}",
    )
    outside_manifest = tmp_path / "outside-runtime-manifest.json"
    outside_manifest.write_text(json.dumps({"runtime_id": "outside"}), encoding="utf-8")
    (state_dir / "runtime-install" / "previous-runtime.json").write_text(
        json.dumps(
            {
                "manifest_path": str(outside_manifest),
                "runtime_id": "outside",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(BootstrapTransactionError, match="outside runtime install root"):
        BootstrapTransactionStore(state_dir).rollback(approved=True)

    assert _read_json(state_dir / "runtime-install" / "active-runtime.json")[
        "runtime_id"
    ] == active["runtime_id"]

def test_bootstrap_rollback_rejects_previous_pointer_manifest_mismatch(
    tmp_path: Path,
) -> None:
    from mcp_broker.bootstrap_transactions import BootstrapTransactionError, BootstrapTransactionStore
    from mcp_broker.runtime_install import RuntimeInstallStore

    state_dir = tmp_path / "state"
    first = RuntimeInstallStore(state_dir).record_installed_runtime(
        version="previous-runtime",
        runtime_path=_write_runtime_dir(tmp_path / "previous-runtime"),
        entrypoint="bin/mcp-broker",
        artifact_digest=f"sha256:{'1' * 64}",
    )
    RuntimeInstallStore(state_dir).record_installed_runtime(
        version="active-runtime",
        runtime_path=_write_runtime_dir(tmp_path / "active-runtime"),
        entrypoint="bin/mcp-broker",
        artifact_digest=f"sha256:{'2' * 64}",
    )
    manifest_path = Path(str(first["manifest_path"]))
    manifest = _read_json(manifest_path)
    manifest["runtime_id"] = "tampered-runtime"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BootstrapTransactionError, match="pointer does not match manifest"):
        BootstrapTransactionStore(state_dir).rollback(approved=True)

def test_bootstrap_status_rejects_latest_pointer_outside_records_dir(
    tmp_path: Path,
) -> None:
    from mcp_broker.bootstrap_transactions import BootstrapTransactionError, BootstrapTransactionStore

    state_dir = tmp_path / "state"
    metadata_path = _write_runtime_package(tmp_path / "package", version="candidate-runtime")
    store = BootstrapTransactionStore(state_dir)
    store.apply(metadata_path=metadata_path, approved=True)
    outside_record = tmp_path / "outside.json"
    outside_record.write_text('{"status": "tampered"}\n', encoding="utf-8")
    (state_dir / "bootstrap-transactions" / "latest.json").write_text(
        json.dumps({"record_path": str(outside_record), "transaction_id": "tampered"}),
        encoding="utf-8",
    )

    with pytest.raises(BootstrapTransactionError, match="latest transaction"):
        store.status()

def test_bootstrap_status_rejects_record_transaction_mismatch(
    tmp_path: Path,
) -> None:
    from mcp_broker.bootstrap_transactions import BootstrapTransactionError, BootstrapTransactionStore

    state_dir = tmp_path / "state"
    metadata_path = _write_runtime_package(tmp_path / "package", version="candidate-runtime")
    store = BootstrapTransactionStore(state_dir)
    store.apply(metadata_path=metadata_path, approved=True)
    latest_path = state_dir / "bootstrap-transactions" / "latest.json"
    latest = _read_json(latest_path)
    record_path = Path(str(latest["record_path"]))
    record = _read_json(record_path)
    record["transaction_id"] = "0000000000000000"
    record_path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(BootstrapTransactionError, match="latest transaction"):
        store.status()

def test_bootstrap_status_and_uninstall_are_approval_gated(tmp_path: Path) -> None:
    from mcp_broker.bootstrap_transactions import BootstrapTransactionError, BootstrapTransactionStore

    state_dir = tmp_path / "state"
    metadata_path = _write_runtime_package(tmp_path / "package", version="candidate-runtime")
    store = BootstrapTransactionStore(state_dir)
    store.apply(metadata_path=metadata_path, approved=True)

    with pytest.raises(BootstrapTransactionError, match="approval"):
        store.uninstall(approved=False)

    uninstall = store.uninstall(approved=True)
    status = store.status()
    assert uninstall["status"] == "uninstalled"
    assert status["latest_transaction"]["status"] == "uninstalled"

def test_bootstrap_status_without_latest_transaction_reports_active_runtime(
    tmp_path: Path,
) -> None:
    from mcp_broker.bootstrap_transactions import BootstrapTransactionStore
    from mcp_broker.runtime_install import RuntimeInstallStore

    state_dir = tmp_path / "state"
    active = RuntimeInstallStore(state_dir).record_installed_runtime(
        version="active-runtime",
        runtime_path=_write_runtime_dir(tmp_path / "active-runtime"),
        entrypoint="bin/mcp-broker",
        artifact_digest=f"sha256:{'1' * 64}",
    )

    status = BootstrapTransactionStore(state_dir).status()

    assert status == {
        "active_runtime_id": active["runtime_id"],
        "latest_transaction": None,
        "status": "ok",
    }

def test_bootstrap_uninstall_without_active_runtime_records_empty_uninstall(
    tmp_path: Path,
) -> None:
    from mcp_broker.bootstrap_transactions import BootstrapTransactionStore

    result = BootstrapTransactionStore(tmp_path / "state").uninstall(approved=True)

    assert result["previous_runtime_id"] is None
    assert result["status"] == "uninstalled"

def test_bootstrap_preflight_cli_reports_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from mcp_broker.cli import main

    metadata_path = _write_runtime_package(tmp_path / "package", version="candidate-runtime")

    assert (
        main(
            [
                "runtime",
                "bootstrap",
                "preflight",
                "--metadata",
                str(metadata_path),
                "--state-dir",
                str(tmp_path / "state"),
            ]
        )
        == 0
    )

    assert '"status": "preflight_passed"' in capsys.readouterr().out

def test_bootstrap_cli_reports_plan_apply_status_uninstall_and_rollback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.cli import main

    state_dir = tmp_path / "state"
    previous_runtime = _write_runtime_dir(tmp_path / "previous-runtime")
    from mcp_broker.runtime_install import RuntimeInstallStore

    RuntimeInstallStore(state_dir).record_installed_runtime(
        version="previous-runtime",
        runtime_path=previous_runtime,
        entrypoint="bin/mcp-broker",
        artifact_digest=f"sha256:{'1' * 64}",
    )
    metadata_path = _write_runtime_package(tmp_path / "package", version="candidate-runtime")

    assert main(["runtime", "bootstrap", "plan", "--metadata", str(metadata_path), "--state-dir", str(state_dir)]) == 0
    assert '"status": "planned"' in capsys.readouterr().out

    assert (
        main(
            [
                "runtime",
                "bootstrap",
                "apply",
                "--metadata",
                str(metadata_path),
                "--state-dir",
                str(state_dir),
                "--approved",
            ]
        )
        == 0
    )
    assert '"status": "applied"' in capsys.readouterr().out

    assert main(["runtime", "bootstrap", "status", "--state-dir", str(state_dir)]) == 0
    assert '"status": "ok"' in capsys.readouterr().out

    assert (
        main(["runtime", "bootstrap", "rollback", "--state-dir", str(state_dir), "--approved"])
        == 0
    )
    assert '"status": "rolled_back"' in capsys.readouterr().out

    assert (
        main(["runtime", "bootstrap", "uninstall", "--state-dir", str(state_dir), "--approved"])
        == 0
    )
    assert '"status": "uninstalled"' in capsys.readouterr().out
