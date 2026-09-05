from __future__ import annotations

import os
from pathlib import Path
import sys

_MUTMUT_STATS_SENTINEL = "stats"
_MUTMUT_SUBPROCESS_ORIGINAL = "mcp_broker_mutmut_subprocess_original"


def repo_root() -> Path:
    configured = os.environ.get("MCP_BROKER_REPO_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    for start in (Path.cwd().resolve(), Path(__file__).resolve()):
        for candidate in (start, *start.parents):
            if (candidate / "Makefile").exists() and (
                candidate / "config" / "broker.example.yaml"
            ).exists():
                return candidate
    return Path(__file__).resolve().parents[2]


def private_config_path() -> Path | None:
    for variable in ("MCP_BROKER_LIVE_CONFIG_PATH", "MCP_BROKER_CONFIG"):
        configured = os.environ.get(variable)
        if configured:
            return Path(configured).expanduser().resolve()
    return None


def make_command(*args: str) -> list[str]:
    mutation_args = []
    if os.environ.get("MUTANT_UNDER_TEST") == _MUTMUT_STATS_SENTINEL:
        mutation_args.append(f"MUTANT_UNDER_TEST={_MUTMUT_SUBPROCESS_ORIGINAL}")
    return [
        "make",
        *args,
        *mutation_args,
        f"PYTHON={sys.executable}",
        f"PYTHON_BIN={sys.executable}",
    ]
