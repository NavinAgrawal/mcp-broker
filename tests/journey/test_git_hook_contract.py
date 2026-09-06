from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.journey


def run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for name in (
        "MAKEFLAGS", "MFLAGS", "MAKELEVEL", "MAKEOVERRIDES",
        "PYTEST_TARGETED_LOG", "PYTEST_LIVE_TARGETED_LOG", "TEST_LOG_DIR",
    ):
        env.pop(name, None)
    env["PYTHON"] = sys.executable
    return subprocess.run(args, cwd=repo, capture_output=True, text=True, check=False, env=env)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    target = tmp_path / "repo"
    target.mkdir()
    assert run(target, "git", "init", "-b", "main").returncode == 0
    for path in ("Makefile", "mk", "scripts", ".cits", ".githooks", ".gitignore"):
        source = ROOT / path
        if source.is_dir():
            shutil.copytree(source, target / path)
        elif source.is_file():
            shutil.copy2(source, target / path)
    return target


def make(repo: Path, target: str, *args: str) -> subprocess.CompletedProcess[str]:
    return run(repo, "make", target, f"PYTHON={sys.executable}", *args)


def test_install_is_idempotent_and_tracks_portable_hooks(repo: Path) -> None:
    first = make(repo, "hooks-install")
    assert first.returncode == 0, first.stderr
    second = make(repo, "hooks-install")
    assert second.returncode == 0, second.stderr
    assert run(repo, "git", "config", "--get", "core.hooksPath").stdout.strip() == ".githooks"
    assert os.access(repo / ".githooks/pre-commit", os.X_OK)


def test_install_refuses_unknown_hook_owner(repo: Path) -> None:
    assert run(repo, "git", "config", "core.hooksPath", "custom-hooks").returncode == 0
    result = make(repo, "hooks-install")
    assert result.returncode != 0
    assert "existing" in result.stderr.lower()
    assert run(repo, "git", "config", "--get", "core.hooksPath").stdout.strip() == "custom-hooks"


def test_secret_gate_scans_staged_content_not_clean_working_copy(repo: Path) -> None:
    secret = "ghp_" + "aB3cD4eF5gH6iJ7kL8mN9pQ0rS1tU2vW3xY4"
    target = repo / "credential.txt"
    target.write_text(secret + "\n", encoding="utf-8")
    assert run(repo, "git", "add", "credential.txt").returncode == 0
    target.write_text("clean working copy\n", encoding="utf-8")
    result = make(repo, "hook-secret-scan")
    assert result.returncode != 0
    assert "leaks found" in (result.stdout + result.stderr).lower()
    assert secret not in result.stdout + result.stderr


def test_secret_gate_accepts_clean_staged_content(repo: Path) -> None:
    (repo / "note.txt").write_text("public documentation\n", encoding="utf-8")
    assert run(repo, "git", "add", "note.txt").returncode == 0
    result = make(repo, "hook-secret-scan")
    assert result.returncode == 0, result.stderr
    assert "no leaks found" in (result.stdout + result.stderr).lower()
    assert "1 staged" in result.stdout


def test_secret_gate_fails_closed_when_scanner_missing(repo: Path) -> None:
    (repo / "note.txt").write_text("public documentation\n", encoding="utf-8")
    assert run(repo, "git", "add", "note.txt").returncode == 0
    result = make(repo, "hook-secret-scan", f"GITLEAKS={repo / 'absent-scanner'}")
    assert result.returncode != 0
    assert "Missing" in result.stderr
    assert "no leaks found" not in result.stdout + result.stderr


def test_linked_worktree_install_does_not_change_primary_hooks(repo: Path) -> None:
    assert run(repo, "git", "add", ".").returncode == 0
    assert run(repo, "git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "Fixture").returncode == 0
    linked = repo.parent / "linked"
    assert run(repo, "git", "worktree", "add", "-b", "task", str(linked)).returncode == 0
    result = make(linked, "hooks-install")
    assert result.returncode == 0, result.stderr
    assert run(linked, "git", "config", "--get", "core.hooksPath").stdout.strip() == ".githooks"
    assert run(repo, "git", "config", "--get", "core.hooksPath").returncode == 1


def test_install_preserves_existing_default_hook(repo: Path) -> None:
    hook = repo / ".git/hooks/pre-commit"
    content = "#!/bin/sh\nexit 0\n"
    hook.write_text(content, encoding="utf-8")
    result = make(repo, "hooks-install")
    assert result.returncode != 0
    assert "existing Git hooks" in result.stderr
    assert hook.read_text(encoding="utf-8") == content


def test_secret_gate_rejects_empty_staged_scope(repo: Path) -> None:
    result = make(repo, "hook-secret-scan")
    assert result.returncode != 0
    assert "0 staged paths" in result.stdout
    assert "no leaks found" not in result.stdout + result.stderr


def test_install_refuses_common_worktree_setting_before_enabling_extension(repo: Path) -> None:
    assert run(repo, "git", "config", "--local", "core.worktree", str(repo)).returncode == 0
    result = make(repo, "hooks-install")
    assert result.returncode != 0
    assert "core.worktree" in result.stderr
    assert run(repo, "git", "config", "--get", "extensions.worktreeConfig").returncode == 1


def test_public_export_includes_hook_implementation_and_contracts() -> None:
    for path in (".githooks/pre-commit", "scripts/install_git_hooks.py", "tests/journey/test_git_hook_contract.py"):
        assert (ROOT / path).is_file(), path


@pytest.mark.parametrize("valid", [True, False])
def test_installed_hook_runs_only_selected_tests_and_blocks_failures(repo: Path, valid: bool) -> None:
    tests = repo / "tests/unit"
    tests.mkdir(parents=True)
    (tests / "test_unrelated.py").write_text(
        "def test_unrelated():\n    raise RuntimeError('unrelated test must not run')\n",
        encoding="utf-8",
    )
    assert run(repo, "git", "add", ".").returncode == 0
    identity = ("-c", "user.name=Test", "-c", "user.email=test@example.invalid")
    assert run(repo, "git", *identity, "commit", "-m", "Fixture").returncode == 0
    baseline = run(repo, "git", "rev-parse", "HEAD").stdout
    assert make(repo, "hooks-install").returncode == 0
    (tests / "test_selected.py").write_text(
        "from pathlib import Path\nimport os\n"
        "def test_selected():\n"
        "    Path('selected-receipt').write_text('executed')\n"
        "    assert os.getenv('GIT_INDEX_FILE') is None\n"
        f"    assert Path('Makefile').is_file() is {valid!r}\n",
        encoding="utf-8",
    )
    assert run(repo, "git", "add", "tests/unit/test_selected.py").returncode == 0
    result = run(repo, "git", *identity, "commit", "-m", "Selected contract")
    assert (result.returncode == 0) is valid, result.stdout + result.stderr
    assert (repo / "selected-receipt").read_text() == "executed"
    assert (repo / "var/test-logs/targeted.log").is_file()
    assert "1 affected pytest files selected" in result.stdout + result.stderr
    assert "unrelated test must not run" not in result.stdout + result.stderr
    assert (run(repo, "git", "rev-parse", "HEAD").stdout != baseline) is valid
