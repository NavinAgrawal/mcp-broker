import json
from pathlib import Path
from typing import Any

import pytest

from tests.support.bundles import minimal_bundle, write_signed_bundle


pytestmark = pytest.mark.unit


PUBLISH_PROVENANCE = {
    "repository": "mcp-broker",
    "commit": "abc1234",
    "builder": "local-publisher",
}
SIGNATURE_REF = "sigstore:governance-bundle.sig"


def _published_manifest(
    tmp_path: Path,
    *,
    bundle_id: str = "personal-local",
    version: str = "2026.07.01",
    channel: str = "stable",
) -> dict[str, Any]:
    from mcp_broker.governance_publish import publish_bundle

    bundle = minimal_bundle()
    bundle["bundle_id"] = bundle_id
    bundle["version"] = version
    bundle["channel"] = channel
    bundle_path = write_signed_bundle(
        tmp_path / f"{bundle_id}-{version}-{channel}.json",
        bundle,
    )
    manifest_path = publish_bundle(
        bundle_path=bundle_path,
        output_dir=tmp_path / "published",
        signature_ref=SIGNATURE_REF,
        provenance=PUBLISH_PROVENANCE,
        promotion_state="candidate",
    )
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def test_assignment_contract_matches_broker_user_team_channel_and_ring(
    tmp_path: Path,
) -> None:
    from mcp_broker.governance_assignment import evaluate_assignment

    published = _published_manifest(tmp_path)
    assignment = {
        "schema_version": 1,
        "assignments": [
            {
                "assignment_id": "team-stable-ring",
                "priority": 100,
                "match": {
                    "broker_ids": ["broker-west-1"],
                    "users": ["engineer-1"],
                    "teams": ["platform"],
                    "channels": ["stable"],
                    "rings": ["canary"],
                },
                "target": {
                    "bundle_id": "personal-local",
                    "version": "2026.07.01",
                    "channel": "stable",
                },
            }
        ],
    }
    context = {
        "broker_id": "broker-west-1",
        "user": "engineer-1",
        "teams": ["platform"],
        "channel": "stable",
        "ring": "canary",
    }

    decision = evaluate_assignment(
        assignment_source=assignment,
        published_manifests=[published],
        broker_context=context,
    )

    assert decision == {
        "schema_version": 1,
        "assignment_id": "team-stable-ring",
        "matched_by": {
            "broker_id": "broker-west-1",
            "user": "engineer-1",
            "teams": ["platform"],
            "channel": "stable",
            "ring": "canary",
        },
        "target": {
            "bundle_id": "personal-local",
            "version": "2026.07.01",
            "channel": "stable",
            "digest": published["bundle"]["digest"],
        },
        "changed_runtime_state": False,
    }


def test_assignment_contract_rejects_unpublished_bundle_reference(tmp_path: Path) -> None:
    from mcp_broker.governance_assignment import (
        GovernanceAssignmentError,
        evaluate_assignment,
    )

    published = _published_manifest(tmp_path, version="2026.07.01")
    assignment = {
        "schema_version": 1,
        "assignments": [
            {
                "assignment_id": "unknown-version",
                "priority": 10,
                "match": {"teams": ["platform"]},
                "target": {
                    "bundle_id": "personal-local",
                    "version": "2026.07.02",
                    "channel": "stable",
                },
            }
        ],
    }

    with pytest.raises(GovernanceAssignmentError) as exc_info:
        evaluate_assignment(
            assignment_source=assignment,
            published_manifests=[published],
            broker_context={"teams": ["platform"], "channel": "stable"},
        )
    assert str(exc_info.value) == "unpublished bundle target"


def test_assignment_contract_selects_highest_priority_match(tmp_path: Path) -> None:
    from mcp_broker.governance_assignment import evaluate_assignment

    low = _published_manifest(tmp_path, version="2026.07.01", channel="stable")
    high = _published_manifest(tmp_path, version="2026.07.02", channel="stable")
    assignment = {
        "schema_version": 1,
        "assignments": [
            {
                "assignment_id": "low-priority",
                "priority": 10,
                "match": {"teams": ["platform"]},
                "target": {
                    "bundle_id": "personal-local",
                    "version": "2026.07.01",
                    "channel": "stable",
                },
            },
            {
                "assignment_id": "high-priority",
                "priority": 100,
                "match": {"teams": ["platform"]},
                "target": {
                    "bundle_id": "personal-local",
                    "version": "2026.07.02",
                    "channel": "stable",
                },
            },
        ],
    }

    decision = evaluate_assignment(
        assignment_source=assignment,
        published_manifests=[low, high],
        broker_context={"teams": ["platform"], "channel": "stable"},
    )

    assert decision["assignment_id"] == "high-priority"
    assert decision["target"]["version"] == "2026.07.02"


