from pathlib import Path

import pytest


pytestmark = pytest.mark.journey

ROOT = Path(__file__).resolve().parents[2]
GUARDRAILS_DOC = ROOT / "docs" / "shared-runtime-guardrails.md"


def test_shared_runtime_guardrails_document_phase_3_gates() -> None:
    assert GUARDRAILS_DOC.is_file()

    document = GUARDRAILS_DOC.read_text(encoding="utf-8")

    required_sections = [
        "# Shared Runtime Guardrails",
        "## Current Boundary",
        "## Preconditions",
        "## Decision Gates",
        "## Threat Model",
        "## Tenant Model",
        "## Remote API Contract",
        "## Session Affinity And State Placement",
        "## Quota And Cost Controls",
        "## Shared Worker Runtime",
        "## Mandatory Non-Goals",
    ]
    required_terms = [
        "shared hosted execution is not implemented",
        "Phase 1 value proof",
        "Phase 2 governance proof",
        "tenant isolation",
        "authorization",
        "quotas",
        "session affinity",
        "distributed state",
        "cost controls",
        "audit",
        "failure domains",
        "tenant, workspace, user, upstream, token, log, runtime-state, and audit",
        "unknown upstream classes default to local-only",
        "stateless allowlisted upstreams are shared-worker eligible only when they require no local state",
        "hosted_execution_supported: false",
        "default_execution_boundary: local_edge",
        "network_listener_supported: false",
        "authenticated tool discovery, describe, call, status, cancellation, streaming chunks, and audit events",
        "auth_context, tenant_context, and policy_decision",
        "stateful, OAuth, browser, file-access, local-secret, and unknown upstream classes remain local edge",
        "shared-worker state binds to tenant, workspace, user, and upstream scope",
        "private inventory class labels are forbidden",
        "default quota decision is deny",
        "external metering is not implemented",
        "global, team, user, upstream, and tool scopes",
        "quota denial is fail-closed and audit-required",
        "kill switches are evaluated before limit counters",
        "in-process fake worker only",
        "network, file-access, secret, local-state, and inherited-environment access default to deny",
        "real upstream routing is not implemented",
        "unsupported shared-worker tools are denied with audit events",
        "local edge broker remains the default",
        "no remote listener",
        "no shared upstream execution",
    ]
    forbidden_terms = [
        "/Users/",
        "CloudStorage",
        "broker.private.yaml",
        "navin@",
        "ms365-",
        "codebase-memory",
    ]

    assert [section for section in required_sections if section not in document] == []
    assert [term for term in required_terms if term not in document] == []
    assert [term for term in forbidden_terms if term in document] == []


def test_public_surfaces_link_shared_runtime_guardrails() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    roadmap = (ROOT / "ROADMAP.md").read_text(encoding="utf-8")
    phase_roadmap = (ROOT / "docs" / "phase-foundation-roadmap.md").read_text(
        encoding="utf-8"
    )

    required_link = "docs/shared-runtime-guardrails.md"

    assert required_link in readme
    assert required_link in roadmap
    assert required_link in phase_roadmap
    assert "shared hosted execution is not implemented" in phase_roadmap
