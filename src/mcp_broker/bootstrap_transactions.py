"""Approval-gated bootstrap transactions for plugin-managed runtimes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import posixpath
import shutil
import subprocess
import sys
import tarfile
from typing import Any, Sequence
import uuid
import zipfile

from mcp_broker.runtime_artifact import RuntimeArtifactError, RuntimeArtifactVerifier
from mcp_broker.runtime_install import RuntimeInstallError, RuntimeInstallStore


ACTIVE_RUNTIME_POINTER = "active-runtime.json"
ATOMIC_JSON_TEMP_SUFFIX = ".tmp"
BOOTSTRAP_SMOKE_ARGS = ("--help",)
BOOTSTRAP_SMOKE_TIMEOUT_SECONDS = 10
EXTRACTED_RUNTIMES_DIR = "extracted-runtimes"
JSON_INDENT = 2
PREVIOUS_RUNTIME_POINTER = "previous-runtime.json"
TEXT_ENCODING = "utf-8"


class BootstrapTransactionError(ValueError):
    """Raised when a bootstrap transaction is invalid or denied."""


SmokeCheck = Callable[[dict[str, object]], bool]


@dataclass(frozen=True)
class BootstrapTransactionStore:
    state_dir: Path

    @property
    def bootstrap_dir(self) -> Path:
        return self.state_dir.expanduser() / "bootstrap-transactions"

    @property
    def records_dir(self) -> Path:
        return self.bootstrap_dir / "records"

    @property
    def latest_pointer(self) -> Path:
        return self.bootstrap_dir / "latest.json"

    @property
    def journal_path(self) -> Path:
        return self.bootstrap_dir / "bootstrap-journal.jsonl"

    @property
    def runtime_install_dir(self) -> Path:
        return RuntimeInstallStore(self.state_dir).install_dir

    def preflight(self, metadata_path: Path) -> dict[str, object]:
        plan = self._plan(metadata_path)
        return {**plan, "status": "preflight_passed", "would_change_runtime_state": False}

    def plan(self, metadata_path: Path) -> dict[str, object]:
        plan = self._plan(metadata_path)
        return {**plan, "status": "planned", "would_change_runtime_state": True}

    def apply(
        self,
        *,
        metadata_path: Path,
        approved: bool,
        smoke: SmokeCheck | None = None,
        smoke_timeout_seconds: float = BOOTSTRAP_SMOKE_TIMEOUT_SECONDS,
    ) -> dict[str, object]:
        _require_approval(approved, "bootstrap apply requires approval")
        plan = self.plan(metadata_path)
        active_before = _read_pointer(self._active_runtime_pointer())
        extracted_runtime_path = self._extract_verified_runtime_candidate(plan)
        prepared_plan = {
            **plan,
            "extracted_runtime_path": str(extracted_runtime_path),
            "runtime_path": str(extracted_runtime_path),
        }
        if not _run_smoke(
            prepared_plan,
            smoke=smoke,
            timeout_seconds=smoke_timeout_seconds,
        ):
            _remove_extracted_runtime(extracted_runtime_path, self._extracted_runtimes_dir())
            failed = {
                **prepared_plan,
                "active_runtime_id": _pointer_id(active_before),
                "status": "failed",
            }
            self._record_transaction(failed, action="apply_failed")
            return failed

        promoted_runtime_path = self._promote_verified_runtime(
            extracted_runtime_path,
            plan,
        )
        prepared_plan = {
            **prepared_plan,
            "extracted_runtime_path": str(promoted_runtime_path),
            "runtime_path": str(promoted_runtime_path),
        }
        install = RuntimeInstallStore(self.state_dir).record_installed_runtime(
            version=str(prepared_plan["version"]),
            runtime_path=Path(str(prepared_plan["runtime_path"])),
            entrypoint=str(prepared_plan["entrypoint"]),
            artifact_digest=str(prepared_plan["artifact_digest"]),
        )
        applied = {
            **prepared_plan,
            "active_runtime_id": install["runtime_id"],
            "manifest_path": install["manifest_path"],
            "previous_runtime_id": _pointer_id(active_before),
            "status": "applied",
        }
        self._record_transaction(applied, action="apply")
        return applied

    def status(self) -> dict[str, object]:
        latest = _read_latest_pointer(self.latest_pointer, records_dir=self.records_dir)
        latest_transaction = None
        if latest is not None:
            latest_transaction = _read_json(Path(str(latest["record_path"])))
            _require_matching_transaction_record(latest_transaction, latest)
        return {
            "active_runtime_id": _pointer_id(_read_pointer(self._active_runtime_pointer())),
            "latest_transaction": latest_transaction,
            "status": "ok",
        }

    def rollback(self, *, approved: bool) -> dict[str, object]:
        _require_approval(approved, "bootstrap rollback requires approval")
        active_path = self._active_runtime_pointer()
        previous_path = self._previous_runtime_pointer()
        active = _require_pointer(active_path, "active runtime pointer is missing")
        previous = _require_pointer(previous_path, "previous runtime pointer is missing")
        self._validate_runtime_pointer(previous, label="previous runtime")
        _write_json_atomic(active_path, previous)
        _write_json_atomic(previous_path, active)
        result = {
            "active_runtime_id": previous["runtime_id"],
            "previous_runtime_id": active["runtime_id"],
            "status": "rolled_back",
            "transaction_id": _transaction_id({"action": "rollback", **previous}),
        }
        self._record_transaction(result, action="rollback")
        return result

    def uninstall(self, *, approved: bool) -> dict[str, object]:
        _require_approval(approved, "bootstrap uninstall requires approval")
        active_path = self._active_runtime_pointer()
        active = _read_pointer(active_path)
        if active_path.exists():
            active_path.unlink()
        if active is not None:
            _write_json_atomic(self._previous_runtime_pointer(), active)
        result = {
            "previous_runtime_id": _pointer_id(active),
            "status": "uninstalled",
            "transaction_id": _transaction_id({"action": "uninstall", "active": active}),
        }
        self._record_transaction(result, action="uninstall")
        return result

    def _plan(self, metadata_path: Path) -> dict[str, object]:
        metadata_file = metadata_path.expanduser()
        if not metadata_file.is_file():
            raise BootstrapTransactionError(f"runtime artifact metadata not found: {metadata_file}")
        metadata = _load_metadata(metadata_file)
        version = _required_string(metadata, "version", metadata_file)
        artifact_digest = _required_string(metadata, "artifact_digest", metadata_file)
        _required_string(metadata, "artifact_path", metadata_file)
        entrypoint_value = _required_string(metadata, "entrypoint", metadata_file)
        runtime_path_value = _required_string(metadata, "runtime_path", metadata_file)
        runtime_path = _metadata_runtime_path(
            metadata_file.parent,
            runtime_path_value,
        )
        _require_runtime_entrypoint(runtime_path, entrypoint_value)
        try:
            artifact_report = RuntimeArtifactVerifier().verify_metadata_file(metadata_file)
        except RuntimeArtifactError as exc:
            raise BootstrapTransactionError(str(exc)) from exc
        entrypoint = str(artifact_report["entrypoint"])
        _require_runtime_entrypoint(runtime_path, entrypoint)
        plan = {
            "artifact_digest": artifact_digest,
            "artifact_path": artifact_report["artifact_path"],
            "entrypoint": entrypoint,
            "metadata_path": str(metadata_file),
            "runtime_path": str(runtime_path),
            "version": artifact_report.get("version", version),
        }
        return {**plan, "transaction_id": _transaction_id(plan)}

    def _record_transaction(self, payload: dict[str, object], *, action: str) -> None:
        transaction_id = str(payload["transaction_id"])
        record_path = self.records_dir / f"{transaction_id}.json"
        record = {**payload, "updated_at": _utc_now()}
        _write_json_atomic(record_path, record)
        _write_json_atomic(
            self.latest_pointer,
            {"record_path": str(record_path), "transaction_id": transaction_id},
        )
        self._append_journal({"action": action, "transaction_id": transaction_id})

    def _append_journal(self, entry: dict[str, object]) -> None:
        self.bootstrap_dir.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("a", encoding=TEXT_ENCODING) as handle:
            handle.write(
                json.dumps({**entry, "ts": _utc_now()}, sort_keys=True, separators=(",", ":"))
                + "\n"
            )

    def _extract_verified_runtime_candidate(self, plan: dict[str, object]) -> Path:
        artifact_path = Path(str(plan["artifact_path"]))
        destination = self._extracted_runtimes_dir() / (
            f"{plan['transaction_id']}.candidate.{uuid.uuid4().hex}"
        )
        _prepare_empty_dir(destination, self._extracted_runtimes_dir())
        if zipfile.is_zipfile(artifact_path):
            _extract_zip_archive(artifact_path, destination)
        elif tarfile.is_tarfile(artifact_path):
            _extract_tar_archive(artifact_path, destination)
        else:
            raise BootstrapTransactionError(f"unsupported runtime artifact archive: {artifact_path}")
        _require_runtime_entrypoint(destination, str(plan["entrypoint"]))
        return destination

    def _promote_verified_runtime(self, candidate_path: Path, plan: dict[str, object]) -> Path:
        destination = self._extracted_runtimes_dir() / str(plan["transaction_id"])
        _prepare_empty_dir(destination, self._extracted_runtimes_dir())
        candidate_path.replace(destination)
        _require_runtime_entrypoint(destination, str(plan["entrypoint"]))
        return destination

    def _extracted_runtimes_dir(self) -> Path:
        return self.runtime_install_dir / EXTRACTED_RUNTIMES_DIR

    def _validate_runtime_pointer(self, pointer: dict[str, str], *, label: str) -> None:
        store = RuntimeInstallStore(self.state_dir)
        manifest_path = Path(pointer["manifest_path"]).expanduser()
        resolved_manifest = manifest_path.resolve(strict=False)
        versions_root = store.versions_dir.expanduser().resolve(strict=False)
        if (
            not _is_relative_to(resolved_manifest, versions_root)
            or resolved_manifest.name != "runtime-manifest.json"
        ):
            raise BootstrapTransactionError(f"{label} manifest path is outside runtime install root")
        manifest = _read_json(manifest_path)
        runtime_id = _json_string(manifest, "runtime_id", manifest_path)
        if runtime_id != pointer["runtime_id"]:
            raise BootstrapTransactionError(f"{label} pointer does not match manifest")
        runtime_path = Path(_json_string(manifest, "runtime_path", manifest_path)).expanduser()
        try:
            _require_runtime_entrypoint(
                runtime_path,
                _json_string(manifest, "entrypoint", manifest_path),
            )
        except BootstrapTransactionError as exc:
            raise BootstrapTransactionError(f"{label} is invalid: {exc}") from exc

    def _active_runtime_pointer(self) -> Path:
        return self.runtime_install_dir / ACTIVE_RUNTIME_POINTER

    def _previous_runtime_pointer(self) -> Path:
        return self.runtime_install_dir / PREVIOUS_RUNTIME_POINTER


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    store = BootstrapTransactionStore(args.state_dir)
    try:
        if args.bootstrap_command == "preflight":
            report = store.preflight(args.metadata)
        elif args.bootstrap_command == "plan":
            report = store.plan(args.metadata)
        elif args.bootstrap_command == "apply":
            report = store.apply(metadata_path=args.metadata, approved=args.approved)
        elif args.bootstrap_command == "status":
            report = store.status()
        elif args.bootstrap_command == "rollback":
            report = store.rollback(approved=args.approved)
        elif args.bootstrap_command == "uninstall":
            report = store.uninstall(approved=args.approved)
        else:
            raise BootstrapTransactionError(f"unknown bootstrap command: {args.bootstrap_command}")
    except (
        BootstrapTransactionError,
        RuntimeArtifactError,
        RuntimeInstallError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    sys.stdout.write(_serialize_report(report) + "\n")
    return 0


def _serialize_report(report: dict[str, object]) -> str:
    return json.dumps(report, sort_keys=True)


def _parse_args(argv: Sequence[str] | None) -> Any:
    import argparse

    parser = argparse.ArgumentParser(description="Manage plugin bootstrap transactions")
    subparsers = parser.add_subparsers(dest="bootstrap_command", required=True)
    for command in ("preflight", "plan", "apply"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--metadata", required=True, type=Path)
        command_parser.add_argument("--state-dir", required=True, type=Path)
        if command == "apply":
            command_parser.add_argument("--approved", action="store_true")
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--state-dir", required=True, type=Path)
    for command in ("rollback", "uninstall"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--state-dir", required=True, type=Path)
        command_parser.add_argument("--approved", action="store_true")
    return parser.parse_args(argv)


def _load_metadata(path: Path) -> dict[str, Any]:
    with path.open("r", encoding=TEXT_ENCODING) as handle:
        loaded = json.load(handle)
    if not isinstance(loaded, dict):
        raise BootstrapTransactionError(f"runtime metadata must be an object: {path}")
    return loaded


def _required_string(data: dict[str, Any], field: str, path: Path) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise BootstrapTransactionError(f"runtime metadata missing {field}: {path}")
    return value


def _metadata_runtime_path(metadata_dir: Path, runtime_path: str) -> Path:
    candidate = Path(runtime_path)
    resolved = (metadata_dir / candidate).resolve()
    if not _is_relative_to(resolved, metadata_dir.resolve()):
        raise BootstrapTransactionError("runtime path must stay inside metadata directory")
    if not resolved.is_dir():
        raise BootstrapTransactionError(f"runtime path not found: {resolved}")
    return resolved


def _require_runtime_entrypoint(runtime_path: Path, entrypoint: str) -> None:
    entrypoint_path = (runtime_path / entrypoint).resolve()
    if not _is_relative_to(entrypoint_path, runtime_path.resolve()):
        raise BootstrapTransactionError("runtime entrypoint must stay inside runtime path")
    if not entrypoint_path.is_file() or not os.access(entrypoint_path, os.X_OK):
        raise BootstrapTransactionError(f"runtime entrypoint is not executable: {entrypoint}")


def _transaction_id(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(TEXT_ENCODING)
    return hashlib.sha256(encoded).hexdigest()[:16]


def _read_pointer(path: Path) -> dict[str, str] | None:
    if not path.exists():
        return None
    data = _read_json(path)
    return {
        "manifest_path": _json_string(data, "manifest_path", path),
        "runtime_id": _json_string(data, "runtime_id", path),
    }


def _read_latest_pointer(path: Path, *, records_dir: Path) -> dict[str, str] | None:
    if not path.exists():
        return None
    data = _read_json(path)
    transaction_id = _json_string(data, "transaction_id", path)
    record_path = Path(_json_string(data, "record_path", path)).expanduser()
    resolved_record = record_path.resolve()
    resolved_records_dir = records_dir.expanduser().resolve()
    if (
        not _valid_transaction_id(transaction_id)
        or not _is_relative_to(resolved_record, resolved_records_dir)
        or resolved_record.name != f"{transaction_id}.json"
    ):
        raise BootstrapTransactionError("latest transaction pointer is invalid")
    return {
        "record_path": str(record_path),
        "transaction_id": transaction_id,
    }


def _require_matching_transaction_record(
    record: dict[str, Any],
    pointer: dict[str, str],
) -> None:
    if _json_string(record, "transaction_id", Path(pointer["record_path"])) != pointer[
        "transaction_id"
    ]:
        raise BootstrapTransactionError("latest transaction record does not match pointer")


def _require_pointer(path: Path, message: str) -> dict[str, str]:
    pointer = _read_pointer(path)
    if pointer is None:
        raise BootstrapTransactionError(message)
    return pointer


def _pointer_id(pointer: dict[str, str] | None) -> str | None:
    return pointer["runtime_id"] if pointer is not None else None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding=TEXT_ENCODING) as handle:
            loaded = json.load(handle)
    except json.JSONDecodeError as exc:
        raise BootstrapTransactionError(f"invalid runtime bootstrap JSON: {path}") from exc
    if not isinstance(loaded, dict):
        raise BootstrapTransactionError(f"expected JSON object: {path}")
    return loaded


def _json_string(data: dict[str, Any], field: str, path: Path) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise BootstrapTransactionError(f"runtime bootstrap JSON missing {field}: {path}")
    return value


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ATOMIC_JSON_TEMP_SUFFIX)
    tmp_path.write_bytes(
        (json.dumps(payload, indent=JSON_INDENT, sort_keys=True) + "\n").encode(TEXT_ENCODING)
    )
    tmp_path.replace(path)


def _run_smoke(
    plan: dict[str, object],
    *,
    smoke: SmokeCheck | None,
    timeout_seconds: float,
) -> bool:
    if smoke is not None:
        return smoke(plan)
    return _default_smoke(plan, timeout_seconds=timeout_seconds)


def _default_smoke(plan: dict[str, object], *, timeout_seconds: float) -> bool:
    executable = Path(str(plan["runtime_path"])) / str(plan["entrypoint"])
    try:
        completed = subprocess.run(
            [str(executable), *BOOTSTRAP_SMOKE_ARGS],
            cwd=str(Path(str(plan["runtime_path"]))),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _extract_zip_archive(artifact_path: Path, destination: Path) -> None:
    with zipfile.ZipFile(artifact_path) as archive:
        for info in archive.infolist():
            member = _safe_archive_member(info.filename)
            if _zip_info_is_symlink(info):
                raise BootstrapTransactionError(f"unsafe archive member: {info.filename}")
            target = _archive_target(destination, member)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as handle:
                shutil.copyfileobj(source, handle)
            mode = (info.external_attr >> 16) & 0o777
            if mode:
                target.chmod(mode)


def _extract_tar_archive(artifact_path: Path, destination: Path) -> None:
    with tarfile.open(artifact_path) as archive:
        for member in archive.getmembers():
            archive_member = _safe_archive_member(member.name)
            if not member.isfile() and not member.isdir():
                raise BootstrapTransactionError(f"unsafe archive member: {member.name}")
            target = _archive_target(destination, archive_member)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                target.chmod(member.mode & 0o777)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise BootstrapTransactionError(f"runtime archive member unreadable: {member.name}")
            with source, target.open("wb") as handle:
                shutil.copyfileobj(source, handle)
            target.chmod(member.mode & 0o777)


def _safe_archive_member(member_name: str) -> str:
    normalized = posixpath.normpath(member_name)
    raw_parts = tuple(part for part in member_name.split("/") if part)
    if (
        not member_name.strip()
        or "\\" in member_name
        or _is_windows_drive_path(member_name)
        or ".." in raw_parts
        or normalized == "."
        or posixpath.isabs(member_name)
    ):
        raise BootstrapTransactionError(f"unsafe archive member: {member_name}")
    return normalized


def _archive_target(destination: Path, member: str) -> Path:
    target = (destination / member).resolve()
    if not _is_relative_to(target, destination.resolve()):
        raise BootstrapTransactionError(f"unsafe archive member: {member}")
    return target


def _zip_info_is_symlink(info: zipfile.ZipInfo) -> bool:
    file_type = (info.external_attr >> 16) & 0o170000
    return file_type == 0o120000


def _prepare_empty_dir(path: Path, allowed_root: Path) -> None:
    resolved_path = path.resolve()
    resolved_root = allowed_root.resolve()
    if not _is_relative_to(resolved_path, resolved_root):
        raise BootstrapTransactionError("extracted runtime path is outside runtime install root")
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)


def _remove_extracted_runtime(path: Path, allowed_root: Path) -> None:
    resolved_path = path.resolve()
    resolved_root = allowed_root.resolve()
    if _is_relative_to(resolved_path, resolved_root) and path.exists():
        shutil.rmtree(path)


def _valid_transaction_id(transaction_id: str) -> bool:
    return len(transaction_id) == 16 and all(char in "0123456789abcdef" for char in transaction_id)


def _is_windows_drive_path(path: str) -> bool:
    return len(path) >= 2 and path[1] == ":" and path[0].isalpha()


def _require_approval(approved: bool, message: str) -> None:
    if not approved:
        raise BootstrapTransactionError(message)


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return True


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
