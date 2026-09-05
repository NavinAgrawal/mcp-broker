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


def test_distributed_state_parse_utc_normalizes_z_and_offsets() -> None:
    from mcp_broker.distributed_state import _parse_utc

    assert _parse_utc("2026-07-04T06:00:00Z") == NOW
    assert _parse_utc("2026-07-04T02:00:00-04:00") == NOW
    assert _parse_utc("2026-07-04T06:00:00Z").tzinfo is UTC


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


@pytest.mark.parametrize(
    ("owner_id", "ttl_seconds", "match"),
    [
        ("", 60, "owner_id is required"),
        ("worker/a", 60, "path separators"),
        ("worker-a", 0, "ttl_seconds"),
    ],
)
def test_distributed_state_store_rejects_invalid_lock_inputs(
    tmp_path: Path,
    owner_id: str,
    ttl_seconds: int,
    match: str,
) -> None:
    from mcp_broker.distributed_state import DistributedStateError, DistributedStateStore

    with pytest.raises(DistributedStateError, match=match):
        DistributedStateStore(tmp_path / "state").acquire_lock(
            owner_id=owner_id,
            tenant_context=TENANT_CONTEXT,
            now=NOW,
            ttl_seconds=ttl_seconds,
        )


def test_distributed_state_store_rejects_naive_lock_timestamp(tmp_path: Path) -> None:
    from mcp_broker.distributed_state import DistributedStateError, DistributedStateStore

    with pytest.raises(DistributedStateError, match="timezone-aware"):
        DistributedStateStore(tmp_path / "state").acquire_lock(
            owner_id="worker-a",
            tenant_context=TENANT_CONTEXT,
            now=datetime(2026, 7, 4, 6, 0),
            ttl_seconds=60,
        )


def test_distributed_state_store_rejects_invalid_tenant_context(tmp_path: Path) -> None:
    from mcp_broker.distributed_state import DistributedStateError, DistributedStateStore

    with pytest.raises(DistributedStateError, match="tenant_id"):
        DistributedStateStore(tmp_path / "state").acquire_lock(
            owner_id="worker-a",
            tenant_context={**TENANT_CONTEXT, "tenant_id": ""},
            now=NOW,
            ttl_seconds=60,
        )


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


def test_distributed_state_store_allocates_revision_from_journal_when_active_is_missing(
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
        state={"deployment_id": "deploy-a"},
        expected_active_revision=None,
    )
    (tmp_path / "state" / "shared-runtime" / "active.json").unlink()

    second = store.apply_state(
        lock=lock,
        state={"deployment_id": "deploy-b"},
        expected_active_revision=None,
    )

    assert first["revision"] == 1
    assert second["revision"] == 2


def test_distributed_state_store_rejects_mutation_with_missing_lock(tmp_path: Path) -> None:
    from mcp_broker.distributed_state import DistributedStateError, DistributedStateStore

    with pytest.raises(DistributedStateError, match="state lock is missing"):
        DistributedStateStore(tmp_path / "state").apply_state(
            lock={"owner_id": "worker-a", "token": "000000000001"},
            state={"deployment_id": "deploy-a"},
            expected_active_revision=None,
        )


