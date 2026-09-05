from __future__ import annotations

import json
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit

SAMPLE_RUNTIME_VERSION = "9.8.7"
NEXT_SAMPLE_RUNTIME_VERSION = "9.8.8"


def test_runtime_install_store_records_active_and_previous_manifests(tmp_path: Path) -> None:
    from mcp_broker.runtime_install import (
        ACTIVE_RUNTIME_POINTER,
        PREVIOUS_RUNTIME_POINTER,
        RUNTIME_INSTALL_DIR,
        RUNTIME_MANIFEST_NAME,
        RUNTIME_VERSIONS_DIR,
        RuntimeInstallStore,
    )

    state_dir = tmp_path / "runtime" / "state"
    first_runtime = tmp_path / "installed" / "versions" / SAMPLE_RUNTIME_VERSION
    second_runtime = tmp_path / "installed" / "versions" / NEXT_SAMPLE_RUNTIME_VERSION
    first_runtime.mkdir(parents=True)
    second_runtime.mkdir(parents=True)

    store = RuntimeInstallStore(state_dir)
    first = store.record_installed_runtime(
        version=SAMPLE_RUNTIME_VERSION,
        runtime_path=first_runtime,
        entrypoint="bin/mcp-broker",
        artifact_digest="sha256:first-runtime",
    )
    second = store.record_installed_runtime(
        version=NEXT_SAMPLE_RUNTIME_VERSION,
        runtime_path=second_runtime,
        entrypoint="bin/mcp-broker",
        artifact_digest="sha256:second-runtime",
    )

    install_dir = state_dir / RUNTIME_INSTALL_DIR
    first_manifest = install_dir / RUNTIME_VERSIONS_DIR / SAMPLE_RUNTIME_VERSION / first["runtime_id"] / RUNTIME_MANIFEST_NAME
    second_manifest = (
        install_dir / RUNTIME_VERSIONS_DIR / NEXT_SAMPLE_RUNTIME_VERSION / second["runtime_id"] / RUNTIME_MANIFEST_NAME
    )

    assert first["runtime_id"] != second["runtime_id"]
    assert _read_json(install_dir / ACTIVE_RUNTIME_POINTER) == {
        "runtime_id": second["runtime_id"],
        "manifest_path": str(second_manifest),
    }
    assert _read_json(install_dir / PREVIOUS_RUNTIME_POINTER) == {
        "runtime_id": first["runtime_id"],
        "manifest_path": str(first_manifest),
    }
    assert _read_json(second_manifest) == {
        "artifact_digest": "sha256:second-runtime",
        "entrypoint": "bin/mcp-broker",
        "runtime_id": second["runtime_id"],
        "runtime_path": str(second_runtime),
        "status": "installed",
        "version": NEXT_SAMPLE_RUNTIME_VERSION,
    }


def test_runtime_install_store_rejects_unsafe_version_path_segments(tmp_path: Path) -> None:
    from mcp_broker.runtime_install import (
        ACTIVE_RUNTIME_POINTER,
        RUNTIME_INSTALL_DIR,
        RuntimeInstallError,
        RuntimeInstallStore,
    )

    state_dir = tmp_path / "runtime" / "state"
    store = RuntimeInstallStore(state_dir)

    with pytest.raises(RuntimeInstallError, match="version"):
        store.record_installed_runtime(
            version="../escape",
            runtime_path=tmp_path / "installed" / "escape",
            entrypoint="bin/mcp-broker",
            artifact_digest="sha256:escape",
        )

    assert not (state_dir / RUNTIME_INSTALL_DIR / ACTIVE_RUNTIME_POINTER).exists()
    assert not (state_dir / "runtime-install-escape").exists()


@pytest.mark.parametrize("version", ["", " ", ".", ".."])
def test_runtime_install_store_rejects_empty_or_reserved_versions(tmp_path: Path, version: str) -> None:
    from mcp_broker.runtime_install import RuntimeInstallError, RuntimeInstallStore

    with pytest.raises(RuntimeInstallError, match="version"):
        RuntimeInstallStore(tmp_path / "runtime" / "state").record_installed_runtime(
            version=version,
            runtime_path=tmp_path / "installed" / "runtime",
            entrypoint="bin/mcp-broker",
            artifact_digest="sha256:first-runtime",
        )


def test_runtime_install_store_rejects_empty_entrypoint(tmp_path: Path) -> None:
    from mcp_broker.runtime_install import RuntimeInstallError, RuntimeInstallStore

    with pytest.raises(RuntimeInstallError, match="entrypoint is required"):
        RuntimeInstallStore(tmp_path / "runtime" / "state").record_installed_runtime(
            version=SAMPLE_RUNTIME_VERSION,
            runtime_path=tmp_path / "installed" / "runtime",
            entrypoint=" ",
            artifact_digest="sha256:first-runtime",
        )


def test_runtime_install_store_rejects_empty_artifact_digest(tmp_path: Path) -> None:
    from mcp_broker.runtime_install import RuntimeInstallError, RuntimeInstallStore

    with pytest.raises(RuntimeInstallError, match="artifact_digest is required"):
        RuntimeInstallStore(tmp_path / "runtime" / "state").record_installed_runtime(
            version=SAMPLE_RUNTIME_VERSION,
            runtime_path=tmp_path / "installed" / "runtime",
            entrypoint="bin/mcp-broker",
            artifact_digest=" ",
        )


