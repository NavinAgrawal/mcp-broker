"""Resolve mutation paths from the repo's mutmut configuration and git diff."""

from __future__ import annotations

import argparse
import configparser
import subprocess
import sys
from pathlib import Path


DEFAULT_CARVEOUT_REGISTRY = Path("docs/mutation-carveouts.md")
REGISTRY_TEXT_ENCODING = "utf-8"


def _split_config_paths(value: str) -> list[Path]:
    return [Path(line.strip()) for line in value.splitlines() if line.strip()]


def load_mutation_roots(config_path: Path) -> list[Path]:
    parser = configparser.ConfigParser()
    parser.optionxform = str
    parser.read(config_path)
    if not parser.has_option("mutmut", "paths_to_mutate"):
        raise ValueError(f"{config_path} has no mutmut paths_to_mutate")
    return _split_config_paths(parser.get("mutmut", "paths_to_mutate"))


def load_whole_file_carveouts(registry_path: Path) -> set[Path]:
    rows = registry_path.read_bytes().decode(REGISTRY_TEXT_ENCODING).splitlines()
    carveouts: set[Path] = set()
    for row in rows:
        table_row = row.strip().removeprefix("|")
        cells = [cell.strip() for cell in table_row.split("|")]
        if len(cells) < 2 or not cells[1].lower().startswith("whole file"):
            continue
        file_cell = cells[0]
        if file_cell.startswith("`") and file_cell.endswith("`"):
            carveouts.add(Path(file_cell[1:-1]))
    return carveouts


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def select_changed_mutation_paths(
    repo_root: Path,
    changed_paths: list[Path],
    *,
    mutation_roots: list[Path],
    whole_file_carveouts: set[Path] | None = None,
) -> list[Path]:
    selected: list[Path] = []
    seen: set[Path] = set()
    carveouts = whole_file_carveouts or set()
    for path in changed_paths:
        normalized = Path(path.as_posix())
        absolute = repo_root / normalized
        if normalized.suffix != ".py" or not absolute.is_file():
            continue
        if normalized in carveouts:
            continue
        if not any(_is_relative_to(normalized, root) for root in mutation_roots):
            continue
        if normalized not in seen:
            selected.append(normalized)
            seen.add(normalized)
    return selected


def select_all_mutation_paths(
    repo_root: Path,
    *,
    mutation_roots: list[Path],
    whole_file_carveouts: set[Path] | None = None,
) -> list[Path]:
    candidates: list[Path] = []
    for root in mutation_roots:
        absolute_root = repo_root / root
        if absolute_root.is_file():
            candidates.append(root)
            continue
        candidates.extend(
            path.relative_to(repo_root) for path in absolute_root.rglob("*.py")
        )
    return select_changed_mutation_paths(
        repo_root,
        sorted(candidates, key=Path.as_posix),
        mutation_roots=mutation_roots,
        whole_file_carveouts=whole_file_carveouts,
    )


def changed_paths_from_git(repo_root: Path, diff_base: str) -> list[Path]:
    result = subprocess.run(
        [
            "git",
            "diff",
            "--name-only",
            "--diff-filter=ACMRT",
            diff_base,
            "--",
        ],
        cwd=repo_root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git diff failed for {diff_base}: {result.stderr.strip()}")
    return [Path(line.strip()) for line in result.stdout.splitlines() if line.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--diff-base", default="origin/main")
    parser.add_argument("--all", action="store_true", dest="select_all")
    parser.add_argument("--format", choices=["lines", "make"], default="lines")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = args.root.resolve()
    roots = load_mutation_roots(repo_root / "setup.cfg")
    whole_file_carveouts = load_whole_file_carveouts(
        repo_root / DEFAULT_CARVEOUT_REGISTRY
    )
    if args.select_all:
        selected = select_all_mutation_paths(
            repo_root,
            mutation_roots=roots,
            whole_file_carveouts=whole_file_carveouts,
        )
    else:
        changed_paths = changed_paths_from_git(repo_root, args.diff_base)
        selected = select_changed_mutation_paths(
            repo_root,
            changed_paths,
            mutation_roots=roots,
            whole_file_carveouts=whole_file_carveouts,
        )
    separator = " " if args.format == "make" else "\n"
    sys.stdout.write(separator.join(path.as_posix() for path in selected))
    if selected:
        sys.stdout.write("\n")
    return 0
