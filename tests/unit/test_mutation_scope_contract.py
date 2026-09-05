from pathlib import Path
import subprocess

import pytest

from mcp_broker.mutation_scope import (
    build_parser,
    changed_paths_from_git,
    load_whole_file_carveouts,
    load_mutation_roots,
    main,
    select_all_mutation_paths,
    select_changed_mutation_paths,
)


pytestmark = pytest.mark.unit


def test_load_mutation_roots_reads_setup_cfg_source_of_truth(tmp_path: Path) -> None:
    setup_cfg = tmp_path / "setup.cfg"
    setup_cfg.write_text(
        "[mutmut]\n"
        "paths_to_mutate=\n"
        "    src/mcp_broker\n"
        "    scripts\n",
        encoding="utf-8",
    )

    assert load_mutation_roots(setup_cfg) == [
        Path("src/mcp_broker"),
        Path("scripts"),
    ]


def test_load_mutation_roots_rejects_noncanonical_option_case(tmp_path: Path) -> None:
    setup_cfg = tmp_path / "setup.cfg"
    setup_cfg.write_text(
        "[mutmut]\nPATHS_TO_MUTATE=src/mcp_broker\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match=r"setup\.cfg has no mutmut paths_to_mutate$",
    ):
        load_mutation_roots(setup_cfg)


