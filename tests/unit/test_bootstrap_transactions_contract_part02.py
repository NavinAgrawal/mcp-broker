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

def test_bootstrap_cli_reports_runtime_artifact_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.cli import main

    assert (
        main(
            [
                "runtime",
                "bootstrap",
                "preflight",
                "--metadata",
                str(tmp_path / "missing.json"),
                "--state-dir",
                str(tmp_path / "state"),
            ]
        )
        == 1
    )

    assert "runtime artifact metadata not found" in capsys.readouterr().err

def test_bootstrap_parse_args_preserves_subcommand_contract(tmp_path: Path) -> None:
    from mcp_broker.bootstrap_transactions import _parse_args

    metadata_path = tmp_path / "runtime-metadata.json"
    state_dir = tmp_path / "state"

    plan_args = _parse_args(
        [
            "plan",
            "--metadata",
            str(metadata_path),
            "--state-dir",
            str(state_dir),
        ]
    )
    apply_args = _parse_args(
        [
            "apply",
            "--metadata",
            str(metadata_path),
            "--state-dir",
            str(state_dir),
            "--approved",
        ]
    )
    status_args = _parse_args(["status", "--state-dir", str(state_dir)])
    rollback_args = _parse_args(["rollback", "--state-dir", str(state_dir), "--approved"])
    uninstall_args = _parse_args(["uninstall", "--state-dir", str(state_dir), "--approved"])

    assert plan_args.bootstrap_command == "plan"
    assert plan_args.metadata == metadata_path
    assert plan_args.state_dir == state_dir
    assert not hasattr(plan_args, "approved")
    assert apply_args.bootstrap_command == "apply"
    assert apply_args.metadata == metadata_path
    assert apply_args.state_dir == state_dir
    assert apply_args.approved is True
    assert status_args.bootstrap_command == "status"
    assert status_args.state_dir == state_dir
    assert rollback_args.bootstrap_command == "rollback"
    assert rollback_args.approved is True
    assert uninstall_args.bootstrap_command == "uninstall"
    assert uninstall_args.approved is True

def test_bootstrap_apply_rejects_unsupported_runtime_archive(tmp_path: Path) -> None:
    from mcp_broker.bootstrap_transactions import BootstrapTransactionError, BootstrapTransactionStore

    package_dir = tmp_path / "package"
    package_dir.mkdir()
    runtime_path = _write_runtime_dir(package_dir / "runtime")
    artifact_path = package_dir / "runtime.bin"
    artifact_path.write_text("not an archive", encoding="utf-8")
    metadata_path = _write_json(
        package_dir / "runtime-metadata.json",
        {
            "artifact_digest": f"sha256:{_sha256(artifact_path)}",
            "artifact_path": "runtime.bin",
            "entrypoint": "bin/mcp-broker",
            "runtime_path": runtime_path.name,
            "version": "candidate-runtime",
        },
    )

    with pytest.raises(BootstrapTransactionError, match="unsupported runtime artifact archive"):
        BootstrapTransactionStore(tmp_path / "state").apply(metadata_path=metadata_path, approved=True)

def test_bootstrap_extract_candidate_rejects_unsupported_archive_after_plan_validation(
    tmp_path: Path,
) -> None:
    from mcp_broker.bootstrap_transactions import BootstrapTransactionError, BootstrapTransactionStore

    artifact_path = tmp_path / "runtime.bin"
    artifact_path.write_text("not an archive", encoding="utf-8")
    plan = {
        "artifact_path": str(artifact_path),
        "entrypoint": "bin/mcp-broker",
        "transaction_id": "0123456789abcdef",
    }

    with pytest.raises(BootstrapTransactionError, match="unsupported runtime artifact archive"):
        BootstrapTransactionStore(tmp_path / "state")._extract_verified_runtime_candidate(plan)

@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ([], "runtime metadata must be an object"),
        ({"version": ""}, "runtime metadata missing version"),
    ],
)
def test_bootstrap_plan_rejects_invalid_metadata_shape(
    tmp_path: Path,
    payload: object,
    match: str,
) -> None:
    from mcp_broker.bootstrap_transactions import BootstrapTransactionError, BootstrapTransactionStore

    metadata_path = tmp_path / "runtime-metadata.json"
    metadata_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(BootstrapTransactionError, match=match):
        BootstrapTransactionStore(tmp_path / "state").plan(metadata_path)

