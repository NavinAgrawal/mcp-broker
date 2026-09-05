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

def test_bootstrap_archive_target_accepts_nested_member(tmp_path: Path) -> None:
    from mcp_broker.bootstrap_transactions import _archive_target

    destination = tmp_path / "destination"

    assert _archive_target(destination, "bin/mcp-broker") == (
        destination / "bin" / "mcp-broker"
    ).resolve(strict=False)

def test_bootstrap_prepare_empty_dir_replaces_existing_candidate(tmp_path: Path) -> None:
    from mcp_broker.bootstrap_transactions import _prepare_empty_dir

    root = tmp_path / "root"
    candidate = root / "candidate"
    stale = candidate / "stale.txt"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale", encoding="utf-8")

    _prepare_empty_dir(candidate, root)

    assert candidate.is_dir()
    assert not stale.exists()

def test_bootstrap_remove_extracted_runtime_ignores_outside_paths(tmp_path: Path) -> None:
    from mcp_broker.bootstrap_transactions import _remove_extracted_runtime

    root = tmp_path / "root"
    outside = tmp_path / "outside"
    outside.mkdir()

    _remove_extracted_runtime(outside, root)

    assert outside.is_dir()

def test_bootstrap_prepare_empty_dir_rejects_paths_outside_allowed_root(tmp_path: Path) -> None:
    from mcp_broker.bootstrap_transactions import BootstrapTransactionError, _prepare_empty_dir

    with pytest.raises(BootstrapTransactionError, match="outside runtime install root"):
        _prepare_empty_dir(tmp_path / "outside", tmp_path / "root")

@pytest.mark.parametrize(
    ("transaction_id", "valid"),
    [
        ("0123456789abcdef", True),
        ("0123456789abcde", False),
        ("0123456789abcdeg", False),
        ("0123456789ABCDEF", False),
    ],
)
def test_bootstrap_transaction_id_validator_contract(transaction_id: str, valid: bool) -> None:
    from mcp_broker.bootstrap_transactions import _valid_transaction_id

    assert _valid_transaction_id(transaction_id) is valid

@pytest.mark.parametrize(
    ("path", "is_windows"),
    [
        ("C:/runtime/bin/mcp-broker", True),
        ("z:/runtime", True),
        ("runtime/C:/bin", False),
        ("/tmp/C:/runtime", False),
        ("runtime/bin/mcp-broker", False),
    ],
)
def test_bootstrap_windows_drive_path_detector_contract(path: str, is_windows: bool) -> None:
    from mcp_broker.bootstrap_transactions import _is_windows_drive_path

    assert _is_windows_drive_path(path) is is_windows

def test_bootstrap_transaction_id_is_stable_hash_prefix() -> None:
    from mcp_broker.bootstrap_transactions import _transaction_id

    payload = {"version": "candidate-runtime", "entrypoint": "bin/mcp-broker"}
    expected = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]

    assert _transaction_id(payload) == expected

def test_bootstrap_utc_now_uses_zulu_iso_timestamp() -> None:
    from mcp_broker.bootstrap_transactions import _utc_now

    timestamp = _utc_now()

    assert timestamp.endswith("Z")
    assert "+00:00" not in timestamp
    assert "T" in timestamp

@pytest.mark.error_simulation
def test_bootstrap_main_reports_unknown_dispatch_command(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    import argparse

    import mcp_broker.bootstrap_transactions as bootstrap_transactions

    monkeypatch.setattr(
        bootstrap_transactions,
        "_parse_args",
        lambda _argv: argparse.Namespace(
            bootstrap_command="unknown",
            state_dir=tmp_path / "state",
        ),
    )

    assert bootstrap_transactions.main([]) == 1
    assert "unknown bootstrap command" in capsys.readouterr().err
