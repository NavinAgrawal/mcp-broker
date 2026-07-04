from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit


TENANT_CONTEXT = {
    "tenant_id": "tenant-a",
    "workspace_id": "workspace-a",
    "user_id": "user-a",
}
NOW = datetime(2026, 7, 4, 6, 0, tzinfo=UTC)


def test_distributed_state_store_acquires_lock_with_audit_event(
    tmp_path: Path,
) -> None:
    from mcp_broker.distributed_state import DistributedStateStore

    store = DistributedStateStore(tmp_path / "state")

    lock = store.acquire_lock(
        owner_id="worker-a",
        tenant_context=TENANT_CONTEXT,
        now=NOW,
        ttl_seconds=60,
    )

    assert lock == {
        "owner_id": "worker-a",
        "token": "000000000001",
        "tenant_id": "tenant-a",
        "workspace_id": "workspace-a",
        "user_id": "user-a",
        "acquired_at": "2026-07-04T06:00:00Z",
        "expires_at": "2026-07-04T06:01:00Z",
    }
    assert _read_json(tmp_path / "state" / "shared-runtime" / "lock.json") == lock
    assert _audit_events(tmp_path / "state")[-1]["event_type"] == "distributed_state_lock"
    assert _audit_events(tmp_path / "state")[-1]["result"] == "acquired"


def test_distributed_state_store_rejects_active_lock_conflict_with_audit(
    tmp_path: Path,
) -> None:
    from mcp_broker.distributed_state import (
        DistributedStateConflict,
        DistributedStateStore,
    )

    store = DistributedStateStore(tmp_path / "state")
    store.acquire_lock(
        owner_id="worker-a",
        tenant_context=TENANT_CONTEXT,
        now=NOW,
        ttl_seconds=60,
    )

    with pytest.raises(DistributedStateConflict, match="state lock is held"):
        store.acquire_lock(
            owner_id="worker-b",
            tenant_context=TENANT_CONTEXT,
            now=NOW + timedelta(seconds=30),
            ttl_seconds=60,
        )

    assert _audit_events(tmp_path / "state")[-1] == {
        "event_type": "distributed_state_lock",
        "tenant_id": "tenant-a",
        "workspace_id": "workspace-a",
        "user_id": "user-a",
        "owner_id": "worker-b",
        "result": "denied",
        "denial_reason": "lock_conflict",
    }


def test_distributed_state_store_recovers_stale_lock_before_acquiring(
    tmp_path: Path,
) -> None:
    from mcp_broker.distributed_state import DistributedStateStore

    store = DistributedStateStore(tmp_path / "state")
    stale = store.acquire_lock(
        owner_id="worker-a",
        tenant_context=TENANT_CONTEXT,
        now=NOW,
        ttl_seconds=30,
    )

    recovered = store.acquire_lock(
        owner_id="worker-b",
        tenant_context=TENANT_CONTEXT,
        now=NOW + timedelta(seconds=31),
        ttl_seconds=60,
    )

    assert recovered["owner_id"] == "worker-b"
    assert recovered["token"] == "000000000002"
    assert _audit_events(tmp_path / "state")[-2] == {
        "event_type": "distributed_state_lock",
        "tenant_id": "tenant-a",
        "workspace_id": "workspace-a",
        "user_id": "user-a",
        "owner_id": "worker-b",
        "result": "recovered",
        "stale_owner_id": stale["owner_id"],
        "stale_token": stale["token"],
    }


def test_distributed_state_store_applies_state_with_conflict_rejection_and_journal(
    tmp_path: Path,
) -> None:
    from mcp_broker.distributed_state import (
        DistributedStateConflict,
        DistributedStateStore,
    )

    store = DistributedStateStore(tmp_path / "state")
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

    assert first["revision"] == 1
    assert first["status"] == "active"
    assert _read_json(tmp_path / "state" / "shared-runtime" / "active.json")[
        "state"
    ] == {"bundle_version": "1.0.0", "deployment_id": "deploy-a"}

    with pytest.raises(DistributedStateConflict, match="active revision conflict"):
        store.apply_state(
            lock=lock,
            state={"deployment_id": "deploy-b", "bundle_version": "1.0.1"},
            expected_active_revision=99,
        )

    assert _journal_actions(tmp_path / "state") == ["apply"]
    assert _audit_events(tmp_path / "state")[-1]["denial_reason"] == "revision_conflict"


def test_distributed_state_store_rolls_back_to_previous_revision(
    tmp_path: Path,
) -> None:
    from mcp_broker.distributed_state import DistributedStateStore

    store = DistributedStateStore(tmp_path / "state")
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

    rollback = store.rollback(lock=lock)

    assert rollback == {
        "active_revision": first["revision"],
        "previous_revision": second["revision"],
    }
    assert _read_json(tmp_path / "state" / "shared-runtime" / "active.json")[
        "revision"
    ] == first["revision"]
    assert _journal_actions(tmp_path / "state") == ["apply", "apply", "rollback"]


def test_distributed_state_store_replays_journal_during_recovery(
    tmp_path: Path,
) -> None:
    from mcp_broker.distributed_state import DistributedStateStore

    store = DistributedStateStore(tmp_path / "state")
    lock = store.acquire_lock(
        owner_id="worker-a",
        tenant_context=TENANT_CONTEXT,
        now=NOW,
        ttl_seconds=60,
    )
    applied = store.apply_state(
        lock=lock,
        state={"deployment_id": "deploy-a", "bundle_version": "1.0.0"},
        expected_active_revision=None,
    )
    active_path = tmp_path / "state" / "shared-runtime" / "active.json"
    active_path.unlink()
    partial = tmp_path / "state" / "shared-runtime" / "active.json.tmp"
    partial.write_text("partial", encoding="utf-8")

    recovery = store.recover()

    assert recovery == {
        "active_revision": applied["revision"],
        "replayed": True,
        "removed_partial_files": [str(partial)],
    }
    assert _read_json(active_path)["revision"] == applied["revision"]
    assert not partial.exists()
    assert _audit_events(tmp_path / "state")[-1]["event_type"] == (
        "distributed_state_recovery"
    )


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _audit_events(state_dir: Path) -> list[dict[str, object]]:
    audit_path = state_dir / "shared-runtime" / "audit.jsonl"
    return [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]


def _journal_actions(state_dir: Path) -> list[str]:
    journal_path = state_dir / "shared-runtime" / "journal.jsonl"
    return [
        json.loads(line)["action"]
        for line in journal_path.read_text(encoding="utf-8").splitlines()
    ]