def test_load_whole_file_carveouts_reads_only_whole_file_registry_rows(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "mutation-carveouts.md"
    registry.write_text(
        "| file | line range | reason class |\n"
        "|---|---|---|\n"
        "| `src/mcp_broker/config_keys.py` | whole file | tool-incompatible |\n"
        "| `src/mcp_broker/upstream_protocols.py` | whole file, lines 1-48 | typing-only |\n"
        "| `src/mcp_broker/daemon.py` | lines 10-20 | tool-incompatible |\n",
        encoding="utf-8",
    )

    assert load_whole_file_carveouts(registry) == {
        Path("src/mcp_broker/config_keys.py"),
        Path("src/mcp_broker/upstream_protocols.py"),
    }


def test_load_whole_file_carveouts_accepts_minimal_row_and_rejects_unbalanced_ticks(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "mutation-carveouts.md"
    registry.write_text(
        "| `src/mcp_broker/minimal.py` | whole file\n"
        "| `src/mcp_broker/leading.py | whole file |\n"
        "| src/mcp_broker/trailing.py` | whole file |\n",
        encoding="utf-8",
    )

    assert load_whole_file_carveouts(registry) == {
        Path("src/mcp_broker/minimal.py")
    }


def test_load_whole_file_carveouts_requires_utf8_registry(tmp_path: Path) -> None:
    registry = tmp_path / "mutation-carveouts.md"
    registry.write_bytes(b"\xff")

    with pytest.raises(UnicodeDecodeError):
        load_whole_file_carveouts(registry)


def test_load_whole_file_carveouts_fails_closed_when_registry_is_missing(
    tmp_path: Path,
) -> None:
    with pytest.raises(FileNotFoundError):
        load_whole_file_carveouts(tmp_path / "missing.md")


def test_select_changed_mutation_paths_keeps_existing_python_under_mutation_roots(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path
    kept = repo_root / "src" / "mcp_broker" / "changed.py"
    ignored_test = repo_root / "tests" / "unit" / "test_changed.py"
    ignored_text = repo_root / "src" / "mcp_broker" / "README.md"
    ignored_deleted = repo_root / "src" / "mcp_broker" / "deleted.py"
    kept.parent.mkdir(parents=True)
    ignored_test.parent.mkdir(parents=True)
    kept.write_text("VALUE = 1\n", encoding="utf-8")
    ignored_test.write_text("def test_value(): pass\n", encoding="utf-8")
    ignored_text.write_text("text\n", encoding="utf-8")

    assert select_changed_mutation_paths(
        repo_root,
        [
            Path("src/mcp_broker/changed.py"),
            Path("src/mcp_broker/README.md"),
            Path("src/mcp_broker/deleted.py"),
            Path("tests/unit/test_changed.py"),
        ],
        mutation_roots=[Path("src/mcp_broker")],
    ) == [kept.relative_to(repo_root)]
    assert not ignored_deleted.exists()


def test_select_changed_mutation_paths_skips_invalid_entries_without_stopping_or_duplicates(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path
    first = repo_root / "src" / "mcp_broker" / "first.py"
    second = repo_root / "src" / "mcp_broker" / "second.py"
    outside = repo_root / "scripts" / "outside.py"
    first.parent.mkdir(parents=True)
    outside.parent.mkdir(parents=True)
    first.write_text("FIRST = 1\n", encoding="utf-8")
    second.write_text("SECOND = 2\n", encoding="utf-8")
    outside.write_text("OUTSIDE = 3\n", encoding="utf-8")

    assert select_changed_mutation_paths(
        repo_root,
        [
            Path("README.md"),
            Path("src/mcp_broker/missing.py"),
            Path("scripts/outside.py"),
            Path("src/mcp_broker/first.py"),
            Path("src/mcp_broker/first.py"),
            Path("src/mcp_broker/second.py"),
        ],
        mutation_roots=[Path("src/mcp_broker")],
    ) == [Path("src/mcp_broker/first.py"), Path("src/mcp_broker/second.py")]


def test_select_changed_mutation_paths_excludes_only_whole_file_carveouts(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path
    carved = repo_root / "src" / "mcp_broker" / "config_keys.py"
    partial = repo_root / "src" / "mcp_broker" / "daemon.py"
    carved.parent.mkdir(parents=True)
    carved.write_text("VALUE = 1\n", encoding="utf-8")
    partial.write_text("VALUE = 2\n", encoding="utf-8")

    assert select_changed_mutation_paths(
        repo_root,
        [carved.relative_to(repo_root), partial.relative_to(repo_root)],
        mutation_roots=[Path("src/mcp_broker")],
        whole_file_carveouts={Path("src/mcp_broker/config_keys.py")},
    ) == [Path("src/mcp_broker/daemon.py")]


def test_select_all_mutation_paths_enumerates_roots_and_excludes_whole_file_carveouts(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path
    package = repo_root / "src" / "mcp_broker"
    nested = package / "alpha"
    scripts = repo_root / "scripts"
    nested.mkdir(parents=True)
    scripts.mkdir()
    (package / "alpha.py").write_text("ALPHA = 1\n", encoding="utf-8")
    (package / "alpha-.py").write_text("ALPHA_DASH = 2\n", encoding="utf-8")
    (nested / "beta.py").write_text("BETA = 2\n", encoding="utf-8")
    (package / "carved.py").write_text("CARVED = 3\n", encoding="utf-8")
    (package / "notes.txt").write_text("not Python\n", encoding="utf-8")
    (scripts / "tool.py").write_text("TOOL = 4\n", encoding="utf-8")

    assert select_all_mutation_paths(
        repo_root,
        mutation_roots=[Path("src/mcp_broker"), Path("scripts")],
        whole_file_carveouts={Path("src/mcp_broker/carved.py")},
    ) == [
        Path("scripts/tool.py"),
        Path("src/mcp_broker/alpha-.py"),
        Path("src/mcp_broker/alpha.py"),
        Path("src/mcp_broker/alpha/beta.py"),
    ]


def test_select_all_mutation_paths_accepts_a_file_root(tmp_path: Path) -> None:
    source = tmp_path / "src" / "single.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8")

    assert select_all_mutation_paths(
        tmp_path,
        mutation_roots=[Path("src/single.py")],
    ) == [Path("src/single.py")]


def test_select_all_mutation_paths_accepts_multiple_file_roots(tmp_path: Path) -> None:
    first = tmp_path / "src" / "first.py"
    second = tmp_path / "src" / "second.py"
    first.parent.mkdir()
    first.write_text("FIRST = 1\n", encoding="utf-8")
    second.write_text("SECOND = 2\n", encoding="utf-8")

    assert select_all_mutation_paths(
        tmp_path,
        mutation_roots=[Path("src/first.py"), Path("src/second.py")],
    ) == [Path("src/first.py"), Path("src/second.py")]


def test_mutation_scope_parser_has_exact_public_contract() -> None:
    parser = build_parser()
    assert parser.description == (
        "Resolve mutation paths from the repo's mutmut configuration and git diff."
    )
    actions = {action.dest: action for action in parser._actions}
    assert set(actions) == {"help", "root", "diff_base", "select_all", "format"}
    assert actions["root"].type is Path
    assert actions["root"].default == Path.cwd()
    assert actions["diff_base"].default == "origin/main"
    assert actions["select_all"].default is False
    assert actions["format"].choices == ["lines", "make"]
    assert actions["format"].default == "lines"


def _write_mutation_repo(
    repo_root: Path,
    *,
    source_names: tuple[str, ...],
) -> None:
    package = repo_root / "src" / "mcp_broker"
    package.mkdir(parents=True)
    for source_name in source_names:
        (package / source_name).write_text("VALUE = 1\n", encoding="utf-8")
    (repo_root / "setup.cfg").write_text(
        "[mutmut]\npaths_to_mutate=src/mcp_broker\n",
        encoding="utf-8",
    )
    docs = repo_root / "docs"
    docs.mkdir()
    (docs / "mutation-carveouts.md").write_text(
        "| file | line range | reason class |\n|---|---|---|\n",
        encoding="utf-8",
    )


def test_main_prints_full_mutable_universe_in_make_format(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_mutation_repo(tmp_path, source_names=("z.py", "a.py"))

    assert main(["--root", str(tmp_path), "--all", "--format", "make"]) == 0
    assert capsys.readouterr().out == (
        "src/mcp_broker/a.py src/mcp_broker/z.py\n"
    )


def test_main_full_universe_applies_registry_carveouts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_mutation_repo(tmp_path, source_names=("kept.py", "carved.py"))
    (tmp_path / "docs" / "mutation-carveouts.md").write_text(
        "| file | line range | reason class |\n"
        "|---|---|---|\n"
        "| `src/mcp_broker/carved.py` | whole file | tool-incompatible |\n",
        encoding="utf-8",
    )

    assert main(["--root", str(tmp_path), "--all", "--format", "make"]) == 0
    assert capsys.readouterr().out == "src/mcp_broker/kept.py\n"


def test_main_prints_changed_paths_in_lines_format(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_mutation_repo(tmp_path, source_names=("changed.py", "second.py", "carved.py"))
    (tmp_path / "docs" / "mutation-carveouts.md").write_text(
        "| file | line range | reason class |\n"
        "|---|---|---|\n"
        "| `src/mcp_broker/carved.py` | whole file | tool-incompatible |\n",
        encoding="utf-8",
    )

    class Result:
        returncode = 0
        stdout = (
            "src/mcp_broker/changed.py\n"
            "src/mcp_broker/carved.py\n"
            "src/mcp_broker/second.py\n"
        )
        stderr = ""

    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **kwargs: object) -> Result:
        calls.append((command, kwargs))
        return Result()

    monkeypatch.setattr(
        "mcp_broker.mutation_scope.subprocess.run",
        run,
    )

    assert main(["--root", str(tmp_path), "--diff-base", "base"]) == 0
    assert capsys.readouterr().out == (
        "src/mcp_broker/changed.py\n"
        "src/mcp_broker/second.py\n"
    )
    assert calls == [
        (
            ["git", "diff", "--name-only", "--diff-filter=ACMRT", "base", "--"],
            {
                "cwd": tmp_path.resolve(),
                "check": False,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
            },
        )
    ]


def test_main_emits_no_newline_for_an_empty_full_universe(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_mutation_repo(tmp_path, source_names=())

    assert main(["--root", str(tmp_path), "--all", "--format", "make"]) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.error_simulation
def test_changed_paths_from_git_parses_stdout_and_rejects_failed_git(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    class Result:
        returncode = 0
        stdout = "src/mcp_broker/a.py\nREADME.md\n"
        stderr = ""

    def run(command: list[str], **kwargs: object) -> Result:
        calls.append((command, kwargs))
        return Result()

    monkeypatch.setattr("mcp_broker.mutation_scope.subprocess.run", run)

    assert changed_paths_from_git(tmp_path, "origin/main") == [
        Path("src/mcp_broker/a.py"),
        Path("README.md"),
    ]
    assert calls == [
        (
            [
                "git",
                "diff",
                "--name-only",
                "--diff-filter=ACMRT",
                "origin/main",
                "--",
            ],
            {
                "cwd": tmp_path,
                "check": False,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
            },
        )
    ]


@pytest.mark.error_simulation
def test_changed_paths_from_git_fails_closed_on_git_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Result:
        returncode = 128
        stdout = ""
        stderr = "fatal: bad revision"

    def run(_command: list[str], **_kwargs: object) -> Result:
        return Result()

    monkeypatch.setattr("mcp_broker.mutation_scope.subprocess.run", run)

    with pytest.raises(RuntimeError, match="git diff failed"):
        changed_paths_from_git(tmp_path, "missing")
