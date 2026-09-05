#!/usr/bin/env python3
"""Plan or apply a safe migration to the canonical broker runtime config."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import sys
from typing import Sequence


@dataclass(frozen=True)
class MigrationPlan:
    source: Path
    destination: Path
    status: str
    backup_path: Path | None

    @property
    def can_apply(self) -> bool:
        return self.status == "ready"

    def as_dict(self) -> dict[str, object]:
        return {
            "source": str(self.source),
            "destination": str(self.destination),
            "status": self.status,
            "backup_path": str(self.backup_path) if self.backup_path is not None else None,
            "can_apply": self.can_apply,
        }


def plan_migration(*, source: Path, destination: Path) -> MigrationPlan:
    source_path = source.expanduser().resolve()
    destination_path = destination.expanduser().resolve()
    if not source_path.is_file():
        return MigrationPlan(source_path, destination_path, "missing_source", None)
    if not destination_path.exists():
        return MigrationPlan(source_path, destination_path, "ready", None)
    if not destination_path.is_file():
        return MigrationPlan(source_path, destination_path, "destination_not_file", None)
    if source_path.read_bytes() == destination_path.read_bytes():
        return MigrationPlan(source_path, destination_path, "already_current", None)
    return MigrationPlan(source_path, destination_path, "divergent_destination", None)


def apply_migration(plan: MigrationPlan) -> MigrationPlan:
    if not plan.can_apply:
        raise ValueError(f"migration cannot apply: {plan.status}")
    plan.destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(plan.source, plan.destination)
    return MigrationPlan(plan.source, plan.destination, "applied", None)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    plan = plan_migration(source=args.source, destination=args.destination)
    if args.apply:
        try:
            plan = apply_migration(plan)
        except ValueError:
            sys.stdout.write(json.dumps(plan.as_dict(), sort_keys=True) + "\n")
            return 1
    sys.stdout.write(json.dumps(plan.as_dict(), sort_keys=True) + "\n")
    return 0 if plan.status in {"ready", "already_current", "applied"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
