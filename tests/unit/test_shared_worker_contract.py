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


def test_echo_tool_coerces_non_string_message_to_empty_string() -> None:
    decision = _runtime().call_tool(
        tenant_context=TENANT_CONTEXT,
        upstream_id="example-stateless",
        upstream_class="stateless",
        allowlisted=True,
        requires_local_state=False,
        tool_name="example.echo",
        arguments={"message": 123},
        quota_decision=ALLOWED_QUOTA,
    )

    assert decision["result"] == {
        "content": [{"type": "text", "text": ""}],
        "structuredContent": {"message": ""},
    }


def test_echo_tool_defaults_a_missing_message_to_empty_string() -> None:
    decision = _runtime().call_tool(
        tenant_context=TENANT_CONTEXT,
        upstream_id="example-stateless",
        upstream_class="stateless",
        allowlisted=True,
        requires_local_state=False,
        tool_name="example.echo",
        arguments={},
        quota_decision=ALLOWED_QUOTA,
    )

    assert decision["result"] == {
        "content": [{"type": "text", "text": ""}],
        "structuredContent": {"message": ""},
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
    assert decision["result"] == {
        "content": [{"type": "text", "text": "sandbox"}],
        "structuredContent": {
            "environment": {},
            "secret_names": [],
            "file_roots": [],
            "network_allowed": False,
            "local_state_allowed": False,
        },
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
    assert decision["audit_event"]["tool_name"] == "example.echo"
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
    assert decision["worker_scope"] == {
        "tenant_id": "tenant-a",
        "workspace_id": "workspace-a",
        "user_id": "user-a",
        "upstream_id": "example-stateless",
    }
    assert decision["audit_event"]["tool_name"] == "example.missing"
    assert decision["audit_event"]["denial_reason"] == "user_quota_exceeded"


def test_worker_quota_rejection_without_reason_uses_default_audit_reason() -> None:
    decision = _runtime().call_tool(
        tenant_context=TENANT_CONTEXT,
        upstream_id="example-stateless",
        upstream_class="stateless",
        allowlisted=True,
        requires_local_state=False,
        tool_name="example.echo",
        arguments={},
        quota_decision={"allowed": False},
    )

    assert decision["allowed"] is False
    assert decision["reason"] == "quota_denied"
    assert decision["audit_event"]["denial_reason"] == "quota_denied"


def test_worker_quota_rejection_ignores_non_string_reason() -> None:
    decision = _runtime().call_tool(
        tenant_context=TENANT_CONTEXT,
        upstream_id="example-stateless",
        upstream_class="stateless",
        allowlisted=True,
        requires_local_state=False,
        tool_name="example.echo",
        arguments={},
        quota_decision={"allowed": False, "reason": 7},
    )

    assert decision["reason"] == "quota_denied"
    assert decision["audit_event"]["denial_reason"] == "quota_denied"


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


def test_worker_placement_denial_wraps_session_affinity_errors() -> None:
    from mcp_broker.shared_worker import SharedWorkerError, _placement_denial

    with pytest.raises(SharedWorkerError, match="tenant_id is required"):
        _placement_denial(
            upstream_class="stateless",
            upstream_id="example-stateless",
            allowlisted=True,
            requires_local_state=False,
            tenant_context={"workspace_id": "workspace-a", "user_id": "user-a"},
        )


def test_worker_rejects_invalid_upstream_and_tool_identifiers() -> None:
    from mcp_broker.shared_worker import SharedWorkerError

    with pytest.raises(SharedWorkerError) as upstream_exc:
        _runtime().call_tool(
            tenant_context=TENANT_CONTEXT,
            upstream_id="example/stateless",
            upstream_class="stateless",
            allowlisted=True,
            requires_local_state=False,
            tool_name="example.echo",
            arguments={},
            quota_decision=ALLOWED_QUOTA,
        )
    assert str(upstream_exc.value) == "upstream_id must not contain path separators"

    with pytest.raises(SharedWorkerError) as tool_exc:
        _runtime().call_tool(
            tenant_context=TENANT_CONTEXT,
            upstream_id="example-stateless",
            upstream_class="stateless",
            allowlisted=True,
            requires_local_state=False,
            tool_name="echo",
            arguments={},
            quota_decision=ALLOWED_QUOTA,
        )
    assert str(tool_exc.value) == "tool_name must include upstream prefix"


def test_worker_constructor_rejects_unsafe_tool_definitions() -> None:
    from mcp_broker.shared_worker import SharedWorkerError, SharedWorkerRuntime, SharedWorkerTool

    with pytest.raises(SharedWorkerError) as exc_info:
        SharedWorkerRuntime(
            tools=[
                SharedWorkerTool(
                    upstream_id="example-stateless",
                    name="example.bad",
                    behavior="bad",
                )
            ]
        )
    assert str(exc_info.value) == "unsupported shared worker tool behavior"


def test_worker_execute_helper_rejects_unsupported_behavior_if_validation_is_bypassed() -> None:
    from mcp_broker.shared_worker import (
        SharedWorkerError,
        SharedWorkerTool,
        _WorkerSandbox,
        _execute_fake_tool,
    )

    with pytest.raises(SharedWorkerError) as exc_info:
        _execute_fake_tool(
            tool=SharedWorkerTool(
                upstream_id="example-stateless",
                name="example.bad",
                behavior="bad",
            ),
            arguments={},
            sandbox=_WorkerSandbox.default_deny(),
        )
    assert str(exc_info.value) == "unsupported shared worker tool behavior"


def test_worker_rejects_empty_upstream_id_and_path_separator_tool_definitions() -> None:
    from mcp_broker.shared_worker import SharedWorkerError, SharedWorkerRuntime, SharedWorkerTool

    with pytest.raises(SharedWorkerError, match="upstream_id is required"):
        SharedWorkerRuntime(
            tools=[
                SharedWorkerTool(
                    upstream_id="",
                    name="example.echo",
                    behavior="echo",
                )
            ]
        )
    with pytest.raises(SharedWorkerError, match="tool_name must not contain path separators"):
        SharedWorkerRuntime(
            tools=[
                SharedWorkerTool(
                    upstream_id="example-stateless",
                    name="example/echo",
                    behavior="echo",
                )
            ]
        )


def test_worker_rejects_backslash_in_tool_definition() -> None:
    from mcp_broker.shared_worker import SharedWorkerError, SharedWorkerRuntime, SharedWorkerTool

    with pytest.raises(SharedWorkerError) as exc_info:
        SharedWorkerRuntime(
            tools=[
                SharedWorkerTool(
                    upstream_id="example-stateless",
                    name=r"example\echo",
                    behavior="echo",
                )
            ]
        )
    assert str(exc_info.value) == "tool_name must not contain path separators"


def test_worker_state_snapshot_is_empty_for_fake_runtime() -> None:
    assert _runtime().state_snapshot() == {}
