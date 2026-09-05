from __future__ import annotations

import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tarfile

import pytest


pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("argv", "required_text"),
    [
        ([], "the following arguments are required: bootstrap_command"),
        (["preflight", "--state-dir", "state"], "--metadata"),
        (["plan", "--metadata", "metadata.json"], "--state-dir"),
        (["status"], "--state-dir"),
        (["rollback"], "--state-dir"),
    ],
)
def test_bootstrap_parse_args_rejects_missing_required_inputs(
    argv: list[str],
    required_text: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.bootstrap_transactions import _parse_args

    with pytest.raises(SystemExit) as raised:
        _parse_args(argv)

    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert captured.err.startswith("usage:")
    assert required_text in captured.err


def test_bootstrap_parse_args_help_keeps_operator_description(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.bootstrap_transactions import _parse_args

    with pytest.raises(SystemExit) as raised:
        _parse_args(["--help"])

    captured = capsys.readouterr()
    assert raised.value.code == 0
    assert "\nManage plugin bootstrap transactions\n" in captured.out
    assert captured.err == ""


def test_bootstrap_main_emits_sorted_single_line_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.bootstrap_transactions import main

    result = main(["status", "--state-dir", str(tmp_path / "state")])

    captured = capsys.readouterr()
    assert result == 0
    assert captured.err == ""
    assert captured.out == (
        '{"active_runtime_id": null, "latest_transaction": null, "status": "ok"}\n'
    )


def test_bootstrap_report_serialization_sorts_unsorted_payload() -> None:
    from mcp_broker.bootstrap_transactions import _serialize_report

    assert _serialize_report({"z": 2, "a": 1}) == '{"a": 1, "z": 2}'


def test_bootstrap_json_readers_request_deterministic_utf8(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker.bootstrap_transactions import _load_metadata, _read_json

    calls: list[tuple[Path, tuple[object, ...], dict[str, object]]] = []

    def open_spy(
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> io.StringIO:
        calls.append((path, args, kwargs))
        return io.StringIO('{"name":"caf\u00e9"}')

    monkeypatch.setattr(Path, "open", open_spy)
    metadata_path = Path("metadata.json")
    pointer_path = Path("pointer.json")

    assert _load_metadata(metadata_path) == {"name": "café"}
    assert _read_json(pointer_path) == {"name": "café"}
    assert calls == [
        (metadata_path, ("r",), {"encoding": "utf-8"}),
        (pointer_path, ("r",), {"encoding": "utf-8"}),
    ]


def test_bootstrap_metadata_path_errors_are_stable_and_specific(tmp_path: Path) -> None:
    from mcp_broker.bootstrap_transactions import BootstrapTransactionError, _metadata_runtime_path

    metadata_dir = tmp_path / "metadata"
    metadata_dir.mkdir()

    with pytest.raises(BootstrapTransactionError) as absolute_error:
        _metadata_runtime_path(metadata_dir, str(tmp_path / "outside"))
    with pytest.raises(BootstrapTransactionError) as traversal_error:
        _metadata_runtime_path(metadata_dir, "../outside")
    with pytest.raises(BootstrapTransactionError) as missing_error:
        _metadata_runtime_path(metadata_dir, "missing")

    expected_boundary = "runtime path must stay inside metadata directory"
    assert str(absolute_error.value) == expected_boundary
    assert str(traversal_error.value) == expected_boundary
    assert str(missing_error.value) == f"runtime path not found: {metadata_dir / 'missing'}"


def test_bootstrap_metadata_path_reports_symlink_escape_boundary(tmp_path: Path) -> None:
    from mcp_broker.bootstrap_transactions import BootstrapTransactionError, _metadata_runtime_path

    metadata_dir = tmp_path / "metadata"
    outside = tmp_path / "outside"
    metadata_dir.mkdir()
    outside.mkdir()
    (metadata_dir / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(BootstrapTransactionError) as raised:
        _metadata_runtime_path(metadata_dir, "escape")

    assert str(raised.value) == "runtime path must stay inside metadata directory"


def test_bootstrap_entrypoint_rejects_missing_and_nonexecutable_files(tmp_path: Path) -> None:
    from mcp_broker.bootstrap_transactions import BootstrapTransactionError, _require_runtime_entrypoint

    runtime_path = tmp_path / "runtime"
    runtime_path.mkdir()

    with pytest.raises(BootstrapTransactionError) as missing_error:
        _require_runtime_entrypoint(runtime_path, "bin/mcp-broker")

    entrypoint = runtime_path / "bin" / "mcp-broker"
    entrypoint.parent.mkdir()
    entrypoint.write_text("#!/bin/sh\n", encoding="utf-8")
    entrypoint.chmod(0o644)
    with pytest.raises(BootstrapTransactionError) as mode_error:
        _require_runtime_entrypoint(runtime_path, "bin/mcp-broker")

    expected = "runtime entrypoint is not executable: bin/mcp-broker"
    assert str(missing_error.value) == expected
    assert str(mode_error.value) == expected
    assert entrypoint.is_file()
    assert not os.access(entrypoint, os.X_OK)


def test_bootstrap_entrypoint_reports_symlink_escape_boundary(tmp_path: Path) -> None:
    from mcp_broker.bootstrap_transactions import BootstrapTransactionError, _require_runtime_entrypoint

    runtime_path = tmp_path / "runtime"
    outside = tmp_path / "outside"
    runtime_path.mkdir()
    outside.write_text("#!/bin/sh\n", encoding="utf-8")
    outside.chmod(0o755)
    (runtime_path / "escape").symlink_to(outside)

    with pytest.raises(BootstrapTransactionError) as raised:
        _require_runtime_entrypoint(runtime_path, "escape")

    assert str(raised.value) == "runtime entrypoint must stay inside runtime path"


def test_bootstrap_pointer_field_errors_include_the_source_path(tmp_path: Path) -> None:
    from mcp_broker.bootstrap_transactions import (
        BootstrapTransactionError,
        _read_latest_pointer,
        _read_pointer,
        _write_json_atomic,
    )

    pointer_path = tmp_path / "active-runtime.json"
    latest_path = tmp_path / "latest.json"
    _write_json_atomic(pointer_path, {"runtime_id": "runtime-001"})
    _write_json_atomic(latest_path, {"record_path": "record.json"})

    with pytest.raises(BootstrapTransactionError) as pointer_error:
        _read_pointer(pointer_path)
    with pytest.raises(BootstrapTransactionError) as latest_error:
        _read_latest_pointer(latest_path, records_dir=tmp_path / "records")

    assert str(pointer_error.value) == (
        f"runtime bootstrap JSON missing manifest_path: {pointer_path}"
    )
    assert str(latest_error.value) == (
        f"runtime bootstrap JSON missing transaction_id: {latest_path}"
    )
    assert pointer_path.is_file()


def test_bootstrap_pointer_second_field_errors_keep_the_source_path(tmp_path: Path) -> None:
    from mcp_broker.bootstrap_transactions import (
        BootstrapTransactionError,
        _read_latest_pointer,
        _read_pointer,
        _write_json_atomic,
    )

    pointer_path = tmp_path / "active-runtime.json"
    latest_path = tmp_path / "latest.json"
    _write_json_atomic(pointer_path, {"manifest_path": "manifest.json"})
    _write_json_atomic(latest_path, {"transaction_id": "0123456789abcdef"})

    with pytest.raises(BootstrapTransactionError) as pointer_error:
        _read_pointer(pointer_path)
    with pytest.raises(BootstrapTransactionError) as latest_error:
        _read_latest_pointer(latest_path, records_dir=tmp_path / "records")

    assert str(pointer_error.value) == f"runtime bootstrap JSON missing runtime_id: {pointer_path}"
    assert str(latest_error.value) == f"runtime bootstrap JSON missing record_path: {latest_path}"


def test_bootstrap_prepare_empty_dir_reports_exact_boundary_error(tmp_path: Path) -> None:
    from mcp_broker.bootstrap_transactions import BootstrapTransactionError, _prepare_empty_dir

    with pytest.raises(BootstrapTransactionError) as raised:
        _prepare_empty_dir(tmp_path / "outside", tmp_path / "root")

    assert str(raised.value) == "extracted runtime path is outside runtime install root"


@pytest.mark.parametrize(
    ("path", "expected"),
    [("C:", True), ("z:", True), ("C", False), ("", False)],
)
def test_bootstrap_windows_drive_path_boundary(path: str, expected: bool) -> None:
    from mcp_broker.bootstrap_transactions import _is_windows_drive_path

    assert _is_windows_drive_path(path) is expected


def test_bootstrap_latest_pointer_rejects_invalid_id_when_other_fields_match(
    tmp_path: Path,
) -> None:
    from mcp_broker.bootstrap_transactions import (
        BootstrapTransactionError,
        _read_latest_pointer,
        _write_json_atomic,
    )

    records_dir = tmp_path / "records"
    transaction_id = "X123456789abcdef"
    record_path = records_dir / f"{transaction_id}.json"
    latest_path = tmp_path / "latest.json"
    _write_json_atomic(
        latest_path,
        {"record_path": str(record_path), "transaction_id": transaction_id},
    )

    with pytest.raises(BootstrapTransactionError) as raised:
        _read_latest_pointer(latest_path, records_dir=records_dir)

    assert str(raised.value) == "latest transaction pointer is invalid"
    assert record_path.parent == records_dir
    assert record_path.name == f"{transaction_id}.json"


def test_bootstrap_transaction_record_errors_include_context(tmp_path: Path) -> None:
    from mcp_broker.bootstrap_transactions import (
        BootstrapTransactionError,
        _require_matching_transaction_record,
    )

    record_path = tmp_path / "0123456789abcdef.json"
    pointer = {
        "record_path": str(record_path),
        "transaction_id": "0123456789abcdef",
    }

    with pytest.raises(BootstrapTransactionError) as missing_error:
        _require_matching_transaction_record({}, pointer)
    with pytest.raises(BootstrapTransactionError) as mismatch_error:
        _require_matching_transaction_record({"transaction_id": "fedcba9876543210"}, pointer)

    assert str(missing_error.value) == (
        f"runtime bootstrap JSON missing transaction_id: {record_path}"
    )
    assert str(mismatch_error.value) == "latest transaction record does not match pointer"
    assert pointer["transaction_id"] in record_path.name


def test_bootstrap_atomic_json_replaces_the_declared_temp_file(tmp_path: Path) -> None:
    from mcp_broker.bootstrap_transactions import _write_json_atomic

    target = tmp_path / "nested" / "state.json"
    stale_temp = target.with_suffix(".json.tmp")
    stale_temp.parent.mkdir(parents=True)
    stale_temp.write_text("stale", encoding="utf-8")

    _write_json_atomic(target, {"z": 2, "a": 1})

    assert target.read_text(encoding="utf-8") == '{\n  "a": 1,\n  "z": 2\n}\n'
    assert not stale_temp.exists()
    assert list(target.parent.iterdir()) == [target]


@pytest.mark.error_simulation
def test_bootstrap_run_smoke_forwards_timeout_to_the_default_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker.bootstrap_transactions import _run_smoke

    executable = tmp_path / "runtime" / "bin" / "mcp-broker"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    calls: list[dict[str, object]] = []

    def run_spy(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(args=args, returncode=0)

    monkeypatch.setattr(subprocess, "run", run_spy)
    plan = {"runtime_path": str(executable.parents[1]), "entrypoint": "bin/mcp-broker"}

    assert _run_smoke(plan, smoke=None, timeout_seconds=4.25) is True
    assert calls[0]["timeout"] == 4.25
    assert calls[0]["args"] == [str(executable), "--help"]


def test_bootstrap_zip_extraction_continues_after_duplicate_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_broker.bootstrap_transactions as bootstrap_transactions

    class ZipInfoStub:
        def __init__(self, filename: str, *, directory: bool, mode: int = 0) -> None:
            self.filename = filename
            self.external_attr = mode << 16
            self._directory = directory

        def is_dir(self) -> bool:
            return self._directory

    directory = ZipInfoStub("bin/", directory=True)
    file_info = ZipInfoStub("bin/mcp-broker", directory=False, mode=0o751)

    class ZipArchiveStub:
        def __enter__(self) -> ZipArchiveStub:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def infolist(self) -> list[ZipInfoStub]:
            return [directory, directory, file_info]

        def open(self, _info: ZipInfoStub) -> io.BytesIO:
            return io.BytesIO(b"#!/bin/sh\nexit 0\n")

    monkeypatch.setattr(
        bootstrap_transactions.zipfile,
        "ZipFile",
        lambda _path: ZipArchiveStub(),
    )
    destination = tmp_path / "destination"

    bootstrap_transactions._extract_zip_archive(tmp_path / "runtime.zip", destination)

    extracted = destination / "bin" / "mcp-broker"
    assert extracted.read_bytes() == b"#!/bin/sh\nexit 0\n"
    assert stat.S_IMODE(extracted.stat().st_mode) == 0o751
    assert (destination / "bin").is_dir()


def test_bootstrap_tar_extraction_continues_after_duplicate_directories(
    tmp_path: Path,
) -> None:
    from mcp_broker.bootstrap_transactions import _extract_tar_archive

    artifact = tmp_path / "runtime.tar"
    payload = b"runtime"
    with tarfile.open(artifact, "w") as archive:
        for _index in range(2):
            directory = tarfile.TarInfo("bin")
            directory.type = tarfile.DIRTYPE
            directory.mode = 0o751
            archive.addfile(directory)
        file_info = tarfile.TarInfo("bin/mcp-broker")
        file_info.size = len(payload)
        file_info.mode = 0o640
        archive.addfile(file_info, io.BytesIO(payload))

    destination = tmp_path / "destination"
    _extract_tar_archive(artifact, destination)

    extracted = destination / "bin" / "mcp-broker"
    assert extracted.read_bytes() == payload
    assert stat.S_IMODE(extracted.stat().st_mode) == 0o640
    assert stat.S_IMODE((destination / "bin").stat().st_mode) == 0o751


@pytest.mark.parametrize("member_type", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE])
def test_bootstrap_tar_extraction_rejects_every_non_file_member(
    tmp_path: Path,
    member_type: bytes,
) -> None:
    from mcp_broker.bootstrap_transactions import BootstrapTransactionError, _extract_tar_archive

    artifact = tmp_path / "unsafe.tar"
    with tarfile.open(artifact, "w") as archive:
        member = tarfile.TarInfo("bin/unsafe")
        member.type = member_type
        member.linkname = "target"
        archive.addfile(member)

    destination = tmp_path / "destination"
    with pytest.raises(BootstrapTransactionError) as raised:
        _extract_tar_archive(artifact, destination)

    assert str(raised.value) == "unsafe archive member: bin/unsafe"
    assert not (destination / "bin" / "unsafe").exists()
    assert artifact.is_file()


@pytest.mark.parametrize(
    "member_name",
    [
        "   ",
        r"folder\file",
        "C:relative/file",
        "/absolute/file",
        "../escape",
        "safe/../escape",
        "safe/..//escape",
    ],
)
def test_bootstrap_safe_archive_member_rejects_each_unsafe_path_class(
    member_name: str,
) -> None:
    from mcp_broker.bootstrap_transactions import BootstrapTransactionError, _safe_archive_member

    with pytest.raises(BootstrapTransactionError) as raised:
        _safe_archive_member(member_name)

    assert str(raised.value) == f"unsafe archive member: {member_name}"
    assert raised.type is BootstrapTransactionError
    assert raised.value.args == (f"unsafe archive member: {member_name}",)


def test_bootstrap_remove_extracted_runtime_tolerates_missing_candidate(tmp_path: Path) -> None:
    from mcp_broker.bootstrap_transactions import _remove_extracted_runtime

    root = tmp_path / "root"
    root.mkdir()
    candidate = root / "missing"

    _remove_extracted_runtime(candidate, root)

    assert root.is_dir()
    assert not candidate.exists()
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("transaction_id", ["X123456789abcdef", "0123456789abcdeX"])
def test_bootstrap_transaction_id_rejects_nonhex_padding(transaction_id: str) -> None:
    from mcp_broker.bootstrap_transactions import _valid_transaction_id

    result = _valid_transaction_id(transaction_id)

    assert result is False
    assert len(transaction_id) == 16
    assert "X" in transaction_id


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (" C:/runtime", False),
        ("C:/runtime/bin", True),
        ("runtime/C:/bin", False),
    ],
)
def test_bootstrap_windows_drive_detection_uses_the_path_prefix(
    path: str,
    expected: bool,
) -> None:
    from mcp_broker.bootstrap_transactions import _is_windows_drive_path

    result = _is_windows_drive_path(path)

    assert result is expected
    assert isinstance(result, bool)
    assert bool(path) is True
