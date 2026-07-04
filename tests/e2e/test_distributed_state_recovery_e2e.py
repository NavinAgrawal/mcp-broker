from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path

import pytest


pytestmark = pytest.mark.e2e


# REAL_INFRA: this E2E exercises the real local filesystem state adapter.
TENANT_CONTEXT = {
    "tenant_id": "tenant-a",
    "workspace_id": "workspace-a",
    "user_id": "user-a",
}
NOW = datetime(2026, 7, 4, 7, 0, tzinfo=UTC)


def test_distributed_state_apply_recover_and_rollback_flow(tmp_path: Path) -> None:
    from mcp_broker.distributed_state import DistributedStateStore

    state_dir = tmp_path / "state"
    store = DistributedStateStore(state_dir)
    lock = store.acquire_lock(
        owner_id="worker-a",
        tenant_context=TENANT_CONTEXT,
        now=NOW,
        ttl_seconds=60,
    )
    first = store.apply_state(
        lock=lock,
        state={"deployment_id": "deploy-a", "bundle_version": "1.0.0"},
        expected_active_revision=None,
    )
    second = store.apply_state(
        lock=lock,
        state={"deployment_id": "deploy-b", "bundle_version": "1.0.1"},
        expected_active_revision=first["revision"],
    )

    active_path = state_dir / "shared-runtime" / "active.json"
    active_path.unlink()
    recovery = store.recover()
    rollback = store.rollback(lock=lock)

    assert recovery["active_revision"] == second["revision"]
    assert recovery["replayed"] is True
    assert rollback == {
        "active_revision": first["revision"],
        "previous_revision": second["revision"],
    }
    assert _read_json(active_path)["state"] == {
        "bundle_version": "1.0.0",
        "deployment_id": "deploy-a",
    }
    assert _audit_results(state_dir) == [
        ("distributed_state_lock", "acquired"),
        ("distributed_state_apply", "allowed"),
        ("distributed_state_apply", "allowed"),
        ("distributed_state_recovery", "replayed"),
        ("distributed_state_rollback", "allowed"),
    ]


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _audit_results(state_dir: Path) -> list[tuple[str, str]]:
    audit_path = state_dir / "shared-runtime" / "audit.jsonl"
    return [
        (event["event_type"], event["result"])
        for event in (
            json.loads(line)
            for line in audit_path.read_text(encoding="utf-8").splitlines()
        )
    ]
