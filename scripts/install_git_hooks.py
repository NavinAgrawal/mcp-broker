"""Install checkout-relative hooks without replacing another hook owner."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys


def install(root: Path, hooks_dir: str) -> None:
    hook = root / hooks_dir / "pre-commit"
    if not hook.is_file() or not hook.stat().st_mode & 0o111:
        raise ValueError("Tracked pre-commit hook is missing or not executable")
    current = subprocess.run(
        ["git", "-C", str(root), "config", "--get", "core.hooksPath"],
        text=True, capture_output=True, check=False,
    )
    if current.returncode not in (0, 1):
        raise ValueError("Cannot inspect existing hook configuration")
    if current.returncode == 0 and current.stdout.strip() != hooks_dir:
        raise ValueError("Refusing to replace existing core.hooksPath")
    if current.returncode == 1:
        active = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-path", "hooks"],
            text=True, capture_output=True, check=True,
        )
        active_dir = root / active.stdout.strip()
        if active_dir.is_dir() and any(
            path.is_file() and not path.name.endswith(".sample")
            for path in active_dir.iterdir()
        ):
            raise ValueError("Refusing to replace existing Git hooks")
    for key in ("core.worktree", "core.bare"):
        value = subprocess.run(
            ["git", "-C", str(root), "config", "--local", "--get", key],
            text=True, capture_output=True, check=False,
        )
        if value.returncode not in (0, 1):
            raise ValueError("Cannot inspect common worktree configuration")
        if value.returncode == 0 and (key == "core.worktree" or value.stdout.strip() != "false"):
            raise ValueError(f"Refusing worktree config migration with existing {key}")
    subprocess.run(
        ["git", "-C", str(root), "config", "--local", "extensions.worktreeConfig", "true"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "--worktree", "core.hooksPath", hooks_dir],
        check=True, capture_output=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--hooks-dir", required=True)
    args = parser.parse_args()
    try:
        install(args.root.resolve(), args.hooks_dir)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        sys.stderr.write(f"Hook installation failed: {exc}\n")
        return 2
    sys.stdout.write("Tracked Git hooks installed.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
