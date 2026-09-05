from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest


pytestmark = pytest.mark.e2e


# REAL_INFRA: this E2E exercises the real local filesystem state adapter and
# composes the real P3 routing, worker, quota, affinity, audit, and rollback code.
def test_shared_runtime_e2e_proof_covers_all_phase_3_gates(tmp_path: Path) -> None:
    from mcp_broker.shared_runtime_e2e import run_shared_runtime_e2e_proof

    report = run_shared_runtime_e2e_proof(
        state_dir=tmp_path / "state",
        now=datetime(2026, 7, 4, 8, 0, tzinfo=UTC),
    )

    assert report["schema_version"] == 1
    assert report["hosted_execution_supported"] is False
    assert report["default_execution_boundary"] == "local_edge"
    assert report["gates"] == {
        "tenant_isolation": "passed",
        "authorization_denial": "passed",
        "quota_denial": "passed",
        "session_affinity": "passed",
        "audit_events": "passed",
        "rollback": "passed",
        "degraded_mode": "passed",
        "local_only_routing": "passed",
        "shared_eligible_routing": "passed",
    }
    assert report["local_only_routing"]["edge_calls"] == [
        ["local-store", "search", {"query": "refund"}, 60]
    ]
    assert report["shared_eligible_routing"]["tenant_results"] == {
        "tenant-a": {"message": "tenant-a"},
        "tenant-b": {"message": "tenant-b"},
    }
    assert report["tenant_isolation"]["worker_state_snapshot"] == {}
    assert report["authorization_denial"] == {
        "allowed": False,
        "reason": "policy decision denied",
        "audit_event": {
            "event_type": "audit_event",
            "request_id": "request-denied",
            "tenant_id": "tenant-a",
            "workspace_id": "workspace-a",
            "user_id": "user-a",
            "principal_id": "principal-a",
            "action": "tool_call",
            "result": "denied",
            "denial_reason": "policy_decision_denied",
        },
    }
    assert report["quota_denial"]["allowed"] is False
    assert report["quota_denial"]["reason"] == "quota_denied"
    assert report["quota_denial"]["audit_event"]["denial_reason"] == "tool_quota_exceeded"
    assert report["session_affinity"] == {
        "shared_worker": "tenant_workspace_user",
        "local_edge": "local_client_session",
    }
    assert report["rollback"]["active_state"] == {
        "bundle_version": "1.0.0",
        "deployment_id": "deploy-a",
    }
    assert report["degraded_mode"] == {
        "trigger": "missing_active_state_file",
        "replayed": True,
        "active_revision": 2,
    }
    assert report["audit_events"] == {
        "quota_denials": ["tool_quota_exceeded"],
        "worker_results": ["allowed", "allowed", "denied"],
        "state_results": [
            "acquired",
            "allowed",
            "allowed",
            "replayed",
            "allowed",
        ],
    }


@pytest.mark.error_simulation
def test_shared_runtime_authorization_denial_requires_fail_closed_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_broker.shared_runtime_e2e as shared_runtime_e2e

    monkeypatch.setattr(shared_runtime_e2e, "validate_remote_request", lambda _request: {})

    with pytest.raises(AssertionError, match="denied policy decision must fail closed"):
        shared_runtime_e2e._authorization_denial()


def test_shared_runtime_unexpected_edge_call_fails_shared_worker_routes() -> None:
    from mcp_broker.shared_runtime_e2e import _unexpected_edge_call

    with pytest.raises(AssertionError, match="shared-worker route must not call the edge broker"):
        _unexpected_edge_call()
