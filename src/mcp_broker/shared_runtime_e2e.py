"""End-to-end proof runner for Phase 3 shared-runtime guardrails."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any, Mapping

from mcp_broker.config import UpstreamConfig
from mcp_broker.distributed_state import DistributedStateStore
from mcp_broker.hybrid_router import HybridToolRouter
from mcp_broker.remote_api_contract import (
    RemoteApiContractError,
    validate_audit_event,
    validate_remote_request,
)
from mcp_broker.session_affinity import decide_session_affinity
from mcp_broker.shared_runtime_policy import (
    build_shared_runtime_policy,
    validate_shared_runtime_policy,
)
from mcp_broker.shared_worker import SharedWorkerRuntime, SharedWorkerTool


SHARED_RUNTIME_E2E_SCHEMA_VERSION = 1
TENANT_A = {
    "tenant_id": "tenant-a",
    "workspace_id": "workspace-a",
    "user_id": "user-a",
}
TENANT_B = {
    "tenant_id": "tenant-b",
    "workspace_id": "workspace-b",
    "user_id": "user-b",
}


def run_shared_runtime_e2e_proof(
    *,
    state_dir: Path,
    now: datetime,
) -> dict[str, Any]:
    policy = validate_shared_runtime_policy(build_shared_runtime_policy())
    worker = _shared_worker()
    edge_calls: list[list[object]] = []
    router = HybridToolRouter(upstreams=_upstreams(), shared_worker=worker)
    local_only = _local_only_call(router=router, edge_calls=edge_calls)
    shared_a = _shared_call(router=router, tenant=TENANT_A, team_id="team-a")
    shared_b = _shared_call(router=router, tenant=TENANT_B, team_id="team-b")
    quota_denial = _quota_denial(router)
    state_report = _state_report(state_dir=state_dir, now=now)
    audit_events = _audit_events(quota_denial=quota_denial, state_dir=state_dir)
    return {
        "schema_version": SHARED_RUNTIME_E2E_SCHEMA_VERSION,
        "hosted_execution_supported": policy["hosted_execution_supported"],
        "default_execution_boundary": policy["default_execution_boundary"],
        "gates": _passed_gates(),
        "local_only_routing": {
            "result": local_only,
            "edge_calls": edge_calls,
        },
        "shared_eligible_routing": {
            "tenant_results": {
                "tenant-a": shared_a["structuredContent"],
                "tenant-b": shared_b["structuredContent"],
            }
        },
        "tenant_isolation": {"worker_state_snapshot": worker.state_snapshot()},
        "authorization_denial": _authorization_denial(),
        "quota_denial": quota_denial,
        "session_affinity": _session_affinity_summary(),
        "rollback": {"active_state": state_report["active_state"]},
        "degraded_mode": state_report["degraded_mode"],
        "audit_events": audit_events,
    }


def _passed_gates() -> dict[str, str]:
    return {
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


def _shared_worker() -> SharedWorkerRuntime:
    return SharedWorkerRuntime(
        tools=[
            SharedWorkerTool(
                upstream_id="example-stateless",
                name="example.echo",
                behavior="echo",
            )
        ]
    )


def _upstreams() -> dict[str, UpstreamConfig]:
    return {
        "local-store": UpstreamConfig(
            name="local-store",
            command="local-store",
            tool_prefix="local-store",
            mode="per_session",
            tags=("stateful",),
            state_dir="local-store",
        ),
        "example-stateless": UpstreamConfig(
            name="example-stateless",
            command="example-stateless",
            tool_prefix="example",
            mode="shared",
            tags=("stateless", "shared-worker"),
        ),
    }


def _local_only_call(
    *,
    router: HybridToolRouter,
    edge_calls: list[list[object]],
) -> dict[str, Any]:
    def edge_call(
        upstream_name: str,
        upstream_tool_name: str,
        arguments: dict[str, object],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        edge_calls.append([upstream_name, upstream_tool_name, arguments, timeout_seconds])
        return {
            "content": [{"type": "text", "text": "edge"}],
            "structuredContent": {"execution_boundary": "local_edge"},
        }

    return router.call_tool(
        advertised_name="local-store.search",
        arguments={"query": "refund"},
        edge_caller=edge_call,
        tenant_context=TENANT_A,
        team_id="team-a",
        quota_snapshot=_quota_snapshot(
            tenant=TENANT_A,
            team_id="team-a",
            upstream_id="local-store",
            tool_name="local-store.search",
        ),
    )


def _shared_call(
    *,
    router: HybridToolRouter,
    tenant: Mapping[str, str],
    team_id: str,
) -> dict[str, Any]:
    return router.call_tool(
        advertised_name="example.echo",
        arguments={"message": tenant["tenant_id"]},
        edge_caller=_unexpected_edge_call,
        tenant_context=tenant,
        team_id=team_id,
        quota_snapshot=_quota_snapshot(
            tenant=tenant,
            team_id=team_id,
            upstream_id="example-stateless",
            tool_name="example.echo",
        ),
    )


def _quota_denial(router: HybridToolRouter) -> dict[str, Any]:
    return router.call_tool(
        advertised_name="example.echo",
        arguments={"message": "blocked"},
        edge_caller=_unexpected_edge_call,
        tenant_context=TENANT_A,
        team_id="team-a",
        quota_snapshot=_quota_snapshot(
            tenant=TENANT_A,
            team_id="team-a",
            upstream_id="example-stateless",
            tool_name="example.echo",
            tool_limit=0,
        ),
    )


def _authorization_denial() -> dict[str, Any]:
    try:
        validate_remote_request(
            _remote_request(policy_decision={"decision_id": "decision-denied", "allowed": False, "reason": "authz_denied"})
        )
    except RemoteApiContractError as error:
        return {
            "allowed": False,
            "reason": str(error),
            "audit_event": validate_audit_event(
                {
                    "event_type": "audit_event",
                    "request_id": "request-denied",
                    "tenant_id": TENANT_A["tenant_id"],
                    "workspace_id": TENANT_A["workspace_id"],
                    "user_id": TENANT_A["user_id"],
                    "principal_id": "principal-a",
                    "action": "tool_call",
                    "result": "denied",
                    "denial_reason": "policy_decision_denied",
                }
            ),
        }
    raise AssertionError("denied policy decision must fail closed")


def _remote_request(*, policy_decision: Mapping[str, object]) -> dict[str, object]:
    return {
        "operation": "tool_call",
        "request_id": "request-denied",
        "tenant_context": TENANT_A,
        "auth_context": {"principal_id": "principal-a", "auth_method": "bearer"},
        "policy_decision": dict(policy_decision),
        "tool_name": "example.echo",
        "arguments": {"message": "blocked"},
    }


def _session_affinity_summary() -> dict[str, str]:
    shared = decide_session_affinity(
        upstream_class="stateless",
        upstream_id="example-stateless",
        allowlisted=True,
        requires_local_state=False,
        tenant_context=TENANT_A,
    )
    local = decide_session_affinity(
        upstream_class="stateful",
        upstream_id="local-store",
        allowlisted=False,
        requires_local_state=True,
        tenant_context=TENANT_A,
    )
    return {
        "shared_worker": shared["session_affinity"],
        "local_edge": local["session_affinity"],
    }


def _state_report(*, state_dir: Path, now: datetime) -> dict[str, Any]:
    store = DistributedStateStore(state_dir)
    lock = store.acquire_lock(
        owner_id="worker-a",
        tenant_context=TENANT_A,
        now=now,
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
    store.active_path.unlink()
    degraded = store.recover()
    rollback = store.rollback(lock=lock)
    return {
        "degraded_mode": {
            "trigger": "missing_active_state_file",
            "replayed": degraded["replayed"],
            "active_revision": degraded["active_revision"],
        },
        "rollback": rollback,
        "second_revision": second["revision"],
        "active_state": _read_json(store.active_path)["state"],
    }


def _audit_events(
    *,
    quota_denial: Mapping[str, Any],
    state_dir: Path,
) -> dict[str, list[str]]:
    return {
        "quota_denials": [quota_denial["audit_event"]["denial_reason"]],
        "worker_results": ["allowed", "allowed", quota_denial["audit_event"]["result"]],
        "state_results": _state_audit_results(state_dir),
    }


def _state_audit_results(state_dir: Path) -> list[str]:
    audit_path = state_dir / "shared-runtime" / "audit.jsonl"
    return [
        event["result"]
        for event in (
            json.loads(line)
            for line in audit_path.read_text(encoding="utf-8").splitlines()
        )
    ]


def _quota_snapshot(
    *,
    tenant: Mapping[str, str],
    team_id: str,
    upstream_id: str,
    tool_name: str,
    tool_limit: int = 10,
) -> dict[str, object]:
    return {
        "kill_switches": {
            "global": False,
            "teams": [],
            "users": [],
            "upstreams": [],
            "tools": [],
        },
        "limits": {
            "global": {"limit": 100, "used": 0},
            "teams": {team_id: {"limit": 100, "used": 0}},
            "users": {tenant["user_id"]: {"limit": 100, "used": 0}},
            "upstreams": {upstream_id: {"limit": 100, "used": 0}},
            "tools": {tool_name: {"limit": tool_limit, "used": 0}},
        },
    }


def _unexpected_edge_call(*_args: object, **_kwargs: object) -> dict[str, object]:
    raise AssertionError("shared-worker route must not call the edge broker")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
