from __future__ import annotations

import pytest


pytestmark = pytest.mark.unit


def test_shared_runtime_policy_defines_required_isolation_domains() -> None:
    from mcp_broker.shared_runtime_policy import (
        REQUIRED_ISOLATION_DOMAINS,
        build_shared_runtime_policy,
        validate_shared_runtime_policy,
    )

    policy = build_shared_runtime_policy()

    assert policy["schema_version"] == 1
    assert policy["hosted_execution_supported"] is False
    assert policy["default_execution_boundary"] == "local_edge"
    assert tuple(policy["isolation_domains"]) == REQUIRED_ISOLATION_DOMAINS
    assert validate_shared_runtime_policy(policy) == policy


@pytest.mark.parametrize(
    "upstream_class",
    [
        "unknown",
        "stateful",
        "browser",
        "file_access",
        "oauth",
        "local_secret",
    ],
)
def test_unknown_and_stateful_upstreams_default_to_local_only(
    upstream_class: str,
) -> None:
    from mcp_broker.shared_runtime_policy import decide_upstream_placement

    decision = decide_upstream_placement(
        upstream_class=upstream_class,
        allowlisted=False,
        requires_local_state=True,
    )

    assert decision == {
        "execution_boundary": "local_edge",
        "shared_worker_eligible": False,
        "reason": "local_state_or_unapproved_upstream_class",
    }


def test_stateless_allowlisted_upstream_is_only_shared_worker_eligible_without_local_state() -> None:
    from mcp_broker.shared_runtime_policy import decide_upstream_placement

    eligible = decide_upstream_placement(
        upstream_class="stateless",
        allowlisted=True,
        requires_local_state=False,
    )
    denied = decide_upstream_placement(
        upstream_class="stateless",
        allowlisted=True,
        requires_local_state=True,
    )

    assert eligible == {
        "execution_boundary": "shared_worker",
        "shared_worker_eligible": True,
        "reason": "allowlisted_stateless_upstream",
    }
    assert denied["execution_boundary"] == "local_edge"
    assert denied["shared_worker_eligible"] is False


def test_tenant_context_requires_tenant_workspace_and_user_ids() -> None:
    from mcp_broker.shared_runtime_policy import (
        SharedRuntimePolicyError,
        validate_tenant_context,
    )

    valid = validate_tenant_context(
        {
            "tenant_id": "tenant-acme",
            "workspace_id": "workspace-platform",
            "user_id": "user-123",
        }
    )

    assert valid == {
        "tenant_id": "tenant-acme",
        "workspace_id": "workspace-platform",
        "user_id": "user-123",
    }
    with pytest.raises(SharedRuntimePolicyError, match="tenant_id"):
        validate_tenant_context({"workspace_id": "workspace-platform", "user_id": "user-123"})
    with pytest.raises(SharedRuntimePolicyError, match="workspace_id"):
        validate_tenant_context({"tenant_id": "tenant-acme", "user_id": "user-123"})
    with pytest.raises(SharedRuntimePolicyError, match="user_id"):
        validate_tenant_context(
            {"tenant_id": "tenant-acme", "workspace_id": "workspace-platform"}
        )


def test_policy_validation_rejects_missing_isolation_domains() -> None:
    from mcp_broker.shared_runtime_policy import (
        SharedRuntimePolicyError,
        build_shared_runtime_policy,
        validate_shared_runtime_policy,
    )

    policy = build_shared_runtime_policy()
    policy["isolation_domains"] = ["tenant"]

    with pytest.raises(SharedRuntimePolicyError, match="isolation domains"):
        validate_shared_runtime_policy(policy)