def test_assignment_contract_rejects_ambiguous_same_priority_matches(
    tmp_path: Path,
) -> None:
    from mcp_broker.governance_assignment import (
        GovernanceAssignmentError,
        evaluate_assignment,
    )

    stable = _published_manifest(tmp_path, version="2026.07.01", channel="stable")
    canary = _published_manifest(tmp_path, version="2026.07.02", channel="canary")
    assignment = {
        "schema_version": 1,
        "assignments": [
            {
                "assignment_id": "stable-platform",
                "priority": 50,
                "match": {"teams": ["platform"]},
                "target": {
                    "bundle_id": "personal-local",
                    "version": "2026.07.01",
                    "channel": "stable",
                },
            },
            {
                "assignment_id": "canary-platform",
                "priority": 50,
                "match": {"teams": ["platform"]},
                "target": {
                    "bundle_id": "personal-local",
                    "version": "2026.07.02",
                    "channel": "canary",
                },
            },
        ],
    }

    with pytest.raises(GovernanceAssignmentError) as exc_info:
        evaluate_assignment(
            assignment_source=assignment,
            published_manifests=[stable, canary],
            broker_context={"teams": ["platform"], "channel": "stable"},
        )
    assert str(exc_info.value) == "ambiguous assignment matches"


def test_assignment_contract_rejects_no_match(tmp_path: Path) -> None:
    from mcp_broker.governance_assignment import (
        GovernanceAssignmentError,
        evaluate_assignment,
    )

    published = _published_manifest(tmp_path)
    assignment = {
        "schema_version": 1,
        "assignments": [
            {
                "assignment_id": "platform-only",
                "match": {"teams": ["platform"]},
                "target": {
                    "bundle_id": "personal-local",
                    "version": "2026.07.01",
                    "channel": "stable",
                },
            }
        ],
    }

    with pytest.raises(GovernanceAssignmentError) as exc_info:
        evaluate_assignment(
            assignment_source=assignment,
            published_manifests=[published],
            broker_context={"teams": ["security"]},
        )
    assert str(exc_info.value) == "no assignment match"


def test_assignment_contract_rejects_scalar_match_fields(tmp_path: Path) -> None:
    from mcp_broker.governance_assignment import (
        GovernanceAssignmentError,
        evaluate_assignment,
    )

    published = _published_manifest(tmp_path)
    assignment = {
        "schema_version": 1,
        "assignments": [
            {
                "assignment_id": "invalid-team-match",
                "priority": 10,
                "match": {"teams": "platform"},
                "target": {
                    "bundle_id": "personal-local",
                    "version": "2026.07.01",
                    "channel": "stable",
                },
            }
        ],
    }

    with pytest.raises(GovernanceAssignmentError, match="assignment match field must be a list"):
        evaluate_assignment(
            assignment_source=assignment,
            published_manifests=[published],
            broker_context={"teams": ["platform"], "channel": "stable"},
        )


@pytest.mark.parametrize(
    ("assignment_source", "expected_error"),
    [
        ({"schema_version": 2, "assignments": []}, "unsupported assignment schema_version"),
        ({"schema_version": 1, "assignments": {}}, "assignments must be a list"),
        ({"schema_version": 1, "assignments": ["bad"]}, "assignment entries must be objects"),
        (
            {
                "schema_version": 1,
                "assignments": [{"target": {"bundle_id": "personal-local"}}],
            },
            "assignment_id is required",
        ),
        (
            {
                "schema_version": 1,
                "assignments": [{"assignment_id": "missing-target"}],
            },
            "assignment target is required",
        ),
        (
            {
                "schema_version": 1,
                "assignments": [
                    {
                        "assignment_id": "missing-target-field",
                        "target": {"bundle_id": "personal-local", "version": "2026.07.01"},
                    }
                ],
            },
            "missing assignment target field: channel",
        ),
        (
            {
                "schema_version": 1,
                "assignments": [
                    {
                        "assignment_id": "bad-match",
                        "match": [],
                        "target": {
                            "bundle_id": "personal-local",
                            "version": "2026.07.01",
                            "channel": "stable",
                        },
                    }
                ],
            },
            "assignment match must be an object",
        ),
    ],
)
def test_assignment_contract_rejects_malformed_assignment_sources(
    tmp_path: Path,
    assignment_source: dict[str, Any],
    expected_error: str,
) -> None:
    from mcp_broker.governance_assignment import (
        GovernanceAssignmentError,
        evaluate_assignment,
    )

    published = _published_manifest(tmp_path)

    with pytest.raises(GovernanceAssignmentError) as exc_info:
        evaluate_assignment(
            assignment_source=assignment_source,
            published_manifests=[published],
            broker_context={"teams": ["platform"], "channel": "stable"},
        )
    assert str(exc_info.value) == expected_error


