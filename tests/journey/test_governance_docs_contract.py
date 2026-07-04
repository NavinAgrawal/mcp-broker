from pathlib import Path

import pytest


pytestmark = pytest.mark.journey

ROOT = Path(__file__).resolve().parents[2]
CONTROL_DOC = ROOT / "docs" / "governance-control-plane.md"
BUNDLES_DOC = ROOT / "docs" / "governance-bundles.md"


def test_governance_docs_split_publishable_bundle_documents() -> None:
    assert CONTROL_DOC.is_file()

    control = CONTROL_DOC.read_text(encoding="utf-8")
    bundles = BUNDLES_DOC.read_text(encoding="utf-8")
    combined = f"{control}\n{bundles}"

    required_sections = [
        "## Governance Bundle Documents",
        "### Profile Bundle",
        "### Upstream Catalog Bundle",
        "### Policy Bundle",
        "### Rollout Bundle",
        "### Compatibility Bundle",
        "## Local Execution Boundary",
        "## Offline Control-Plane Simulation",
        "## Signed Bundle Publishing",
        "## Assignment Source Contract",
        "## Rollout Controller",
        "## Operator Approval Workflow",
        "## Reference Control Plane",
    ]
    required_terms = [
        "profile bundle",
        "upstream catalog bundle",
        "policy bundle",
        "rollout bundle",
        "compatibility bundle",
        "publishable documents",
        "execution stays local",
        "local broker",
        "local simulation only",
        "not a hosted service",
        "does not run upstream tools centrally",
        "does not accept inbound remote tool calls",
        "approval_required",
        "allow_remote_code_execution",
        "mutating_upstreams_require_allowlist",
        "canary",
        "staged rollout",
        "rollback",
        "break-glass",
        "degraded status",
        "bypassed policy paths",
        "audit record",
        "expiration",
        "compatibility rejection",
        "signature reference",
        "promotion state",
        "revocation state",
        "reject unsigned",
        "reject unchecksummed",
        "changed_runtime_state: false",
        "broker identifiers",
        "users, teams, channels, and rings",
        "unpublished bundle target",
        "ambiguous assignment matches",
        "local paths are not allowed",
        "mcp-broker governance rollout-control",
        "governance-rollout/action-log.jsonl",
        "append-only action ledger",
        "canary, staged, broad, rollback, and hold actions",
        "mcp-broker governance approve",
        "governance-approvals/audit.jsonl",
        "expiring approval record",
        "rollout, rollback, policy_override, and break_glass approvals",
        "mcp-broker governance reference-control-plane",
        "governance-reference-control-plane",
        "local_reference_only",
        "governance-reference-control-plane/reports/reference-report.json",
        "publish, assign, collect, rollout_control, approve, and rollback",
        "does not centralize tool execution",
        "does not call upstream tools",
        "does not apply deployments",
    ]
    forbidden_terms = [
        "/Users/",
        "CloudStorage",
        "broker.private.yaml.bak",
        "navin@",
        "ms365-",
        "codebase-memory",
    ]

    assert [section for section in required_sections if section not in control] == []
    assert [term for term in required_terms if term not in combined] == []
    assert [term for term in forbidden_terms if term in combined] == []


def test_phase_roadmap_links_governance_control_plane_contract() -> None:
    roadmap = (ROOT / "docs" / "phase-foundation-roadmap.md").read_text(encoding="utf-8")

    assert "docs/governance-control-plane.md" in roadmap
    assert "publishable profile, upstream-catalog, policy, rollout, and compatibility" in roadmap
