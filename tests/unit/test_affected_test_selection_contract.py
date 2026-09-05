from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SELECTOR = ROOT / "scripts" / "select_affected_tests.py"
pytestmark = pytest.mark.unit


def _run_selector(
    repo: Path,
    changed: list[str] | None,
    *,
    tier: str = "push",
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if changed is None:
        env.pop("CITS_CHANGED_FILES", None)
    else:
        env["CITS_CHANGED_FILES"] = "\n".join(changed)
    return subprocess.run(
        [
            sys.executable,
            str(SELECTOR),
            "--root",
            str(repo),
            "--tier",
            tier,
            "--base",
            "origin/main",
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


def test_selector_maps_source_module_to_named_and_importing_tests(tmp_path: Path) -> None:
    (tmp_path / "src/mcp_broker").mkdir(parents=True)
    (tmp_path / "tests/unit").mkdir(parents=True)
    (tmp_path / "src/mcp_broker/widget.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "tests/unit/test_widget_contract.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "tests/unit/test_consumer.py").write_text(
        "from mcp_broker.widget import VALUE\n",
        encoding="utf-8",
    )

    result = _run_selector(tmp_path, ["src/mcp_broker/widget.py"])

    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        "tests/unit/test_consumer.py",
        "tests/unit/test_widget_contract.py",
    ]


def test_selector_applies_declared_non_code_mapping(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "tests/journey").mkdir(parents=True)
    (tmp_path / "docs/release.md").write_text("release\n", encoding="utf-8")
    (tmp_path / "tests/journey/test_release.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / ".test-impact.json").write_text(
        json.dumps(
            {
                "version": 1,
                "map": [
                    {
                        "changed": "docs/**",
                        "runTests": ["tests/journey/test_release.py"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = _run_selector(tmp_path, ["docs/release.md"])

    assert result.returncode == 0
    assert result.stdout.splitlines() == ["tests/journey/test_release.py"]


def test_selector_fails_when_changed_file_has_no_test_mapping(tmp_path: Path) -> None:
    (tmp_path / "src/mcp_broker").mkdir(parents=True)
    (tmp_path / "src/mcp_broker/orphan.py").write_text("VALUE = 1\n", encoding="utf-8")

    result = _run_selector(tmp_path, ["src/mcp_broker/orphan.py"])

    assert result.returncode == 2
    assert "no affected tests: src/mcp_broker/orphan.py" in result.stderr


def test_selector_maps_distribution_make_fragment_to_npm_contract() -> None:
    result = _run_selector(ROOT, ["mk/distribution.mk"])

    assert result.returncode == 0
    assert "tests/journey/test_npm_distribution_contract.py" in result.stdout.splitlines()


def test_selector_maps_support_module_to_importing_tests_only(tmp_path: Path) -> None:
    (tmp_path / "tests/support").mkdir(parents=True)
    (tmp_path / "tests/unit").mkdir(parents=True)
    (tmp_path / "tests/support/helpers.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "tests/unit/test_consumer.py").write_text(
        "from tests.support.helpers import VALUE\n",
        encoding="utf-8",
    )
    (tmp_path / "tests/unit/test_unrelated.py").write_text("pass\n", encoding="utf-8")

    result = _run_selector(tmp_path, ["tests/support/helpers.py"])

    assert result.returncode == 0
    assert result.stdout.splitlines() == ["tests/unit/test_consumer.py"]


def test_selector_maps_deleted_source_to_its_tests(tmp_path: Path) -> None:
    (tmp_path / "src/mcp_broker").mkdir(parents=True)
    (tmp_path / "tests/unit").mkdir(parents=True)
    source = tmp_path / "src/mcp_broker/widget.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "tests/unit/test_widget_contract.py").write_text("pass\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "src", "tests"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=tmp_path, check=True)
    source.unlink()

    result = _run_selector(tmp_path, None, tier="commit")

    assert result.returncode == 0
    assert result.stdout.splitlines() == ["tests/unit/test_widget_contract.py"]
