#!/usr/bin/env python3
"""Select pytest files affected by changed mcp-broker files."""

from __future__ import annotations

import argparse
from fnmatch import fnmatchcase
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Sequence


def _git_lines(root: Path, args: list[str]) -> list[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git change selection failed")
    return [line for line in result.stdout.splitlines() if line]


def changed_files(root: Path, tier: str, base: str) -> list[str]:
    override = os.environ.get("CITS_CHANGED_FILES")
    if override is not None:
        return sorted({line.strip() for line in override.splitlines() if line.strip()})
    if tier == "commit":
        committed = []
    else:
        committed = _git_lines(root, ["diff", "--name-only", "--diff-filter=ACMRTD", f"{base}...HEAD"])
    working = _git_lines(root, ["diff", "--name-only", "--diff-filter=ACMRTD", "HEAD"])
    untracked = _git_lines(root, ["ls-files", "--others", "--exclude-standard"])
    return sorted(set(committed + working + untracked))


def _test_files(root: Path) -> list[tuple[Path, str]]:
    tests_root = root / "tests"
    return [
        (path, path.read_text(encoding="utf-8"))
        for path in sorted(tests_root.rglob("test_*.py"))
        if path.is_file()
    ]


def _python_dependency_tests(root: Path, changed: str, tests: list[tuple[Path, str]]) -> set[str]:
    path = Path(changed)
    if path.suffix != ".py":
        return set()
    if path.parts[:2] == ("src", "mcp_broker"):
        module_parts = path.with_suffix("").parts[1:]
    elif path.parts[:2] == ("tests", "support"):
        module_parts = path.with_suffix("").parts
    else:
        return set()
    module = ".".join(module_parts)
    stem = path.stem
    selected: set[str] = set()
    for test, text in tests:
        relative = test.relative_to(root).as_posix()
        if stem in test.stem:
            selected.add(relative)
            continue
        if module in text or f"from mcp_broker import {stem}" in text:
            selected.add(relative)
    return selected


def _declared_tests(root: Path, changed: str) -> set[str]:
    config_path = root / ".test-impact.json"
    if not config_path.is_file():
        return set()
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    selected: set[str] = set()
    for row in payload.get("map", []):
        pattern = row.get("changed")
        if not isinstance(pattern, str) or not fnmatchcase(changed, pattern):
            continue
        for test_pattern in row.get("runTests", []):
            selected.update(
                path.relative_to(root).as_posix()
                for path in root.glob(test_pattern)
                if path.is_file()
            )
    return selected


def select_affected_tests(root: Path, changed: list[str]) -> tuple[list[str], list[str]]:
    tests = _test_files(root)
    selected: set[str] = set()
    unmapped: list[str] = []
    for relative in changed:
        path = root / relative
        mapped: set[str] = set()
        if (
            relative.startswith("tests/")
            and path.is_file()
            and path.suffix == ".py"
            and path.name.startswith("test_")
        ):
            mapped.add(relative)
        mapped.update(_python_dependency_tests(root, relative, tests))
        mapped.update(_declared_tests(root, relative))
        if not mapped:
            unmapped.append(relative)
        selected.update(mapped)
    return sorted(selected), unmapped


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--tier", choices=["commit", "push"], required=True)
    parser.add_argument("--base", default="origin/main")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.resolve()
    try:
        changed = changed_files(root, args.tier, args.base)
        if not changed:
            sys.stderr.write("no changed files in CITS scope\n")
            return 2
        selected, unmapped = select_affected_tests(root, changed)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"affected test selection failed: {exc}\n")
        return 2
    if unmapped:
        for path in unmapped:
            sys.stderr.write(f"no affected tests: {path}\n")
        return 2
    if not selected:
        sys.stderr.write("affected test selection returned zero tests\n")
        return 2
    sys.stdout.write("\n".join(selected) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
