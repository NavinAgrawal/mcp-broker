from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import sync_release_metadata
from scripts.sync_release_metadata import (
    _bump_version,
    _validate_version,
    docker_catalog_version_from_text,
    replace_docker_catalog_version,
)

pytestmark = pytest.mark.unit


def test_release_bump_calculates_patch_minor_and_major_versions() -> None:
    assert _bump_version("2.3.4", "patch") == "2.3.5"
    assert _bump_version("2.3.4", "minor") == "2.4.0"
    assert _bump_version("2.3.4", "major") == "3.0.0"


def test_release_version_validation_rejects_non_semver() -> None:
    assert _validate_version("2.3.4") == "2.3.4"

    try:
        _validate_version("v2.3.4")
    except ValueError as exc:
        assert "invalid semantic version" in str(exc)
    else:
        raise AssertionError("version validation accepted v-prefixed input")


def test_emit_version_only_does_not_report_synchronization() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/sync_release_metadata.py",
            "--version",
            "9.8.7",
            "--emit-version",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout == "9.8.7\n"
    assert result.stderr == ""


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


@pytest.mark.error_simulation
def test_json_metadata_sync_includes_codex_plugin_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    version = "9.8.7"
    package = {"version": "1.2.3"}
    registry_metadata = {"version": "1.2.3", "packages": [{"version": "1.2.3"}]}

    _write_json(tmp_path / "npm" / "package.json", package)
    _write_json(tmp_path / ".codex-plugin" / "plugin.json", package)
    _write_json(tmp_path / "registry" / "server.json", registry_metadata)
    _write_json(tmp_path / "registry" / "server.template.json", registry_metadata)
    _write_json(tmp_path / "mcpb" / "manifest.json", package)
    _write_json(tmp_path / ".well-known" / "mcp" / "server-card.json", {"packages": [{"version": "1.2.3"}]})
    monkeypatch.setattr(sync_release_metadata, "ROOT", tmp_path)

    updates = sync_release_metadata._json_metadata_updates(version)

    assert updates[".codex-plugin/plugin.json"]["version"] == version


def test_docker_catalog_version_sync_uses_standard_library_parser() -> None:
    text = "name: example-broker\nimage: ${DOCKER_REPOSITORY_IMAGE}:2.3.4\ncategory: dev\n"

    updated = replace_docker_catalog_version(text, "2.3.5")

    assert docker_catalog_version_from_text(updated) == "2.3.5"
    assert updated == (
        "name: example-broker\n"
        "image: ${DOCKER_REPOSITORY_IMAGE}:2.3.5\n"
        "category: dev\n"
    )
