import argparse
import json
from pathlib import Path

import pytest

from mcp_broker.mutation_stats import (
    ALL_STATUSES,
    MutationReport,
    blocked_file_summary,
    build_parser,
    build_report,
    load_mutant_results,
    main,
    mutant_source_path,
    non_negative_int,
    status_for_exit_code,
    write_report,
)


pytestmark = pytest.mark.unit


def write_meta(path: Path, exit_codes: list[object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "exit_code_by_key": {
                    f"mcp_broker.sample.x_func__mutmut_{index}": code
                    for index, code in enumerate(exit_codes, start=1)
                }
            }
        ),
        encoding="utf-8",
    )


def test_mutation_stats_gate_passes_when_all_mutants_are_killed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mutants_dir = tmp_path / "mutants"
    report_path = tmp_path / "quality" / "mutation_stats.json"
    write_meta(mutants_dir / "src" / "mcp_broker" / "sample.py.meta", [1, 3, 37])

    result = main(
        [
            "--mutants-dir",
            str(mutants_dir),
            "--output-json",
            str(report_path),
            "--min-score",
            "100",
        ]
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert result == 0
    assert "Mutation gate passed" in capsys.readouterr().out
    assert report["total"] == 3
    assert report["passed"] == 3
    assert report["score"] == 100.0
    assert report["counts"]["killed"] == 2
    assert report["counts"]["caught_by_type_check"] == 1


def test_mutation_stats_filters_report_to_selected_mutants(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mutants_dir = tmp_path / "mutants"
    report_path = tmp_path / "quality" / "mutation_stats.json"
    meta_path = mutants_dir / "src" / "mcp_broker" / "sample.py.meta"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(
        json.dumps(
            {
                "exit_code_by_key": {
                    "mcp_broker.sample.x_selected__mutmut_1": 1,
                    "mcp_broker.sample.x_unselected_survivor__mutmut_2": 0,
                    "mcp_broker.sample.x_unselected_not_checked__mutmut_3": None,
                    "mcp_broker.sample.x_unselected_segfault__mutmut_4": -9,
                }
            }
        ),
        encoding="utf-8",
    )

    result = main(
        [
            "--mutants-dir",
            str(mutants_dir),
            "--output-json",
            str(report_path),
            "--min-score",
            "100",
            "--include-mutants",
            "mcp_broker.sample.x_selected__mutmut_1",
        ]
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert result == 0
    assert "Mutation gate passed" in capsys.readouterr().out
    assert report["total"] == 1
    assert report["passed"] == 1
    assert report["counts"]["killed"] == 1
    assert report["counts"]["survived"] == 0
    assert report["counts"]["not_checked"] == 0
    assert report["counts"]["segfault"] == 0


def test_mutation_stats_filters_report_with_a_changed_callable_pattern(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mutants_dir = tmp_path / "mutants"
    report_path = tmp_path / "quality" / "mutation_stats.json"
    meta_path = mutants_dir / "src" / "mcp_broker" / "sample.py.meta"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(
        json.dumps(
            {
                "exit_code_by_key": {
                    "mcp_broker.sample.x_changed__mutmut_1": 1,
                    "mcp_broker.sample.x_changed__mutmut_2": 1,
                    "mcp_broker.sample.x_unchanged__mutmut_1": 0,
                }
            }
        ),
        encoding="utf-8",
    )

    result = main(
        [
            "--mutants-dir",
            str(mutants_dir),
            "--output-json",
            str(report_path),
            "--min-score",
            "100",
            "--include-mutants",
            "mcp_broker.sample.x_changed__mutmut_*",
        ]
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert result == 0
    assert "Mutation gate passed" in capsys.readouterr().out
    assert report["total"] == 2
    assert report["passed"] == 2
    assert report["counts"]["survived"] == 0


def test_mutation_stats_fails_when_selected_mutant_is_missing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mutants_dir = tmp_path / "mutants"
    report_path = tmp_path / "quality" / "mutation_stats.json"
    write_meta(mutants_dir / "src" / "mcp_broker" / "sample.py.meta", [1])

    result = main(
        [
            "--mutants-dir",
            str(mutants_dir),
            "--output-json",
            str(report_path),
            "--include-mutants",
            "mcp_broker.sample.x_missing__mutmut_99",
        ]
    )

    assert result == 1
    assert capsys.readouterr().out == (
        "Mutation gate failed: selected mutants not found: "
        "mcp_broker.sample.x_missing__mutmut_99. "
        f"Report: {report_path}\n"
    )
    assert json.loads(report_path.read_text(encoding="utf-8"))["total"] == 0


def test_mutation_stats_reports_multiple_missing_selected_mutants_with_exact_separator(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mutants_dir = tmp_path / "mutants"
    report_path = tmp_path / "quality" / "mutation_stats.json"
    write_meta(mutants_dir / "src" / "mcp_broker" / "sample.py.meta", [1])

    result = main(
        [
            "--mutants-dir",
            str(mutants_dir),
            "--output-json",
            str(report_path),
            "--include-mutants",
            "mcp_broker.sample.x_missing_a__mutmut_98",
            "mcp_broker.sample.x_missing_b__mutmut_99",
        ]
    )

    assert result == 1
    assert capsys.readouterr().out == (
        "Mutation gate failed: selected mutants not found: "
        "mcp_broker.sample.x_missing_a__mutmut_98, "
        "mcp_broker.sample.x_missing_b__mutmut_99. "
        f"Report: {report_path}\n"
    )


def test_mutation_stats_fails_when_any_selected_mutant_is_missing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mutants_dir = tmp_path / "mutants"
    report_path = tmp_path / "quality" / "mutation_stats.json"
    meta_path = mutants_dir / "src" / "mcp_broker" / "sample.py.meta"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(
        json.dumps(
            {
                "exit_code_by_key": {
                    "mcp_broker.sample.x_selected__mutmut_1": 1,
                }
            }
        ),
        encoding="utf-8",
    )

    result = main(
        [
            "--mutants-dir",
            str(mutants_dir),
            "--output-json",
            str(report_path),
            "--include-mutants",
            "mcp_broker.sample.x_selected__mutmut_1",
            "mcp_broker.sample.x_missing__mutmut_99",
        ]
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert result == 1
    assert capsys.readouterr().out == (
        "Mutation gate failed: selected mutants not found: "
        "mcp_broker.sample.x_missing__mutmut_99. "
        f"Report: {report_path}\n"
    )
    assert report["total"] == 1
    assert report["passed"] == 1


def test_mutation_stats_reports_multiple_missing_selected_mutants_after_filtering(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mutants_dir = tmp_path / "mutants"
    report_path = tmp_path / "quality" / "mutation_stats.json"
    meta_path = mutants_dir / "src" / "mcp_broker" / "sample.py.meta"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(
        json.dumps(
            {
                "exit_code_by_key": {
                    "mcp_broker.sample.x_selected__mutmut_1": 1,
                }
            }
        ),
        encoding="utf-8",
    )

    result = main(
        [
            "--mutants-dir",
            str(mutants_dir),
            "--output-json",
            str(report_path),
            "--include-mutants",
            "mcp_broker.sample.x_selected__mutmut_1",
            "mcp_broker.sample.x_missing_a__mutmut_98",
            "mcp_broker.sample.x_missing_b__mutmut_99",
        ]
    )

    assert result == 1
    assert capsys.readouterr().out == (
        "Mutation gate failed: selected mutants not found: "
        "mcp_broker.sample.x_missing_a__mutmut_98, "
        "mcp_broker.sample.x_missing_b__mutmut_99. "
        f"Report: {report_path}\n"
    )


def test_mutation_stats_gate_fails_for_unusable_statuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mutants_dir = tmp_path / "mutants"
    report_path = tmp_path / "quality" / "mutation_stats.json"
    write_meta(
        mutants_dir / "src" / "mcp_broker" / "sample.py.meta",
        [0, 33, 34, 35, 36, 2, -9, None],
    )

    result = main(
        [
            "--mutants-dir",
            str(mutants_dir),
            "--output-json",
            str(report_path),
        ]
    )

    stdout = capsys.readouterr().out
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert result == 1
    assert stdout == (
        "Mutation gate failed: check_was_interrupted_by_user=1, no_tests=1, "
        "not_checked=1, segfault=1, skipped=1, survived=1, suspicious=1, "
        f"timeout=1. Report: {report_path}\n"
    )
    assert "survived=1" in stdout
    assert "no_tests=1" in stdout
    assert "skipped=1" in stdout
    assert "suspicious=1" in stdout
    assert "timeout=1" in stdout
    assert "check_was_interrupted_by_user=1" in stdout
    assert "segfault=1" in stdout
    assert "not_checked=1" in stdout
    assert report["total"] == 8
    assert report["score"] == 0.0
    assert report["blocked_by_file"] == [
        {
            "blocked": 8,
            "counts": {
                "check_was_interrupted_by_user": 1,
                "no_tests": 1,
                "not_checked": 1,
                "segfault": 1,
                "skipped": 1,
                "survived": 1,
                "suspicious": 1,
                "timeout": 1,
            },
            "examples": {
                "check_was_interrupted_by_user": ["mcp_broker.sample.x_func__mutmut_6"],
                "no_tests": ["mcp_broker.sample.x_func__mutmut_2"],
                "not_checked": ["mcp_broker.sample.x_func__mutmut_8"],
                "segfault": ["mcp_broker.sample.x_func__mutmut_7"],
                "skipped": ["mcp_broker.sample.x_func__mutmut_3"],
                "survived": ["mcp_broker.sample.x_func__mutmut_1"],
                "suspicious": ["mcp_broker.sample.x_func__mutmut_4"],
                "timeout": ["mcp_broker.sample.x_func__mutmut_5"],
            },
            "path": "src/mcp_broker/sample.py",
            "total": 8,
        }
    ]


def test_mutation_stats_gate_fails_when_no_mutants_exist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mutants_dir = tmp_path / "mutants"
    mutants_dir.mkdir()
    report_path = tmp_path / "quality" / "mutation_stats.json"

    result = main(
        [
            "--mutants-dir",
            str(mutants_dir),
            "--output-json",
            str(report_path),
        ]
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert result == 1
    assert capsys.readouterr().out == (
        f"Mutation gate failed: no mutants found. Report: {report_path}\n"
    )
    assert report["total"] == 0
    assert report["score"] == 0.0


def test_mutation_stats_gate_fails_when_score_is_too_low(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mutants_dir = tmp_path / "mutants"
    report_path = tmp_path / "quality" / "mutation_stats.json"
    write_meta(mutants_dir / "src" / "mcp_broker" / "sample.py.meta", [1, 0])

    result = main(
        [
            "--mutants-dir",
            str(mutants_dir),
            "--output-json",
            str(report_path),
            "--min-score",
            "100",
            "--fail-statuses",
            "no_tests",
        ]
    )

    assert result == 1
    assert capsys.readouterr().out == (
        f"Mutation gate failed: score=50.00, min_score=100.00. Report: {report_path}\n"
    )
    assert json.loads(report_path.read_text(encoding="utf-8"))["score"] == 50.0


def test_mutation_stats_rejects_invalid_metadata(tmp_path: Path) -> None:
    mutants_dir = tmp_path / "mutants"
    meta_path = mutants_dir / "src" / "mcp_broker" / "sample.py.meta"
    meta_path.parent.mkdir(parents=True)
    meta_path.write_text(json.dumps({"exit_code_by_key": []}), encoding="utf-8")

    with pytest.raises(ValueError, match="exit_code_by_key"):
        build_report(mutants_dir)


def test_mutation_stats_rejects_metadata_without_exit_code_mapping(tmp_path: Path) -> None:
    mutants_dir = tmp_path / "mutants"
    meta_path = mutants_dir / "src" / "mcp_broker" / "sample.py.meta"
    meta_path.parent.mkdir(parents=True)
    meta_path.write_text(json.dumps({}), encoding="utf-8")

    with pytest.raises(ValueError, match="object exit_code_by_key"):
        build_report(mutants_dir)


def test_mutation_stats_rejects_missing_mutants_dir(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Mutants directory"):
        build_report(tmp_path / "missing")


def test_mutation_stats_maps_unknown_codes_to_suspicious() -> None:
    assert status_for_exit_code(9999) == "suspicious"
    assert status_for_exit_code("bad") == "suspicious"
    assert status_for_exit_code(True) == "suspicious"
    assert status_for_exit_code(False) == "suspicious"


def test_mutant_source_path_keeps_paths_without_meta_suffix(tmp_path: Path) -> None:
    mutants_dir = tmp_path / "mutants"
    meta_path = mutants_dir / "src" / "mcp_broker" / "sample.py"

    assert mutant_source_path(meta_path, mutants_dir) == "src/mcp_broker/sample.py"


def test_mutation_stats_ranks_blocked_files_and_limits_examples(tmp_path: Path) -> None:
    mutants_dir = tmp_path / "mutants"
    report_path = tmp_path / "quality" / "mutation_stats.json"
    write_meta(
        mutants_dir / "src" / "mcp_broker" / "small.py.meta",
        [0],
    )
    write_meta(
        mutants_dir / "src" / "mcp_broker" / "large.py.meta",
        [0, 0, 33, 1],
    )

    result = main(
        [
            "--mutants-dir",
            str(mutants_dir),
            "--output-json",
            str(report_path),
            "--example-limit",
            "1",
        ]
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert result == 1
    assert [entry["path"] for entry in report["blocked_by_file"]] == [
        "src/mcp_broker/large.py",
        "src/mcp_broker/small.py",
    ]
    assert report["blocked_by_file"][0]["blocked"] == 3
    assert report["blocked_by_file"][0]["examples"] == {
        "no_tests": ["mcp_broker.sample.x_func__mutmut_3"],
        "survived": ["mcp_broker.sample.x_func__mutmut_1"],
    }


def test_load_mutant_results_requires_metadata_object(tmp_path: Path) -> None:
    mutants_dir = tmp_path / "mutants"
    meta_path = mutants_dir / "src" / "mcp_broker" / "sample.py.meta"
    meta_path.parent.mkdir(parents=True)
    meta_path.write_text(json.dumps([]), encoding="utf-8")

    with pytest.raises(ValueError, match="metadata object"):
        load_mutant_results(mutants_dir)


def test_load_mutant_results_orders_sources_and_mutants(tmp_path: Path) -> None:
    mutants_dir = tmp_path / "mutants"
    beta = mutants_dir / "src" / "mcp_broker" / "beta.py.meta"
    alpha = mutants_dir / "src" / "mcp_broker" / "alpha.py.meta"
    beta.parent.mkdir(parents=True)
    beta.write_text(
        json.dumps(
            {
                "exit_code_by_key": {
                    "mcp_broker.beta.z__mutmut_2": 0,
                    "mcp_broker.beta.a__mutmut_1": 1,
                }
            }
        ),
        encoding="utf-8",
    )
    alpha.parent.mkdir(parents=True, exist_ok=True)
    alpha.write_text(
        json.dumps({"exit_code_by_key": {"mcp_broker.alpha.only__mutmut_1": 37}}),
        encoding="utf-8",
    )

    assert load_mutant_results(mutants_dir) == [
        ("src/mcp_broker/alpha.py", "mcp_broker.alpha.only__mutmut_1", "caught_by_type_check"),
        ("src/mcp_broker/beta.py", "mcp_broker.beta.a__mutmut_1", "killed"),
        ("src/mcp_broker/beta.py", "mcp_broker.beta.z__mutmut_2", "survived"),
    ]


def test_blocked_file_summary_rejects_negative_example_limit() -> None:
    with pytest.raises(ValueError) as exc_info:
        blocked_file_summary(
            [("src/mcp_broker/sample.py", "mcp_broker.sample.x__mutmut_1", "survived")],
            fail_statuses=["survived"],
            example_limit=-1,
        )
    assert str(exc_info.value) == "example_limit must be greater than or equal to 0"


def test_blocked_file_summary_keeps_zero_example_limit() -> None:
    summary = blocked_file_summary(
        [
            ("src/mcp_broker/sample.py", "mcp_broker.sample.x__mutmut_1", "survived"),
            ("src/mcp_broker/sample.py", "mcp_broker.sample.y__mutmut_2", "timeout"),
        ],
        fail_statuses=["survived", "timeout"],
        example_limit=0,
    )

    assert summary == [
        {
            "blocked": 2,
            "counts": {"survived": 1, "timeout": 1},
            "examples": {"survived": [], "timeout": []},
            "path": "src/mcp_broker/sample.py",
            "total": 2,
        }
    ]


def test_build_report_rejects_unknown_fail_status(tmp_path: Path) -> None:
    mutants_dir = tmp_path / "mutants"
    write_meta(mutants_dir / "src" / "mcp_broker" / "sample.py.meta", [0])

    with pytest.raises(ValueError, match="unknown fail status: surived"):
        build_report(mutants_dir, fail_statuses=["surived"])


def test_build_report_reports_multiple_unknown_fail_statuses(tmp_path: Path) -> None:
    mutants_dir = tmp_path / "mutants"
    write_meta(mutants_dir / "src" / "mcp_broker" / "sample.py.meta", [0])

    with pytest.raises(ValueError) as exc_info:
        build_report(mutants_dir, fail_statuses=["zombie", "surived"])

    assert str(exc_info.value) == "unknown fail status: surived, zombie"


def test_build_report_preserves_empty_fail_status_list(tmp_path: Path) -> None:
    mutants_dir = tmp_path / "mutants"
    write_meta(mutants_dir / "src" / "mcp_broker" / "sample.py.meta", [0])

    report = build_report(mutants_dir, fail_statuses=[])

    assert report.counts["survived"] == 1
    assert report.blocked_by_file == []


def test_build_report_default_example_limit_keeps_ten_examples(tmp_path: Path) -> None:
    mutants_dir = tmp_path / "mutants"
    write_meta(mutants_dir / "src" / "mcp_broker" / "sample.py.meta", [0] * 11)

    report = build_report(mutants_dir, fail_statuses=["survived"])

    examples = report.blocked_by_file[0]["examples"]["survived"]
    assert report.blocked_by_file[0]["counts"]["survived"] == 11
    assert examples == [
        "mcp_broker.sample.x_func__mutmut_1",
        "mcp_broker.sample.x_func__mutmut_10",
        "mcp_broker.sample.x_func__mutmut_11",
        "mcp_broker.sample.x_func__mutmut_2",
        "mcp_broker.sample.x_func__mutmut_3",
        "mcp_broker.sample.x_func__mutmut_4",
        "mcp_broker.sample.x_func__mutmut_5",
        "mcp_broker.sample.x_func__mutmut_6",
        "mcp_broker.sample.x_func__mutmut_7",
        "mcp_broker.sample.x_func__mutmut_8",
    ]


def test_parser_rejects_unknown_fail_status(tmp_path: Path) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(
            [
                "--mutants-dir",
                str(tmp_path / "mutants"),
                "--output-json",
                str(tmp_path / "quality" / "mutation_stats.json"),
                "--fail-statuses",
                "surived",
            ]
        )

    assert exc_info.value.code == 2


def test_parser_rejects_negative_example_limit(tmp_path: Path) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(
            [
                "--mutants-dir",
                str(tmp_path / "mutants"),
                "--output-json",
                str(tmp_path / "quality" / "mutation_stats.json"),
                "--example-limit",
                "-1",
            ]
        )

    assert exc_info.value.code == 2


def test_non_negative_int_accepts_zero_and_reports_exact_error() -> None:
    assert non_negative_int("0") == 0

    with pytest.raises(
        argparse.ArgumentTypeError,
        match="^must be greater than or equal to 0$",
    ):
        non_negative_int("-1")


def test_parser_accepts_selected_mutant_filter(tmp_path: Path) -> None:
    parser = build_parser()

    parsed = parser.parse_args(
        [
            "--mutants-dir",
            str(tmp_path / "mutants"),
            "--output-json",
            str(tmp_path / "quality" / "mutation_stats.json"),
            "--include-mutants",
            "mcp_broker.sample.x_one__mutmut_1",
            "mcp_broker.sample.x_two__mutmut_2",
        ]
    )

    assert parsed.include_mutants == [
        "mcp_broker.sample.x_one__mutmut_1",
        "mcp_broker.sample.x_two__mutmut_2",
    ]


def test_parser_contract_includes_required_options_and_defaults(tmp_path: Path) -> None:
    parser = build_parser()

    parsed = parser.parse_args(
        [
            "--mutants-dir",
            str(tmp_path / "mutants"),
            "--output-json",
            str(tmp_path / "quality" / "mutation_stats.json"),
            "--min-score",
            "99.5",
            "--example-limit",
            "0",
        ]
    )

    assert parser.description == "Validate mutmut metadata as a release gate."
    assert parsed.mutants_dir == tmp_path / "mutants"
    assert parsed.output_json == tmp_path / "quality" / "mutation_stats.json"
    assert parsed.min_score == 99.5
    assert parsed.example_limit == 0
    assert parsed.include_mutants is None
    include_action = next(action for action in parser._actions if "--include-mutants" in action.option_strings)
    assert include_action.nargs == "+"
    assert include_action.help == "Optional mutant names to grade; unselected mutants are ignored."


@pytest.mark.parametrize(
    "argv",
    [
        ["--output-json", "var/quality/mutation_stats.json"],
        ["--mutants-dir", "var/quality/mutants"],
        [
            "--mutants-dir",
            "var/quality/mutants",
            "--output-json",
            "var/quality/mutation_stats.json",
            "--min-score",
            "not-a-number",
        ],
    ],
)
def test_parser_rejects_missing_required_options_and_bad_min_score(argv: list[str]) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(argv)

    assert exc_info.value.code == 2


def test_write_report_creates_parent_and_sorts_keys(tmp_path: Path) -> None:
    output_json = tmp_path / "nested" / "quality" / "mutation_stats.json"
    report = MutationReport(
        counts={"survived": 1, "killed": 2},
        total=3,
        passed=2,
        score=66.6667,
        blocked_by_file=[
            {
                "blocked": 1,
                "counts": {"survived": 1},
                "examples": {"survived": ["mcp_broker.sample.x__mutmut_1"]},
                "path": "src/mcp_broker/sample.py",
                "total": 3,
            }
        ],
    )

    write_report(report, output_json)
    write_report(report, output_json)

    payload = output_json.read_text(encoding="utf-8")
    assert payload == (
        "{\n"
        '  "blocked_by_file": [\n'
        "    {\n"
        '      "blocked": 1,\n'
        '      "counts": {\n'
        '        "survived": 1\n'
        "      },\n"
        '      "examples": {\n'
        '        "survived": [\n'
        '          "mcp_broker.sample.x__mutmut_1"\n'
        "        ]\n"
        "      },\n"
        '      "path": "src/mcp_broker/sample.py",\n'
        '      "total": 3\n'
        "    }\n"
        "  ],\n"
        '  "counts": {\n'
        '    "killed": 2,\n'
        '    "survived": 1\n'
        "  },\n"
        '  "passed": 2,\n'
        '  "score": 66.6667,\n'
        '  "total": 3\n'
        "}\n"
    )
    assert list(json.loads(payload)) == [
        "blocked_by_file",
        "counts",
        "passed",
        "score",
        "total",
    ]


def test_default_fail_statuses_match_all_non_passing_statuses() -> None:
    parser = build_parser()
    parsed = parser.parse_args(
        [
            "--mutants-dir",
            "mutants",
            "--output-json",
            "var/quality/mutation_stats.json",
        ]
    )

    assert parsed.fail_statuses == [
        status for status in ALL_STATUSES if status not in {"killed", "caught_by_type_check"}
    ]
    assert parsed.min_score == 100.0
    assert parsed.example_limit == 10