def test_distributed_state_store_rejects_mutation_with_mismatched_lock(
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

    with pytest.raises(DistributedStateConflict, match="matching lock token"):
        store.apply_state(
            lock={
                "owner_id": "worker-b",
                "token": "000000000999",
                **TENANT_CONTEXT,
            },
            state={"deployment_id": "deploy-a"},
            expected_active_revision=None,
        )

    assert _audit_events(tmp_path / "state")[-1]["denial_reason"] == "lock_token_mismatch"


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


def test_distributed_state_store_rejects_rollback_without_active_state(
    tmp_path: Path,
) -> None:
    from mcp_broker.distributed_state import DistributedStateError, DistributedStateStore

    store = DistributedStateStore(tmp_path / "state")
    lock = store.acquire_lock(
        owner_id="worker-a",
        tenant_context=TENANT_CONTEXT,
        now=NOW,
        ttl_seconds=60,
    )

    with pytest.raises(DistributedStateError, match="active shared-runtime state"):
        store.rollback(lock=lock)


def test_distributed_state_store_rejects_rollback_without_previous_state(
    tmp_path: Path,
) -> None:
    from mcp_broker.distributed_state import DistributedStateError, DistributedStateStore

    store = DistributedStateStore(tmp_path / "state")
    lock = store.acquire_lock(
        owner_id="worker-a",
        tenant_context=TENANT_CONTEXT,
        now=NOW,
        ttl_seconds=60,
    )
    store.apply_state(
        lock=lock,
        state={"deployment_id": "deploy-a"},
        expected_active_revision=None,
    )

    with pytest.raises(DistributedStateError, match="previous shared-runtime state"):
        store.rollback(lock=lock)


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


def test_distributed_state_store_replay_rejects_apply_entry_without_record(
    tmp_path: Path,
) -> None:
    from mcp_broker.distributed_state import DistributedStateError, DistributedStateStore

    root = tmp_path / "state" / "shared-runtime"
    root.mkdir(parents=True)
    (root / "journal.jsonl").write_text('{"action":"apply"}\n', encoding="utf-8")

    with pytest.raises(DistributedStateError, match="apply journal entry is missing record"):
        DistributedStateStore(tmp_path / "state").recover()


def test_distributed_state_store_replay_rejects_incomplete_rollback_entry(
    tmp_path: Path,
) -> None:
    from mcp_broker.distributed_state import DistributedStateError, DistributedStateStore

    root = tmp_path / "state" / "shared-runtime"
    root.mkdir(parents=True)
    (root / "journal.jsonl").write_text(
        json.dumps(
            {
                "action": "rollback",
                "active_record": {"revision": 1, "state": {"deployment_id": "deploy-a"}},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(DistributedStateError, match="rollback journal entry is incomplete"):
        DistributedStateStore(tmp_path / "state").recover()


def test_distributed_state_store_replays_valid_rollback_journal_entry(
    tmp_path: Path,
) -> None:
    from mcp_broker.distributed_state import DistributedStateStore

    root = tmp_path / "state" / "shared-runtime"
    root.mkdir(parents=True)
    first = {"revision": 1, "status": "active", "state": {"deployment_id": "deploy-a"}}
    second = {"revision": 2, "status": "active", "state": {"deployment_id": "deploy-b"}}
    root.joinpath("journal.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"action": "apply", "record": first}),
                json.dumps({"action": "apply", "record": second}),
                json.dumps(
                    {
                        "action": "rollback",
                        "active_record": first,
                        "previous_record": second,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    recovery = DistributedStateStore(tmp_path / "state").recover()

    assert recovery["active_revision"] == 1
    assert _read_json(root / "active.json") == first
    assert _read_json(root / "previous.json") == second


def test_distributed_state_store_ignores_unknown_journal_actions_during_replay(
    tmp_path: Path,
) -> None:
    from mcp_broker.distributed_state import DistributedStateStore

    root = tmp_path / "state" / "shared-runtime"
    root.mkdir(parents=True)
    active = {"revision": 1, "status": "active", "state": {"deployment_id": "deploy-a"}}
    root.joinpath("journal.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"action": "noop", "record": {"revision": 99}}),
                json.dumps({"action": "apply", "record": active}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    recovery = DistributedStateStore(tmp_path / "state").recover()

    assert recovery["active_revision"] == 1
    assert _read_json(root / "active.json") == active


def test_distributed_state_store_recovery_checks_existing_active_state(
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
        state={"deployment_id": "deploy-a"},
        expected_active_revision=None,
    )
    partial = tmp_path / "state" / "shared-runtime" / "active.json.tmp"
    partial.write_text("partial", encoding="utf-8")

    recovery = store.recover()

    assert recovery == {
        "active_revision": applied["revision"],
        "replayed": False,
        "removed_partial_files": [str(partial)],
    }
    assert not partial.exists()
    assert _audit_events(tmp_path / "state")[-1]["result"] == "checked"


def test_distributed_state_store_recovery_requires_journal_entries(tmp_path: Path) -> None:
    from mcp_broker.distributed_state import DistributedStateError, DistributedStateStore

    with pytest.raises(DistributedStateError, match="no shared-runtime journal"):
        DistributedStateStore(tmp_path / "state").recover()


def test_distributed_state_store_recovery_rejects_malformed_apply_journal(
    tmp_path: Path,
) -> None:
    from mcp_broker.distributed_state import DistributedStateError, DistributedStateStore

    journal = tmp_path / "state" / "shared-runtime" / "journal.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({"action": "apply"}) + "\n", encoding="utf-8")

    with pytest.raises(DistributedStateError, match="apply journal entry"):
        DistributedStateStore(tmp_path / "state").recover()


def test_distributed_state_store_recovery_rejects_malformed_rollback_journal(
    tmp_path: Path,
) -> None:
    from mcp_broker.distributed_state import DistributedStateError, DistributedStateStore

    journal = tmp_path / "state" / "shared-runtime" / "journal.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({"action": "rollback"}) + "\n", encoding="utf-8")

    with pytest.raises(DistributedStateError, match="rollback journal entry"):
        DistributedStateStore(tmp_path / "state").recover()


def test_distributed_state_store_recovery_rejects_non_object_journal_line(
    tmp_path: Path,
) -> None:
    from mcp_broker.distributed_state import DistributedStateError, DistributedStateStore

    journal = tmp_path / "state" / "shared-runtime" / "journal.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_text("[]\n", encoding="utf-8")

    with pytest.raises(DistributedStateError, match="expected JSON object"):
        DistributedStateStore(tmp_path / "state").recover()


def test_distributed_state_store_rejects_non_object_active_file(tmp_path: Path) -> None:
    from mcp_broker.distributed_state import DistributedStateError, DistributedStateStore

    active_path = tmp_path / "state" / "shared-runtime" / "active.json"
    active_path.parent.mkdir(parents=True)
    active_path.write_text("[]", encoding="utf-8")

    with pytest.raises(DistributedStateError, match="expected JSON object"):
        DistributedStateStore(tmp_path / "state").recover()


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