@pytest.mark.parametrize(
    ("runtime_path", "match"),
    [
        ("/tmp/outside-runtime", "runtime path must stay inside metadata directory"),
        ("../outside-runtime", "runtime path must stay inside metadata directory"),
        ("missing-runtime", "runtime path not found"),
    ],
)
def test_bootstrap_plan_rejects_unsafe_or_missing_runtime_path(
    tmp_path: Path,
    runtime_path: str,
    match: str,
) -> None:
    from mcp_broker.bootstrap_transactions import BootstrapTransactionError, BootstrapTransactionStore

    package_dir = tmp_path / "package"
    package_dir.mkdir()
    artifact_path = package_dir / "runtime.zip"
    with zipfile.ZipFile(artifact_path, "w") as archive:
        archive.writestr("bin/mcp-broker", "#!/bin/sh\nexit 0\n")
    metadata_path = _write_json(
        package_dir / "runtime-metadata.json",
        {
            "artifact_digest": f"sha256:{_sha256(artifact_path)}",
            "artifact_path": artifact_path.name,
            "entrypoint": "bin/mcp-broker",
            "runtime_path": runtime_path,
            "version": "candidate-runtime",
        },
    )

    with pytest.raises(BootstrapTransactionError, match=match):
        BootstrapTransactionStore(tmp_path / "state").plan(metadata_path)

def test_bootstrap_metadata_runtime_path_rejects_symlink_escape(tmp_path: Path) -> None:
    from mcp_broker.bootstrap_transactions import BootstrapTransactionError, _metadata_runtime_path

    metadata_dir = tmp_path / "metadata"
    metadata_dir.mkdir()
    outside_runtime = _write_runtime_dir(tmp_path / "outside-runtime")
    (metadata_dir / "runtime-link").symlink_to(outside_runtime, target_is_directory=True)

    with pytest.raises(BootstrapTransactionError, match="runtime path must stay inside metadata directory"):
        _metadata_runtime_path(metadata_dir, "runtime-link")

def test_bootstrap_plan_rejects_entrypoint_traversal(tmp_path: Path) -> None:
    from mcp_broker.bootstrap_transactions import BootstrapTransactionError, BootstrapTransactionStore

    package_dir = tmp_path / "package"
    runtime_path = _write_runtime_dir(package_dir / "runtime")
    artifact_path = package_dir / "runtime.zip"
    with zipfile.ZipFile(artifact_path, "w") as archive:
        archive.writestr("bin/mcp-broker", "#!/bin/sh\nexit 0\n")
    metadata_path = _write_json(
        package_dir / "runtime-metadata.json",
        {
            "artifact_digest": f"sha256:{_sha256(artifact_path)}",
            "artifact_path": artifact_path.name,
            "entrypoint": "../outside",
            "runtime_path": runtime_path.name,
            "version": "candidate-runtime",
        },
    )

    with pytest.raises(BootstrapTransactionError, match="runtime entrypoint must stay inside runtime path"):
        BootstrapTransactionStore(tmp_path / "state").plan(metadata_path)

def test_bootstrap_helpers_reject_invalid_json_and_missing_fields(tmp_path: Path) -> None:
    from mcp_broker.bootstrap_transactions import (
        BootstrapTransactionError,
        _load_metadata,
        _json_string,
        _read_json,
        _required_string,
    )

    invalid_json = tmp_path / "invalid.json"
    invalid_json.write_text("{", encoding="utf-8")
    non_object_json = tmp_path / "array.json"
    non_object_json.write_text("[]", encoding="utf-8")
    metadata_json = tmp_path / "metadata.json"
    metadata_json.write_text('{"version":"1.0.0"}', encoding="utf-8")

    with pytest.raises(BootstrapTransactionError, match="invalid runtime bootstrap JSON"):
        _read_json(invalid_json)
    with pytest.raises(BootstrapTransactionError, match="expected JSON object"):
        _read_json(non_object_json)
    with pytest.raises(BootstrapTransactionError, match="runtime bootstrap JSON missing runtime_id"):
        _json_string({}, "runtime_id", non_object_json)
    with pytest.raises(BootstrapTransactionError, match="runtime metadata must be an object"):
        _load_metadata(non_object_json)
    with pytest.raises(BootstrapTransactionError, match="runtime metadata missing version"):
        _required_string({"version": "  "}, "version", metadata_json)
    assert _load_metadata(metadata_json) == {"version": "1.0.0"}
    assert _required_string({"version": "1.0.0"}, "version", metadata_json) == "1.0.0"

def test_bootstrap_pointer_helpers_require_valid_manifest_fields(tmp_path: Path) -> None:
    from mcp_broker.bootstrap_transactions import (
        BootstrapTransactionError,
        _pointer_id,
        _read_pointer,
        _require_pointer,
        _write_json_atomic,
    )

    pointer_path = tmp_path / "runtime-install" / "active-runtime.json"

    assert _read_pointer(pointer_path) is None
    assert _pointer_id(None) is None
    with pytest.raises(BootstrapTransactionError, match="active missing"):
        _require_pointer(pointer_path, "active missing")

    _write_json_atomic(
        pointer_path,
        {
            "manifest_path": str(tmp_path / "runtime-manifest.json"),
            "runtime_id": "runtime-001",
        },
    )
    pointer = _read_pointer(pointer_path)
    assert pointer == {
        "manifest_path": str(tmp_path / "runtime-manifest.json"),
        "runtime_id": "runtime-001",
    }
    assert _require_pointer(pointer_path, "active missing") == pointer
    assert _pointer_id(pointer) == "runtime-001"

    _write_json_atomic(pointer_path, {"manifest_path": str(tmp_path / "runtime-manifest.json")})
    with pytest.raises(BootstrapTransactionError, match="runtime_id"):
        _read_pointer(pointer_path)

