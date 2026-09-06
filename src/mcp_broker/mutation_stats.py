"""Validate mutmut metadata as a release gate."""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


STATUS_BY_EXIT_CODE = {
    0: "survived",
    1: "killed",
    2: "check_was_interrupted_by_user",
    3: "killed",
    5: "no_tests",
    24: "timeout",
    33: "no_tests",
    34: "skipped",
    35: "suspicious",
    36: "timeout",
    37: "caught_by_type_check",
    152: "timeout",
    255: "timeout",
    -24: "timeout",
    -11: "segfault",
    -9: "segfault",
}

ALL_STATUSES = [
    "killed",
    "caught_by_type_check",
    "survived",
    "no_tests",
    "skipped",
    "suspicious",
    "timeout",
    "check_was_interrupted_by_user",
    "segfault",
    "not_checked",
]

PASS_STATUSES = {"killed", "caught_by_type_check"}
DEFAULT_FAIL_STATUSES = [status for status in ALL_STATUSES if status not in PASS_STATUSES]
DEFAULT_EXAMPLE_LIMIT = 10


@dataclass(frozen=True)
class MutationReport:
    counts: dict[str, int]
    total: int
    passed: int
    score: float
    blocked_by_file: list[dict[str, Any]]
    missing_selected_mutants: list[str] = field(default_factory=list)


def write_line(message: str) -> None:
    sys.stdout.write(f"{message}\n")


def status_for_exit_code(exit_code: Any) -> str:
    if exit_code is None:
        return "not_checked"
    if isinstance(exit_code, bool):
        return "suspicious"
    if isinstance(exit_code, int):
        return STATUS_BY_EXIT_CODE.get(exit_code, "suspicious")
    return "suspicious"


def mutant_source_path(meta_path: Path, mutants_dir: Path) -> str:
    relative = meta_path.relative_to(mutants_dir)
    path_text = relative.as_posix()
    if path_text.endswith(".meta"):
        path_text = path_text[: -len(".meta")]
    return path_text


def load_mutant_results(mutants_dir: Path) -> list[tuple[str, str, str]]:
    results: list[tuple[str, str, str]] = []
    for meta_path in sorted(mutants_dir.rglob("*.meta")):
        data = json.loads(meta_path.read_bytes())
        if not isinstance(data, dict):
            raise ValueError(f"{meta_path} has no metadata object")
        raw_codes = data.get("exit_code_by_key")
        if not isinstance(raw_codes, dict):
            raise ValueError(f"{meta_path} has no object exit_code_by_key")
        source_path = mutant_source_path(meta_path, mutants_dir)
        for mutant_name, exit_code in sorted(raw_codes.items()):
            results.append((source_path, str(mutant_name), status_for_exit_code(exit_code)))
    return results


def filter_mutant_results(
    results: list[tuple[str, str, str]],
    include_mutants: list[str] | None,
) -> tuple[list[tuple[str, str, str]], list[str]]:
    if include_mutants is None:
        return results, []
    filtered = [
        result
        for result in results
        if any(fnmatch.fnmatchcase(result[1], pattern) for pattern in include_mutants)
    ]
    matched_patterns = {
        pattern
        for pattern in include_mutants
        if any(fnmatch.fnmatchcase(result[1], pattern) for result in results)
    }
    missing = sorted(set(include_mutants) - matched_patterns)
    return filtered, missing


def blocked_file_summary(
    results: list[tuple[str, str, str]],
    *,
    fail_statuses: list[str],
    example_limit: int,
) -> list[dict[str, Any]]:
    if example_limit < 0:
        raise ValueError("example_limit must be greater than or equal to 0")
    validate_fail_statuses(fail_statuses)
    by_file: dict[str, dict[str, Any]] = {}
    fail_set = set(fail_statuses)
    for source_path, mutant_name, status in results:
        entry = by_file.setdefault(
            source_path,
            {
                "path": source_path,
                "counts": Counter(),
                "examples": {},
                "blocked": 0,
                "total": 0,
            },
        )
        entry["total"] += 1
        if status in fail_set:
            entry["blocked"] += 1
            entry["counts"][status] += 1
            examples = entry["examples"].setdefault(status, [])
            if len(examples) < example_limit:
                examples.append(mutant_name)

    blocked = [entry for entry in by_file.values() if entry["blocked"] > 0]
    for entry in blocked:
        entry["counts"] = dict(sorted(entry["counts"].items()))
        entry["examples"] = dict(sorted(entry["examples"].items()))
    return sorted(blocked, key=lambda entry: (-entry["blocked"], entry["path"]))


