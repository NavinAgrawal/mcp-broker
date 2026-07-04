import os

import pytest


pytestmark = pytest.mark.unit


TENANT_CONTEXT = {
    "tenant_id": "tenant-a",
    "workspace_id": "workspace-a",
    "user_id": "user-a",
}

ALLOWED_QUOTA = {"allowed": True, "reason": "quota_allowed"}


def _runtime():
    from mcp_broker.shared_worker import SharedWorkerRuntime, SharedWorkerTool

    return SharedWorkerRuntime(
        tools=[
            SharedWorkerTool(
                upstream_id="example-stateless",
                name="example.echo",
                behavior="echo",
            ),
            SharedWorkerTool(
                upstream_id="example-stateless",
                name="example.inspect_sandbox",
                behavior="inspect_sandbox",
            ),
        ]
    )


def test_shared_worker_policy_declares_default_deny_isolation() -> None:
    from mcp_broker.shared_worker import build_shared_worker_policy

    policy = build_shared_worker_policy()

    assert policy["schema_version"] == 1
    assert policy["runtime_kind"] == "in_process_fake_worker"
    assert policy["real_upstream_routing_supported"] is False
    assert policy["eligible_upstream_classes"] == ["stateless"]
    assert policy["required_placement"] == "shared_worker"
    assert policy["default_capabilities"] == {
        "network": "deny",
        "file_access": "deny",
        "secrets": "deny",
        "local_state": "deny",
        "inherited_environment": "deny",
    }
    assert policy["denial_audit_required"] is True


def test_allowlisted_stateless_fake_tool_runs_with_audited_tenant_scope() -> None:
    decision = _runtime().call_tool(
        tenant_context=TENANT_CONTEXT,
        upstream_id="example-stateless",
        upstream_class="stateless",
        allowlisted=True,
        requires_local_state=False,
        tool_name="example.echo",
        arguments={"message": "hello"},
        quota_decision=ALLOWED_QUOTA,
    )

    assert decision["allowed"] is True
    assert decision["reason"] == "worker_call_allowed"
    assert decision["result"] == {
        "content": [{"type": "text", "text": "hello"}],
        "structuredContent": {"message": "hello"},
    }
    assert decision["worker_scope"] == {
        "tenant_id": "tenant-a",
        "workspace_id": "workspace-a",
        "user_id": "user-a",
        "upstream_id": "example-stateless",
    }
    assert decision["audit_event"] == {
        "event_type": "shared_worker_call",
        "tenant_id": "tenant-a",
        "workspace_id": "workspace-a",
        "user_id": "user-a",
        "upstream_id": "example-stateless",
        "tool_name": "example.echo",
        "result": "allowed",
    }


def test_worker_sandbox_exposes_no_inherited_environment_secrets_or_file_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_BROKER_TEST_SECRET", "must-not-leak")

    decision = _runtime().call_tool(
        tenant_context=TENANT_CONTEXT,
        upstream_id="example-stateless",
        upstream_class="stateless",
        allowlisted=True,
        requires_local_state=False,
        tool_name="example.inspect_sandbox",
        arguments={},
        quota_decision=ALLOWED_QUOTA,
    )

    assert os.environ["MCP_BROKER_TEST_SECRET"] == "must-not-leak"
    assert decision["allowed"] is True
    assert decision["result"]["structuredContent"] == {
        "environment": {},
        "secret_names": [],
        "file_roots": [],
        "network_allowed": False,
        "local_state_allowed": False,
    }


@pytest.mark.parametrize(
    ("upstream_class", "allowlisted", "requires_local_state", "expected_reason"),
    [
        ("stateful", True, False, "shared_worker_placement_denied"),
        ("stateless", False, False, "shared_worker_placement_denied"),
        ("stateless", True, True, "shared_worker_placement_denied"),
    ],
)
def test_worker_denies_non_eligible_upstream_placement_with_audit_event(
    upstream_class: str,
    allowlisted: bool,
    requires_local_state: bool,
    expected_reason: str,
) -> None:
    decision = _runtime().call_tool(
        tenant_context=TENANT_CONTEXT,
        upstream_id="example-stateless",
        upstream_class=upstream_class,
        allowlisted=allowlisted,
        requires_local_state=requires_local_state,
        tool_name="example.echo",
        arguments={"message": "blocked"},
        quota_decision=ALLOWED_QUOTA,
    )

    assert decision["allowed"] is False
    assert decision["reason"] == expected_reason
    assert decision["result"] is None
    assert decision["audit_event"]["result"] == "denied"
    assert decision["audit_event"]["denial_reason"] == expected_reason


def test_worker_denies_quota_rejection_before_tool_lookup() -> None:
    decision = _runtime().call_tool(
        tenant_context=TENANT_CONTEXT,
        upstream_id="example-stateless",
        upstream_class="stateless",
        allowlisted=True,
        requires_local_state=False,
        tool_name="example.missing",
        arguments={},
        quota_decision={"allowed": False, "reason": "user_quota_exceeded"},
    )

    assert decision["allowed"] is False
    assert decision["reason"] == "quota_denied"
    assert decision["audit_event"]["denial_reason"] == "user_quota_exceeded"


def test_worker_denies_unsupported_tool_with_audit_event() -> None:
    decision = _runtime().call_tool(
        tenant_context=TENANT_CONTEXT,
        upstream_id="example-stateless",
        upstream_class="stateless",
        allowlisted=True,
        requires_local_state=False,
        tool_name="example.missing",
        arguments={},
        quota_decision=ALLOWED_QUOTA,
    )

    assert decision["allowed"] is False
    assert decision["reason"] == "unsupported_tool"
    assert decision["audit_event"] == {
        "event_type": "shared_worker_call",
        "tenant_id": "tenant-a",
        "workspace_id": "workspace-a",
        "user_id": "user-a",
        "upstream_id": "example-stateless",
        "tool_name": "example.missing",
        "result": "denied",
        "denial_reason": "unsupported_tool",
    }


def test_worker_rejects_missing_tenant_context() -> None:
    from mcp_broker.shared_worker import SharedWorkerError

    with pytest.raises(SharedWorkerError, match="tenant_id is required"):
        _runtime().call_tool(
            tenant_context={
                "workspace_id": "workspace-a",
                "user_id": "user-a",
            },
            upstream_id="example-stateless",
            upstream_class="stateless",
            allowlisted=True,
            requires_local_state=False,
            tool_name="example.echo",
            arguments={},
            quota_decision=ALLOWED_QUOTA,
        )
