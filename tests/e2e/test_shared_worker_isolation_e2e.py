import os

import pytest


pytestmark = pytest.mark.e2e


def test_shared_worker_keeps_tenant_scopes_separate_without_runtime_state() -> None:
    from mcp_broker.shared_worker import SharedWorkerRuntime, SharedWorkerTool

    runtime = SharedWorkerRuntime(
        tools=[
            SharedWorkerTool(
                upstream_id="example-stateless",
                name="example.echo",
                behavior="echo",
            )
        ]
    )

    first = runtime.call_tool(
        tenant_context={
            "tenant_id": "tenant-a",
            "workspace_id": "workspace-a",
            "user_id": "user-a",
        },
        upstream_id="example-stateless",
        upstream_class="stateless",
        allowlisted=True,
        requires_local_state=False,
        tool_name="example.echo",
        arguments={"message": "tenant-a"},
        quota_decision={"allowed": True, "reason": "quota_allowed"},
    )
    second = runtime.call_tool(
        tenant_context={
            "tenant_id": "tenant-b",
            "workspace_id": "workspace-b",
            "user_id": "user-b",
        },
        upstream_id="example-stateless",
        upstream_class="stateless",
        allowlisted=True,
        requires_local_state=False,
        tool_name="example.echo",
        arguments={"message": "tenant-b"},
        quota_decision={"allowed": True, "reason": "quota_allowed"},
    )

    assert first["allowed"] is True
    assert second["allowed"] is True
    assert first["worker_scope"] == {
        "tenant_id": "tenant-a",
        "workspace_id": "workspace-a",
        "user_id": "user-a",
        "upstream_id": "example-stateless",
    }
    assert second["worker_scope"] == {
        "tenant_id": "tenant-b",
        "workspace_id": "workspace-b",
        "user_id": "user-b",
        "upstream_id": "example-stateless",
    }
    assert runtime.state_snapshot() == {}


def test_shared_worker_denies_local_secret_file_and_network_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker.shared_worker import SharedWorkerRuntime, SharedWorkerTool

    monkeypatch.setenv("MCP_BROKER_TEST_SECRET", "must-not-leak")
    runtime = SharedWorkerRuntime(
        tools=[
            SharedWorkerTool(
                upstream_id="example-stateless",
                name="example.inspect_sandbox",
                behavior="inspect_sandbox",
            )
        ]
    )

    decision = runtime.call_tool(
        tenant_context={
            "tenant_id": "tenant-a",
            "workspace_id": "workspace-a",
            "user_id": "user-a",
        },
        upstream_id="example-stateless",
        upstream_class="stateless",
        allowlisted=True,
        requires_local_state=False,
        tool_name="example.inspect_sandbox",
        arguments={},
        quota_decision={"allowed": True, "reason": "quota_allowed"},
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


def test_shared_worker_unsupported_tool_denial_is_auditable() -> None:
    from mcp_broker.shared_worker import SharedWorkerRuntime, SharedWorkerTool

    runtime = SharedWorkerRuntime(
        tools=[
            SharedWorkerTool(
                upstream_id="example-stateless",
                name="example.echo",
                behavior="echo",
            )
        ]
    )

    decision = runtime.call_tool(
        tenant_context={
            "tenant_id": "tenant-a",
            "workspace_id": "workspace-a",
            "user_id": "user-a",
        },
        upstream_id="example-stateless",
        upstream_class="stateless",
        allowlisted=True,
        requires_local_state=False,
        tool_name="example.unknown",
        arguments={},
        quota_decision={"allowed": True, "reason": "quota_allowed"},
    )

    assert decision["allowed"] is False
    assert decision["reason"] == "unsupported_tool"
    assert decision["audit_event"] == {
        "event_type": "shared_worker_call",
        "tenant_id": "tenant-a",
        "workspace_id": "workspace-a",
        "user_id": "user-a",
        "upstream_id": "example-stateless",
        "tool_name": "example.unknown",
        "result": "denied",
        "denial_reason": "unsupported_tool",
    }