def validate_fail_statuses(fail_statuses: list[str]) -> None:
    unknown = sorted(set(fail_statuses) - set(ALL_STATUSES))
    if unknown:
        raise ValueError(f"unknown fail status: {', '.join(unknown)}")


def non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be greater than or equal to 0")
    return parsed


def build_report(
    mutants_dir: Path,
    *,
    fail_statuses: list[str] | None = None,
    example_limit: int | None = None,
    include_mutants: list[str] | None = None,
) -> MutationReport:
    if not mutants_dir.is_dir():
        raise FileNotFoundError(f"Mutants directory does not exist: {mutants_dir}")

    fail_statuses = [] if fail_statuses is None else fail_statuses
    example_limit = DEFAULT_EXAMPLE_LIMIT if example_limit is None else example_limit
    validate_fail_statuses(fail_statuses)
    counts = {status: 0 for status in ALL_STATUSES}
    results, missing_selected_mutants = filter_mutant_results(
        load_mutant_results(mutants_dir),
        include_mutants,
    )
    for _source_path, _mutant_name, status in results:
        counts[status] += 1

    total = sum(counts.values())
    passed = counts["killed"] + counts["caught_by_type_check"]
    score = 0.0 if total == 0 else passed / total * 100.0
    blocked = blocked_file_summary(
        results,
        fail_statuses=fail_statuses,
        example_limit=example_limit,
    )
    return MutationReport(
        counts=counts,
        total=total,
        passed=passed,
        score=score,
        blocked_by_file=blocked,
        missing_selected_mutants=missing_selected_mutants,
    )


def write_report(report: MutationReport, output_json: Path) -> None:
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_bytes(
        (
            json.dumps(
                {
                    "counts": report.counts,
                    "total": report.total,
                    "passed": report.passed,
                    "score": report.score,
                    "blocked_by_file": report.blocked_by_file,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode()
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mutants-dir", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--min-score", type=float, default=100.0)
    parser.add_argument("--example-limit", type=non_negative_int, default=10)
    parser.add_argument(
        "--include-mutants",
        nargs="+",
        help="Optional mutant names to grade; unselected mutants are ignored.",
    )
    parser.add_argument(
        "--fail-statuses",
        nargs="+",
        choices=ALL_STATUSES,
        default=DEFAULT_FAIL_STATUSES,
    )
    return parser


def failure_exit_code(report: MutationReport, args: argparse.Namespace) -> int | None:
    blocked_counts = {
        status: report.counts[status]
        for status in args.fail_statuses
        if report.counts[status] > 0
    }
    if report.total == 0:
        if report.missing_selected_mutants:
            write_line(
                "Mutation gate failed: selected mutants not found: "
                + ", ".join(report.missing_selected_mutants)
                + f". Report: {args.output_json}"
            )
            return 1
        write_line(f"Mutation gate failed: no mutants found. Report: {args.output_json}")
        return 1
    if report.missing_selected_mutants:
        write_line(
            "Mutation gate failed: selected mutants not found: "
            + ", ".join(report.missing_selected_mutants)
            + f". Report: {args.output_json}"
        )
        return 1
    if blocked_counts:
        write_line(
            "Mutation gate failed: "
            + ", ".join(f"{status}={count}" for status, count in sorted(blocked_counts.items()))
            + f". Report: {args.output_json}"
        )
        return 1
    if report.score < args.min_score:
        write_line(
            f"Mutation gate failed: score={report.score:.2f}, "
            f"min_score={args.min_score:.2f}. Report: {args.output_json}"
        )
        return 1
    return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = build_report(
        args.mutants_dir,
        fail_statuses=args.fail_statuses,
        example_limit=args.example_limit,
        include_mutants=args.include_mutants,
    )
    write_report(report, args.output_json)

    if exit_code := failure_exit_code(report, args):
        return exit_code

    write_line(
        f"Mutation gate passed: score={report.score:.2f}, "
        f"passed={report.passed}, total={report.total}. Report: {args.output_json}"
    )
    return 0
