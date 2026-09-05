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

    assert policy == {
        "schema_version": 1,
        "hosted_execution_supported": False,
        "default_execution_boundary": "local_edge",
        "isolation_domains": list(REQUIRED_ISOLATION_DOMAINS),
        "tenant_context_required": ["tenant_id", "workspace_id", "user_id"],
        "upstream_defaults": {
            "unknown": "local_edge",
            "stateful": "local_edge",
            "stateless": "local_edge",
        },
    }
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
        upstream_class="  STATELESS  ",
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

    with pytest.raises(SharedRuntimePolicyError) as exc_info:
        validate_shared_runtime_policy(policy)
    assert str(exc_info.value) == "shared runtime isolation domains are incomplete"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "schema_version",
            2,
            "shared runtime policy schema_version is invalid",
        ),
        (
            "hosted_execution_supported",
            True,
            "hosted execution must remain unsupported",
        ),
        (
            "default_execution_boundary",
            "shared_worker",
            "default execution boundary must be local_edge",
        ),
    ],
)
def test_policy_validation_rejects_unsafe_policy_fields(
    field: str,
    value: object,
    message: str,
) -> None:
    from mcp_broker.shared_runtime_policy import (
        SharedRuntimePolicyError,
        build_shared_runtime_policy,
        validate_shared_runtime_policy,
    )

    policy = build_shared_runtime_policy()
    policy[field] = value

    with pytest.raises(SharedRuntimePolicyError) as exc_info:
        validate_shared_runtime_policy(policy)
    assert str(exc_info.value) == message


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tenant_id", "tenant/a"),
        ("workspace_id", r"workspace\\a"),
        ("user_id", "user/a"),
    ],
)
def test_tenant_context_rejects_path_separator_identifiers(field: str, value: str) -> None:
    from mcp_broker.shared_runtime_policy import (
        SharedRuntimePolicyError,
        validate_tenant_context,
    )

    context = {
        "tenant_id": "tenant-a",
        "workspace_id": "workspace-a",
        "user_id": "user-a",
    }
    context[field] = value

    with pytest.raises(SharedRuntimePolicyError, match="path separators"):
        validate_tenant_context(context)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tenant_id", ""),
        ("workspace_id", " "),
        ("user_id", None),
    ],
)
def test_tenant_context_rejects_missing_or_blank_identifiers(field: str, value: object) -> None:
    from mcp_broker.shared_runtime_policy import (
        SharedRuntimePolicyError,
        validate_tenant_context,
    )

    context = {
        "tenant_id": "tenant-a",
        "workspace_id": "workspace-a",
        "user_id": "user-a",
    }
    context[field] = value

    with pytest.raises(SharedRuntimePolicyError, match=f"{field} is required"):
        validate_tenant_context(context)
