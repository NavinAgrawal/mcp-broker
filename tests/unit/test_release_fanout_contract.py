from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.release_fanout import ReleaseFanoutStep, run_release_fanout


pytestmark = pytest.mark.unit


def test_release_fanout_writes_failed_step_to_ledger_without_blocking_verification(
    tmp_path: Path,
) -> None:
    ledger_path = tmp_path / "release" / "publish-everywhere-ledger.json"
    calls: list[list[str]] = []

    def runner(command: list[str]) -> int:
        calls.append(command)
        return 1 if command == ["make", "npm"] else 0

    status = run_release_fanout(
        [
            ReleaseFanoutStep(name="npm", command=("make", "npm")),
            ReleaseFanoutStep(name="docker", command=("make", "docker")),
        ],
        ledger_path=ledger_path,
        runner=runner,
    )

    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))

    assert status == 0
    assert calls == [["make", "npm"], ["make", "docker"]]
    assert ledger["summary"] == {"total": 2, "passed": 1, "failed": 1}
    assert ledger["steps"][0]["name"] == "npm"
    assert ledger["steps"][0]["status"] == "failed"
    assert ledger["steps"][0]["exit_code"] == 1
    assert ledger["steps"][1]["name"] == "docker"
    assert ledger["steps"][1]["status"] == "passed"
    assert ledger["steps"][1]["exit_code"] == 0
