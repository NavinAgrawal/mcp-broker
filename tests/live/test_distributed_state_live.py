from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path

import pytest


pytestmark = pytest.mark.live


def test_distributed_state_live_filesystem_recovery_flow(tmp_path: Path) -> None:
    from mcp_broker.distributed_state import DistributedStateStore

    state_dir = tmp_path / "runtime" / "state"
    store = DistributedStateStore(state_dir)
    lock = store.acquire_lock(
        owner_id="live-worker",
        tenant_context={
            "tenant_id": "tenant-live",
            "workspace_id": "workspace-live",
            "user_id": "user-live",
        },
        now=datetime(2026, 7, 4, 8, 0, tzinfo=UTC),
        ttl_seconds=60,
    )
    applied = store.apply_state(
        lock=lock,
        state={"deployment_id": "live-deploy", "bundle_version": "1.0.0"},
        expected_active_revision=None,
    )

    active_path = state_dir / "shared-runtime" / "active.json"
    active_path.unlink()
    recovery = store.recover()

    assert recovery == {
        "active_revision": applied["revision"],
        "replayed": True,
        "removed_partial_files": [],
    }
    assert json.loads(active_path.read_text(encoding="utf-8"))["state"] == {
        "bundle_version": "1.0.0",
        "deployment_id": "live-deploy",
    }
    assert (state_dir / "shared-runtime" / "journal.jsonl").is_file()
    assert (state_dir / "shared-runtime" / "audit.jsonl").is_file()
