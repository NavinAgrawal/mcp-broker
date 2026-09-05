from __future__ import annotations

import sys

import pytest

from pathlib import Path

from tests.support.repo_paths import make_command, private_config_path


pytestmark = pytest.mark.unit


def test_make_command_neutralizes_mutmut_stats_for_nested_make(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MUTANT_UNDER_TEST", "stats")

    command = make_command("plugin-bootstrap-preflight")

    assert command == [
        "make",
        "plugin-bootstrap-preflight",
        "MUTANT_UNDER_TEST=mcp_broker_mutmut_subprocess_original",
        f"PYTHON={sys.executable}",
        f"PYTHON_BIN={sys.executable}",
    ]


def test_make_command_preserves_specific_mutant_for_nested_make(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutant_name = "mcp_broker.bootstrap_transactions.x_plan__mutmut_1"
    monkeypatch.setenv("MUTANT_UNDER_TEST", mutant_name)

    command = make_command("plugin-bootstrap-preflight")

    assert "MUTANT_UNDER_TEST=mcp_broker_mutmut_subprocess_original" not in command
    assert command == [
        "make",
        "plugin-bootstrap-preflight",
        f"PYTHON={sys.executable}",
        f"PYTHON_BIN={sys.executable}",
    ]


def test_private_config_path_prefers_live_config_over_isolated_runtime_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configured = tmp_path / "configured.yaml"
    live = tmp_path / "live.yaml"
    monkeypatch.setenv("MCP_BROKER_CONFIG", str(configured))
    monkeypatch.setenv("MCP_BROKER_LIVE_CONFIG_PATH", str(live))

    assert private_config_path() == live


def test_private_config_path_falls_back_to_live_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    live = tmp_path / "live.yaml"
    monkeypatch.delenv("MCP_BROKER_CONFIG", raising=False)
    monkeypatch.setenv("MCP_BROKER_LIVE_CONFIG_PATH", str(live))

    assert private_config_path() == live


def test_private_config_path_is_absent_without_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MCP_BROKER_CONFIG", raising=False)
    monkeypatch.delenv("MCP_BROKER_LIVE_CONFIG_PATH", raising=False)

    assert private_config_path() is None