def test_bootstrap_latest_pointer_accepts_only_record_under_records_dir(tmp_path: Path) -> None:
    from mcp_broker.bootstrap_transactions import (
        BootstrapTransactionError,
        _read_latest_pointer,
        _write_json_atomic,
    )

    records_dir = tmp_path / "records"
    transaction_id = "0123456789abcdef"
    record_path = records_dir / f"{transaction_id}.json"
    latest_path = tmp_path / "latest.json"
    _write_json_atomic(latest_path, {"record_path": str(record_path), "transaction_id": transaction_id})

    assert _read_latest_pointer(latest_path, records_dir=records_dir) == {
        "record_path": str(record_path),
        "transaction_id": transaction_id,
    }

    _write_json_atomic(
        latest_path,
        {"record_path": str(records_dir / "other.json"), "transaction_id": transaction_id},
    )
    with pytest.raises(BootstrapTransactionError, match="latest transaction pointer"):
        _read_latest_pointer(latest_path, records_dir=records_dir)

    _write_json_atomic(
        latest_path,
        {"record_path": str(record_path), "transaction_id": "0123456789abcdeg"},
    )
    with pytest.raises(BootstrapTransactionError, match="latest transaction pointer"):
        _read_latest_pointer(latest_path, records_dir=records_dir)