def test_runtime_install_store_rejects_version_symlink_escape(tmp_path: Path) -> None:
    from mcp_broker.runtime_install import RuntimeInstallError, RuntimeInstallStore

    state_dir = tmp_path / "runtime" / "state"
    store = RuntimeInstallStore(state_dir)
    store.versions_dir.mkdir(parents=True)
    outside = tmp_path / "outside-version"
    outside.mkdir()
    (store.versions_dir / SAMPLE_RUNTIME_VERSION).symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeInstallError, match="outside runtime versions directory"):
        store.record_installed_runtime(
            version=SAMPLE_RUNTIME_VERSION,
            runtime_path=tmp_path / "installed" / "runtime",
            entrypoint="bin/mcp-broker",
            artifact_digest="sha256:first-runtime",
        )


def test_runtime_install_store_rejects_non_object_active_pointer(tmp_path: Path) -> None:
    from mcp_broker.runtime_install import RuntimeInstallError, RuntimeInstallStore

    state_dir = tmp_path / "runtime" / "state"
    store = RuntimeInstallStore(state_dir)
    store.active_pointer.parent.mkdir(parents=True)
    store.active_pointer.write_text("[]", encoding="utf-8")

    with pytest.raises(RuntimeInstallError, match="expected JSON object"):
        store.record_installed_runtime(
            version=SAMPLE_RUNTIME_VERSION,
            runtime_path=tmp_path / "installed" / "runtime",
            entrypoint="bin/mcp-broker",
            artifact_digest="sha256:first-runtime",
        )


def test_runtime_install_store_preserves_same_version_rollbacks_by_runtime_id(tmp_path: Path) -> None:
    from mcp_broker.runtime_install import (
        ACTIVE_RUNTIME_POINTER,
        PREVIOUS_RUNTIME_POINTER,
        RUNTIME_INSTALL_DIR,
        RUNTIME_MANIFEST_NAME,
        RUNTIME_VERSIONS_DIR,
        RuntimeInstallStore,
    )

    state_dir = tmp_path / "runtime" / "state"
    runtime_path = tmp_path / "installed" / "versions" / SAMPLE_RUNTIME_VERSION
    runtime_path.mkdir(parents=True)

    store = RuntimeInstallStore(state_dir)
    first = store.record_installed_runtime(
        version=SAMPLE_RUNTIME_VERSION,
        runtime_path=runtime_path,
        entrypoint="bin/mcp-broker",
        artifact_digest="sha256:first-runtime",
    )
    second = store.record_installed_runtime(
        version=SAMPLE_RUNTIME_VERSION,
        runtime_path=runtime_path,
        entrypoint="bin/mcp-broker",
        artifact_digest="sha256:second-runtime",
    )

    install_dir = state_dir / RUNTIME_INSTALL_DIR
    first_manifest = install_dir / RUNTIME_VERSIONS_DIR / SAMPLE_RUNTIME_VERSION / first["runtime_id"] / RUNTIME_MANIFEST_NAME
    second_manifest = install_dir / RUNTIME_VERSIONS_DIR / SAMPLE_RUNTIME_VERSION / second["runtime_id"] / RUNTIME_MANIFEST_NAME

    assert first_manifest != second_manifest
    assert _read_json(first_manifest)["artifact_digest"] == "sha256:first-runtime"
    assert _read_json(second_manifest)["artifact_digest"] == "sha256:second-runtime"
    assert _read_json(install_dir / ACTIVE_RUNTIME_POINTER) == {
        "runtime_id": second["runtime_id"],
        "manifest_path": str(second_manifest),
    }
    assert _read_json(install_dir / PREVIOUS_RUNTIME_POINTER) == {
        "runtime_id": first["runtime_id"],
        "manifest_path": str(first_manifest),
    }


def test_runtime_install_store_keeps_active_pointer_when_previous_pointer_write_fails(
    tmp_path: Path,
) -> None:
    from mcp_broker.runtime_install import (
        ACTIVE_RUNTIME_POINTER,
        RUNTIME_INSTALL_DIR,
        RuntimeInstallError,
        RuntimeInstallStore,
    )

    state_dir = tmp_path / "runtime" / "state"
    first_runtime = tmp_path / "installed" / "versions" / SAMPLE_RUNTIME_VERSION
    second_runtime = tmp_path / "installed" / "versions" / NEXT_SAMPLE_RUNTIME_VERSION
    first_runtime.mkdir(parents=True)
    second_runtime.mkdir(parents=True)

    store = RuntimeInstallStore(state_dir)
    first = store.record_installed_runtime(
        version=SAMPLE_RUNTIME_VERSION,
        runtime_path=first_runtime,
        entrypoint="bin/mcp-broker",
        artifact_digest="sha256:first-runtime",
    )
    store.previous_pointer.mkdir()

    with pytest.raises(RuntimeInstallError, match="previous-runtime"):
        store.record_installed_runtime(
            version=NEXT_SAMPLE_RUNTIME_VERSION,
            runtime_path=second_runtime,
            entrypoint="bin/mcp-broker",
            artifact_digest="sha256:second-runtime",
        )

    assert _read_json(state_dir / RUNTIME_INSTALL_DIR / ACTIVE_RUNTIME_POINTER) == {
        "runtime_id": first["runtime_id"],
        "manifest_path": first["manifest_path"],
    }


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))
