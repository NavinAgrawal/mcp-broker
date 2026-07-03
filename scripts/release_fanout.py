#!/usr/bin/env python3
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import shlex
import subprocess
import time
from typing import Callable, Sequence


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReleaseFanoutStep:
    name: str
    command: tuple[str, ...]


Runner = Callable[[list[str]], int]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _run_step(step: ReleaseFanoutStep, runner: Runner) -> dict[str, object]:
    started_at = _utc_now()
    started = time.monotonic()
    exit_code = runner(list(step.command))
    elapsed_seconds = round(time.monotonic() - started, 3)
    return {
        "name": step.name,
        "command": list(step.command),
        "status": "passed" if exit_code == 0 else "failed",
        "exit_code": exit_code,
        "started_at": started_at,
        "ended_at": _utc_now(),
        "elapsed_seconds": elapsed_seconds,
    }


def _default_runner(command: list[str]) -> int:
    return subprocess.run(command, check=False).returncode


def run_release_fanout(
    steps: Sequence[ReleaseFanoutStep],
    *,
    ledger_path: Path,
    runner: Runner = _default_runner,
    jobs: int = 1,
) -> int:
    if not steps:
        raise ValueError("at least one release fanout step is required")
    if jobs < 1:
        raise ValueError("jobs must be at least 1")

    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    started_at = _utc_now()
    ordered_results: list[dict[str, object] | None] = [None] * len(steps)

    with ThreadPoolExecutor(max_workers=min(jobs, len(steps))) as executor:
        future_to_index = {
            executor.submit(_run_step, step, runner): index
            for index, step in enumerate(steps)
        }
        for future in as_completed(future_to_index):
            ordered_results[future_to_index[future]] = future.result()

    results = [result for result in ordered_results if result is not None]
    failed = sum(1 for result in results if result["status"] == "failed")
    ledger = {
        "started_at": started_at,
        "ended_at": _utc_now(),
        "summary": {
            "total": len(results),
            "passed": len(results) - failed,
            "failed": failed,
        },
        "steps": results,
    }
    ledger_path.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if failed:
        LOGGER.warning(
            "release_fanout_child_failures ledger=%s failed=%s total=%s",
            ledger_path,
            failed,
            len(results),
        )
    else:
        LOGGER.info("release_fanout_children_passed ledger=%s total=%s", ledger_path, len(results))
    return 0


def _parse_step(raw_step: str) -> ReleaseFanoutStep:
    if "::" not in raw_step:
        raise argparse.ArgumentTypeError("step must use name::command format")
    name, raw_command = raw_step.split("::", maxsplit=1)
    command = tuple(shlex.split(raw_command))
    if not name:
        raise argparse.ArgumentTypeError("step name is required")
    if not command:
        raise argparse.ArgumentTypeError("step command is required")
    return ReleaseFanoutStep(name=name, command=command)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run release registry fanout and write a ledger.")
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--step", action="append", type=_parse_step, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args(argv)
    return run_release_fanout(args.step, ledger_path=args.ledger, jobs=args.jobs)


if __name__ == "__main__":
    raise SystemExit(main())
