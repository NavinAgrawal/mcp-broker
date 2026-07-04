from pathlib import Path

import pytest


pytestmark = pytest.mark.journey


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_protocol_docs_capture_websocket_transport_decision() -> None:
    protocol = (REPO_ROOT / "docs" / "protocol.md").read_text(encoding="utf-8")
    planning_path = REPO_ROOT / "TODO.md"
    if not planning_path.exists():
        planning_path = REPO_ROOT / "ROADMAP.md"
    planning = planning_path.read_text(encoding="utf-8")

    assert "WebSocket is not a standard MCP transport" in protocol
    assert "custom transport extension point" in protocol
    assert "opened with a real server compatibility fixture" in protocol
    assert "WebSocket" in planning


def test_protocol_docs_capture_remote_api_contract_non_listener_boundary() -> None:
    protocol = (REPO_ROOT / "docs" / "protocol.md").read_text(encoding="utf-8")

    assert "Remote Broker API Contract" in protocol
    assert "network_listener_supported: false" in protocol
    assert "auth_context, tenant_context, and policy_decision" in protocol
    assert "tool discovery, describe, call, status, cancellation" in protocol
    assert "streaming chunks and audit events" in protocol
    assert "does not expose a network listener" in protocol