@pytest.mark.parametrize(
    "assignment_source, expected_error",
    [
        (
            {
                "schema_version": 1,
                "assignments": [],
                "metadata": {"local_path": "/var/tmp/private.yaml"},
            },
            "local paths are not allowed",
        ),
        (
            {
                "schema_version": 1,
                "assignments": [],
                "metadata": {"owner": "person@example.com"},
            },
            "account names are not allowed",
        ),
        (
            {
                "schema_version": 1,
                "assignments": [],
                "metadata": {"api_token": "example-token-value"},
            },
            "secret values are not allowed",
        ),
    ],
)
def test_assignment_contract_rejects_private_or_secret_assignment_source_values(
    tmp_path: Path,
    assignment_source: dict[str, Any],
    expected_error: str,
) -> None:
    from mcp_broker.governance_assignment import (
        GovernanceAssignmentError,
        evaluate_assignment,
    )

    published = _published_manifest(tmp_path)

    with pytest.raises(GovernanceAssignmentError) as exc_info:
        evaluate_assignment(
            assignment_source=assignment_source,
            published_manifests=[published],
            broker_context={"broker_id": "broker-west-1"},
        )
    assert str(exc_info.value) == expected_error


def test_assignment_match_helpers_allow_missing_match_fields() -> None:
    from mcp_broker.governance_assignment import _matches_any, _matches_scalar

    assert _matches_scalar({}, "channel", "stable") is True
    assert _matches_any({}, "teams", ["platform"]) is True


def test_assignment_secret_value_detection_flags_secret_field_names() -> None:
    from mcp_broker.governance_assignment import GovernanceAssignmentError, _is_secret_value, _reject_private_values

    with pytest.raises(GovernanceAssignmentError, match="secret values are not allowed"):
        _reject_private_values({"auth_token": "opaque-value"})

    assert _is_secret_value("metadata", "gh" + "p_" + "example") is True
    assert _is_secret_value("metadata", "public-channel") is False


def test_assignment_rules_default_to_an_empty_list() -> None:
    from mcp_broker.governance_assignment import _assignment_rules

    assert _assignment_rules({}) == []


def test_assignment_without_match_accepts_context_with_explicit_empty_defaults(
    tmp_path: Path,
) -> None:
    from mcp_broker.governance_assignment import evaluate_assignment

    published = _published_manifest(tmp_path)
    assignment = {
        "schema_version": 1,
        "assignments": [
            {
                "assignment_id": "default",
                "target": {
                    "bundle_id": "personal-local",
                    "version": "2026.07.01",
                    "channel": "stable",
                },
            }
        ],
    }

    decision = evaluate_assignment(
        assignment_source=assignment,
        published_manifests=[published],
        broker_context={},
    )

    assert decision["assignment_id"] == "default"
    assert decision["matched_by"] == {
        "broker_id": "",
        "user": "",
        "teams": [],
        "channel": "",
        "ring": "",
    }


def test_assignment_rules_reject_whitespace_only_ids() -> None:
    from mcp_broker.governance_assignment import GovernanceAssignmentError, _assignment_rules

    with pytest.raises(GovernanceAssignmentError) as exc_info:
        _assignment_rules({"assignments": [{"assignment_id": "  ", "target": {}}]})
    assert str(exc_info.value) == "assignment_id is required"


