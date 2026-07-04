from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.release_idempotency import (
    ReleaseIdempotencyError,
    decide_release_action,
    inspect_mcp_registry_release,
    inspect_npm_release,
    inspect_pypi_release,
)


pytestmark = pytest.mark.unit


def _write_artifact(path: Path, payload: bytes) -> str:
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def test_release_action_publishes_when_remote_version_is_missing(tmp_path: Path) -> None:
    artifact = tmp_path / "mcp_broker-9.8.7.tar.gz"
    digest = _write_artifact(artifact, b"sdist")

    action = decide_release_action(
        surface="pypi",
        version="9.8.7",
        expected={"mcp_broker-9.8.7.tar.gz": digest},
        remote={},
    )

    assert action.status == "publish"
    assert action.surface == "pypi"
    assert action.version == "9.8.7"


def test_release_action_skips_when_remote_artifact_digest_matches(tmp_path: Path) -> None:
    artifact = tmp_path / "mcp_broker-9.8.7.tar.gz"
    digest = _write_artifact(artifact, b"sdist")

    action = decide_release_action(
        surface="pypi",
        version="9.8.7",
        expected={"mcp_broker-9.8.7.tar.gz": digest},
        remote={"mcp_broker-9.8.7.tar.gz": digest},
    )

    assert action.status == "skip"
    assert action.verified == {"mcp_broker-9.8.7.tar.gz": digest}


def test_release_action_fails_closed_when_remote_digest_mismatches(tmp_path: Path) -> None:
    artifact = tmp_path / "mcp_broker-9.8.7.tar.gz"
    digest = _write_artifact(artifact, b"sdist")

    with pytest.raises(ReleaseIdempotencyError, match="digest mismatch"):
        decide_release_action(
            surface="pypi",
            version="9.8.7",
            expected={"mcp_broker-9.8.7.tar.gz": digest},
            remote={"mcp_broker-9.8.7.tar.gz": "0" * 64},
        )


def test_pypi_inspector_compares_every_local_dist_artifact(tmp_path: Path) -> None:
    sdist = tmp_path / "mcp_broker-9.8.7.tar.gz"
    wheel = tmp_path / "mcp_broker-9.8.7-py3-none-any.whl"
    sdist_digest = _write_artifact(sdist, b"sdist")
    wheel_digest = _write_artifact(wheel, b"wheel")

    payload = {
        "info": {"version": "9.8.7"},
        "urls": [
            {"filename": sdist.name, "digests": {"sha256": sdist_digest}},
            {"filename": wheel.name, "digests": {"sha256": wheel_digest}},
        ],
    }

    action = inspect_pypi_release(
        version="9.8.7",
        dist_dir=tmp_path,
        fetch_json=lambda _url: payload,
        pypi_version_url="https://pypi.example.test/pypi/mcp-broker/9.8.7/json",
    )

    assert action.status == "skip"
    assert action.verified == {sdist.name: sdist_digest, wheel.name: wheel_digest}


def test_npm_inspector_compares_local_package_integrity(tmp_path: Path) -> None:
    package_dir = tmp_path / "npm"
    package_dir.mkdir()
    package_json = package_dir / "package.json"
    package_json.write_text(
        json.dumps({"name": "@example/mcp-broker", "version": "9.8.7"}),
        encoding="utf-8",
    )
    pack = tmp_path / "package.tgz"
    integrity = "sha512-test-integrity"

    action = inspect_npm_release(
        version="9.8.7",
        package_dir=package_dir,
        package_name="@example/mcp-broker",
        npm_registry_url="https://npm.example.test",
        npm_pack=lambda _package_dir: (pack.name, integrity),
        fetch_json=lambda _url: {
            "versions": {"9.8.7": {"dist": {"integrity": integrity}}},
            "dist-tags": {"latest": "9.8.7"},
        },
    )

    assert action.status == "skip"
    assert action.verified == {pack.name: integrity}


def test_mcp_registry_inspector_requires_matching_name_and_version() -> None:
    metadata = {
        "servers": [
            {"server": {"name": "io.example/other", "version": "9.8.7"}},
            {"server": {"name": "io.example/mcp-broker", "version": "9.8.7"}},
        ]
    }

    nonmatching = inspect_mcp_registry_release(
        version="9.8.7",
        registry_name="io.example/missing",
        fetch_json=lambda _url: metadata,
        mcp_registry_search_url="https://registry.example.test/search",
    )

    action = inspect_mcp_registry_release(
        version="9.8.7",
        registry_name="io.example/mcp-broker",
        fetch_json=lambda _url: metadata,
        mcp_registry_search_url="https://registry.example.test/search",
    )

    assert nonmatching.status == "publish"
    assert action.status == "skip"
    assert action.verified == {"io.example/mcp-broker": "9.8.7"}
