from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support.makefiles import (
    expand_make_value,
    read_combined_makefiles,
    read_make_variable_defaults,
)


pytestmark = pytest.mark.journey

ROOT = Path(__file__).resolve().parents[2]


def _package_version() -> str:
    package = json.loads((ROOT / "npm" / "package.json").read_text(encoding="utf-8"))
    return str(package["version"])


def test_npm_package_is_scoped_and_delegates_to_python_runtime() -> None:
    make_vars = read_make_variable_defaults(ROOT)
    package = json.loads((ROOT / "npm" / "package.json").read_text(encoding="utf-8"))
    wrapper = (ROOT / "npm" / "bin" / "mcp-broker.js").read_text(encoding="utf-8")
    readme = (ROOT / "npm" / "README.md").read_text(encoding="utf-8")
    allowlist_path = ROOT / "public-export" / "allowlist.txt"

    assert package["name"] == expand_make_value(make_vars, make_vars["NPM_PACKAGE_NAME"])
    assert package["version"] == _package_version()
    assert package["author"] == make_vars["PACKAGE_AUTHOR"]
    assert package["license"] == "MIT"
    assert package["bin"] == {"mcp-broker": "bin/mcp-broker.js"}
    assert package["files"] == ["bin/", "README.md"]
    assert "dependencies" not in package
    assert "uvx" in wrapper
    assert "mcp-broker==" in wrapper
    assert "MCP_BROKER_NPM_DEV_ROOT" in wrapper
    assert "spawnSync" in wrapper
    assert "does not reimplement the Python broker in Node" in readme
    if allowlist_path.exists():
        assert "npm/**" in allowlist_path.read_text(encoding="utf-8")


def test_makefile_exposes_npm_distribution_targets() -> None:
    makefile = read_combined_makefiles(ROOT)
    make_vars = read_make_variable_defaults(ROOT)

    for term in [
        f"NPM_PACKAGE_NAME  ?= {make_vars['NPM_PACKAGE_NAME']}",
        "npm-account-check:",
        "npm-package-check:",
        "npm-smoke:",
        "npm-release-smoke:",
        "publish-everywhere:",
        "MCP_BROKER_NPM_DEV_ROOT",
        "NPM_PUBLISH_PROVENANCE_ARGS ?=",
        "$(NPM) publish --access public $(NPM_PUBLISH_PROVENANCE_ARGS)",
    ]:
        assert term in makefile

    assert make_vars["NPM_PUBLISH_PROVENANCE_ARGS"] == ""
    assert "publish-npm:" not in makefile