def test_assignment_published_target_validation_checks_every_assignment() -> None:
    from mcp_broker.governance_assignment import GovernanceAssignmentError, _validate_published_targets

    assignments = [
        {
            "assignment_id": "published",
            "target": {"bundle_id": "bundle", "version": "1", "channel": "stable"},
        },
        {
            "assignment_id": "unpublished",
            "target": {"bundle_id": "bundle", "version": "2", "channel": "stable"},
        },
    ]
    published = {("bundle", "1", "stable"): {"bundle": {}}}

    with pytest.raises(GovernanceAssignmentError) as exc_info:
        _validate_published_targets(assignments, published)
    assert str(exc_info.value) == "unpublished bundle target"


@pytest.mark.parametrize(
    ("match_field", "context_field", "matching_value", "other_value"),
    [
        ("broker_ids", "broker_id", "broker-one", "broker-two"),
        ("users", "user", "user-one", "user-two"),
        ("channels", "channel", "stable", "canary"),
        ("rings", "ring", "early", "general"),
    ],
)
def test_assignment_scalar_match_fields_require_the_matching_context_value(
    match_field: str,
    context_field: str,
    matching_value: str,
    other_value: str,
) -> None:
    from mcp_broker.governance_assignment import _assignment_matches

    match = {match_field: [matching_value]}

    assert _assignment_matches(match, {context_field: matching_value}) is True
    assert _assignment_matches(match, {context_field: other_value}) is False
    assert _assignment_matches(match, {}) is False


@pytest.mark.parametrize(
    "match_field",
    ["broker_ids", "users", "channels", "rings"],
)
def test_assignment_scalar_match_fields_use_empty_string_for_missing_context(
    match_field: str,
) -> None:
    from mcp_broker.governance_assignment import _assignment_matches

    assert _assignment_matches({match_field: [""]}, {}) is True


def test_assignment_team_match_requires_any_matching_team() -> None:
    from mcp_broker.governance_assignment import _assignment_matches

    match = {"teams": ["platform", "security"]}

    assert _assignment_matches(match, {"teams": ["support", "security"]}) is True
    assert _assignment_matches(match, {"teams": ["support"]}) is False
    assert _assignment_matches(match, {}) is False


def test_assignment_match_helpers_compare_values_as_strings() -> None:
    from mcp_broker.governance_assignment import _matches_any, _matches_scalar

    assert _matches_scalar({"values": [7]}, "values", "7") is True
    assert _matches_scalar({"values": [7]}, "values", "8") is False
    assert _matches_any({"values": [7, 9]}, "values", [8, "9"]) is True
    assert _matches_any({"values": [7, 9]}, "values", [8]) is False


def test_assignment_match_values_reject_a_scalar_with_exact_error() -> None:
    from mcp_broker.governance_assignment import GovernanceAssignmentError, _allowed_match_values

    with pytest.raises(GovernanceAssignmentError) as exc_info:
        _allowed_match_values({"values": "stable"}, "values")
    assert str(exc_info.value) == "assignment match field must be a list"


def test_assignment_priority_defaults_to_zero_and_accepts_numeric_strings() -> None:
    from mcp_broker.governance_assignment import _assignment_priority

    assert _assignment_priority({}) == 0
    assert _assignment_priority({"priority": "7"}) == 7


@pytest.mark.parametrize(
    ("value", "expected_error"),
    [
        ({"api_token": ["public-looking"]}, "secret values are not allowed"),
        ({"items": ["safe", "/var/tmp/private.yaml"]}, "local paths are not allowed"),
        ({"items": ["safe", "person@example.com"]}, "account names are not allowed"),
    ],
)
def test_assignment_private_value_rejection_recurses_through_lists(
    value: Any,
    expected_error: str,
) -> None:
    from mcp_broker.governance_assignment import GovernanceAssignmentError, _reject_private_values

    with pytest.raises(GovernanceAssignmentError) as exc_info:
        _reject_private_values(value)
    assert str(exc_info.value) == expected_error


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/var/tmp/private.yaml", True),
        ("~/private.yaml", True),
        (r"C:\\private.yaml", True),
        ("D:/private.yaml", True),
        ("relative/private.yaml", False),
        ("https://example.com/private.yaml", False),
    ],
)
def test_assignment_local_path_detection_covers_supported_path_forms(
    value: str,
    expected: bool,
) -> None:
    from mcp_broker.governance_assignment import _is_local_path

    assert _is_local_path(value) is expected