@pytest.mark.error_simulation
def test_bootstrap_default_smoke_returns_false_for_subprocess_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    from mcp_broker.bootstrap_transactions import _default_smoke

    runtime_path = _write_runtime_dir(tmp_path / "runtime")
    plan = {"runtime_path": str(runtime_path), "entrypoint": "bin/mcp-broker"}

    def raise_os_error(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise OSError("blocked")

    monkeypatch.setattr(subprocess, "run", raise_os_error)

    assert _default_smoke(plan, timeout_seconds=0.01) is False

@pytest.mark.error_simulation
def test_bootstrap_default_smoke_uses_entrypoint_cwd_args_and_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    from mcp_broker.bootstrap_transactions import (
        BOOTSTRAP_SMOKE_ARGS,
        _default_smoke,
    )

    runtime_path = _write_runtime_dir(tmp_path / "runtime")
    plan = {"runtime_path": str(runtime_path), "entrypoint": "bin/mcp-broker"}
    calls: list[dict[str, object]] = []

    def fake_run(
        args: list[str],
        *,
        cwd: str,
        stdout: int,
        stderr: int,
        check: bool,
        timeout: float,
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append(
            {
                "args": args,
                "cwd": cwd,
                "stdout": stdout,
                "stderr": stderr,
                "check": check,
                "timeout": timeout,
            }
        )
        return subprocess.CompletedProcess(args=args, returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert _default_smoke(plan, timeout_seconds=3.5) is True
    assert calls == [
        {
            "args": [str(runtime_path / "bin" / "mcp-broker"), *BOOTSTRAP_SMOKE_ARGS],
            "cwd": str(runtime_path),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "check": False,
            "timeout": 3.5,
        }
    ]

@pytest.mark.error_simulation
def test_bootstrap_default_smoke_returns_false_for_nonzero_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    from mcp_broker.bootstrap_transactions import _default_smoke

    runtime_path = _write_runtime_dir(tmp_path / "runtime")
    plan = {"runtime_path": str(runtime_path), "entrypoint": "bin/mcp-broker"}

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **_kwargs: subprocess.CompletedProcess(args=args, returncode=7),
    )

    assert _default_smoke(plan, timeout_seconds=0.01) is False

def test_bootstrap_extract_zip_rejects_symlinks_and_preserves_directories(
    tmp_path: Path,
) -> None:
    from mcp_broker.bootstrap_transactions import (
        BootstrapTransactionError,
        _extract_zip_archive,
    )

    zip_with_dir = tmp_path / "dir.zip"
    with zipfile.ZipFile(zip_with_dir, "w") as archive:
        archive.writestr("bin/", "")
        archive.writestr("bin/mcp-broker", "#!/bin/sh\nexit 0\n")
    destination = tmp_path / "zip-destination"

    _extract_zip_archive(zip_with_dir, destination)

    assert (destination / "bin").is_dir()

    zip_with_symlink = tmp_path / "symlink.zip"
    with zipfile.ZipFile(zip_with_symlink, "w") as archive:
        info = zipfile.ZipInfo("bin/link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "target")

    with pytest.raises(BootstrapTransactionError, match="unsafe archive member"):
        _extract_zip_archive(zip_with_symlink, tmp_path / "symlink-destination")

@pytest.mark.error_simulation
def test_bootstrap_extract_zip_accepts_file_without_mode_bits(tmp_path: Path) -> None:
    from mcp_broker import bootstrap_transactions
    from mcp_broker.bootstrap_transactions import _extract_zip_archive

    class ZipInfoStub:
        filename = "bin/mcp-broker"
        external_attr = 0

        def is_dir(self) -> bool:
            return False

    class ZipArchiveStub:
        def __enter__(self) -> ZipArchiveStub:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def infolist(self) -> list[ZipInfoStub]:
            return [ZipInfoStub()]

        def open(self, _info: ZipInfoStub) -> io.BytesIO:
            return io.BytesIO(b"#!/bin/sh\nexit 0\n")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(bootstrap_transactions.zipfile, "ZipFile", lambda _path: ZipArchiveStub())

    destination = tmp_path / "zip-destination"
    try:
        _extract_zip_archive(tmp_path / "no-mode.zip", destination)
    finally:
        monkeypatch.undo()

    assert (destination / "bin" / "mcp-broker").read_text(encoding="utf-8") == "#!/bin/sh\nexit 0\n"

def test_bootstrap_extract_tar_rejects_links_and_preserves_directories(
    tmp_path: Path,
) -> None:
    from mcp_broker.bootstrap_transactions import (
        BootstrapTransactionError,
        _extract_tar_archive,
    )

    tar_with_dir = tmp_path / "dir.tar"
    with tarfile.open(tar_with_dir, "w") as archive:
        directory = tarfile.TarInfo("bin")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        archive.addfile(directory)
        payload = b"#!/bin/sh\nexit 0\n"
        file_info = tarfile.TarInfo("bin/mcp-broker")
        file_info.size = len(payload)
        file_info.mode = 0o755
        archive.addfile(file_info, io.BytesIO(payload))
    destination = tmp_path / "tar-destination"

    _extract_tar_archive(tar_with_dir, destination)

    assert (destination / "bin").is_dir()

    tar_with_link = tmp_path / "link.tar"
    with tarfile.open(tar_with_link, "w") as archive:
        link = tarfile.TarInfo("bin/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "mcp-broker"
        archive.addfile(link)

    with pytest.raises(BootstrapTransactionError, match="unsafe archive member"):
        _extract_tar_archive(tar_with_link, tmp_path / "link-destination")

@pytest.mark.error_simulation
def test_bootstrap_extract_tar_rejects_unreadable_file_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import bootstrap_transactions
    from mcp_broker.bootstrap_transactions import BootstrapTransactionError, _extract_tar_archive

    class ArchiveStub:
        def __enter__(self) -> ArchiveStub:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def getmembers(self) -> list[tarfile.TarInfo]:
            member = tarfile.TarInfo("bin/mcp-broker")
            member.size = 1
            member.mode = 0o755
            return [member]

        def extractfile(self, _member: tarfile.TarInfo) -> None:
            return None

    monkeypatch.setattr(bootstrap_transactions.tarfile, "open", lambda _path: ArchiveStub())

    with pytest.raises(BootstrapTransactionError, match="runtime archive member unreadable"):
        _extract_tar_archive(tmp_path / "runtime.tar", tmp_path / "destination")

@pytest.mark.parametrize("member_name", ["", ".", "../escape", "a/../escape", r"C:\escape"])
def test_bootstrap_safe_archive_member_rejects_unsafe_names(member_name: str) -> None:
    from mcp_broker.bootstrap_transactions import BootstrapTransactionError, _safe_archive_member

    with pytest.raises(BootstrapTransactionError, match="unsafe archive member"):
        _safe_archive_member(member_name)

@pytest.mark.parametrize(
    ("member_name", "normalized"),
    [
        ("bin/mcp-broker", "bin/mcp-broker"),
        ("./bin/mcp-broker", "bin/mcp-broker"),
    ],
)
def test_bootstrap_safe_archive_member_normalizes_safe_names(
    member_name: str,
    normalized: str,
) -> None:
    from mcp_broker.bootstrap_transactions import _safe_archive_member

    assert _safe_archive_member(member_name) == normalized

def test_bootstrap_archive_target_rejects_resolved_escape(tmp_path: Path) -> None:
    from mcp_broker.bootstrap_transactions import BootstrapTransactionError, _archive_target

    with pytest.raises(BootstrapTransactionError, match="unsafe archive member"):
        _archive_target(tmp_path / "destination", "../escape")
