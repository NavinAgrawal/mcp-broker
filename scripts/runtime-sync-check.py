#!/usr/bin/env python3
"""Verify that a LaunchAgent uses the broker's canonical runtime config."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import plistlib
import sys
from typing import Sequence


@dataclass(frozen=True)
class RuntimeSyncReport:
    expected_config_path: Path
    observed_config_path: Path | None
    expected_runtime_root: Path
    observed_runtime_root: Path | None
    expected_working_directory: Path
    observed_working_directory: Path | None
    findings: list[str]

    @property
    def is_clean(self) -> bool:
        return not self.findings

    def as_dict(self) -> dict[str, object]:
        return {
            "expected_config_path": str(self.expected_config_path),
            "observed_config_path": _path_text(self.observed_config_path),
            "expected_runtime_root": str(self.expected_runtime_root),
            "observed_runtime_root": _path_text(self.observed_runtime_root),
            "expected_working_directory": str(self.expected_working_directory),
            "observed_working_directory": _path_text(self.observed_working_directory),
            "findings": self.findings,
            "is_clean": self.is_clean,
        }


def check_runtime_sync(
    *,
    plist_path: Path,
    runtime_root: Path,
    config_path: Path,
    working_directory: Path,
) -> RuntimeSyncReport:
    document = _load_plist(plist_path)
    arguments = document.get("ProgramArguments")
    if not isinstance(arguments, list) or not all(isinstance(item, str) for item in arguments):
        raise ValueError("LaunchAgent ProgramArguments must be a list of strings")

    expected_runtime_root = runtime_root.expanduser().resolve()
    expected_config_path = config_path.expanduser().resolve()
    expected_working_directory = working_directory.expanduser().resolve()
    observed_runtime_root = _argument_value(arguments, "--runtime-root")
    observed_config_path = _argument_value(arguments, "--config")
    observed_working_directory = document.get("WorkingDirectory")
    findings = _findings(
        expected_runtime_root=expected_runtime_root,
        observed_runtime_root=observed_runtime_root,
        expected_config_path=expected_config_path,
        observed_config_path=observed_config_path,
        expected_working_directory=expected_working_directory,
        observed_working_directory=observed_working_directory,
    )
    return RuntimeSyncReport(
        expected_config_path=expected_config_path,
        observed_config_path=_resolved_path(observed_config_path),
        expected_runtime_root=expected_runtime_root,
        observed_runtime_root=_resolved_path(observed_runtime_root),
        expected_working_directory=expected_working_directory,
        observed_working_directory=_resolved_path(observed_working_directory),
        findings=findings,
    )


def _load_plist(plist_path: Path) -> dict[str, object]:
    with plist_path.expanduser().open("rb") as file_handle:
        document = plistlib.load(file_handle)
    if not isinstance(document, dict):
        raise ValueError("LaunchAgent plist must contain a dictionary")
    return document


def _argument_value(arguments: list[str], flag: str) -> str | None:
    try:
        index = arguments.index(flag)
    except ValueError:
        return None
    if index + 1 >= len(arguments):
        return None
    return arguments[index + 1]


def _findings(
    *,
    expected_runtime_root: Path,
    observed_runtime_root: str | None,
    expected_config_path: Path,
    observed_config_path: str | None,
    expected_working_directory: Path,
    observed_working_directory: object,
) -> list[str]:
    findings: list[str] = []
    if _resolved_path(observed_runtime_root) != expected_runtime_root:
        findings.append("runtime_root_mismatch")
    if _resolved_path(observed_config_path) != expected_config_path:
        findings.append("config_path_mismatch")
    if not isinstance(observed_working_directory, str) or _resolved_path(observed_working_directory) != expected_working_directory:
        findings.append("working_directory_mismatch")
    return findings


def _resolved_path(value: object) -> Path | None:
    if not isinstance(value, str):
        return None
    return Path(value).expanduser().resolve()


def _path_text(value: Path | None) -> str | None:
    return str(value) if value is not None else None


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plist", required=True, type=Path)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--working-directory", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        report = check_runtime_sync(
            plist_path=args.plist,
            runtime_root=args.runtime_root,
            config_path=args.config,
            working_directory=args.working_directory,
        )
    except (OSError, ValueError, plistlib.InvalidFileException) as exc:
        sys.stderr.write(f"runtime-sync-check failed: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(report.as_dict(), sort_keys=True) + "\n")
    return 0 if report.is_clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
