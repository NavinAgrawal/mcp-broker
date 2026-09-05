"""Governance bundle assignment evaluation."""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence


ASSIGNMENT_SCHEMA_VERSION = 1
SECRET_FIELD_PATTERN = re.compile(r"(secret|token|password|credential)", re.IGNORECASE)
SECRET_VALUE_PATTERN = re.compile(
    r"(sk-proj-|sk-ant-|ghp_|gho_|ghu_|xoxb-|xoxp-|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
WINDOWS_ABSOLUTE_PATH_PATTERN = re.compile(r"^[A-Za-z]:[\\/]")


class GovernanceAssignmentError(ValueError):
    """Raised when assignment source evaluation fails."""


def evaluate_assignment(
    *,
    assignment_source: Mapping[str, Any],
    published_manifests: Sequence[Mapping[str, Any]],
    broker_context: Mapping[str, Any],
) -> dict[str, Any]:
    _validate_assignment_source(assignment_source)
    published = _published_targets(published_manifests)
    assignments = _assignment_rules(assignment_source)
    _validate_published_targets(assignments, published)

    matches = [
        assignment
        for assignment in assignments
        if _assignment_matches(assignment.get("match", {}), broker_context)
    ]
    if not matches:
        raise GovernanceAssignmentError("no assignment match")

    best_priority = max(_assignment_priority(assignment) for assignment in matches)
    best_matches = [
        assignment
        for assignment in matches
        if _assignment_priority(assignment) == best_priority
    ]
    if len(best_matches) != 1:
        raise GovernanceAssignmentError("ambiguous assignment matches")

    selected = best_matches[0]
    target_key = _target_key(selected["target"])
    published_manifest = published[target_key]
    return {
        "schema_version": ASSIGNMENT_SCHEMA_VERSION,
        "assignment_id": selected["assignment_id"],
        "matched_by": {
            "broker_id": broker_context.get("broker_id", ""),
            "user": broker_context.get("user", ""),
            "teams": list(broker_context.get("teams", [])),
            "channel": broker_context.get("channel", ""),
            "ring": broker_context.get("ring", ""),
        },
        "target": {
            "bundle_id": target_key[0],
            "version": target_key[1],
            "channel": target_key[2],
            "digest": published_manifest["bundle"]["digest"],
        },
        "changed_runtime_state": False,
    }


def _validate_assignment_source(assignment_source: Mapping[str, Any]) -> None:
    if assignment_source.get("schema_version") != ASSIGNMENT_SCHEMA_VERSION:
        raise GovernanceAssignmentError("unsupported assignment schema_version")
    _reject_private_values(assignment_source)


def _assignment_rules(assignment_source: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    assignments = assignment_source.get("assignments", [])
    if not isinstance(assignments, list):
        raise GovernanceAssignmentError("assignments must be a list")
    for assignment in assignments:
        if not isinstance(assignment, Mapping):
            raise GovernanceAssignmentError("assignment entries must be objects")
        if not str(assignment.get("assignment_id", "")).strip():
            raise GovernanceAssignmentError("assignment_id is required")
        if "target" not in assignment:
            raise GovernanceAssignmentError("assignment target is required")
    return assignments


def _published_targets(
    published_manifests: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str, str], Mapping[str, Any]]:
    targets: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for manifest in published_manifests:
        bundle = manifest["bundle"]
        targets[(bundle["bundle_id"], bundle["version"], bundle["channel"])] = manifest
    return targets


def _validate_published_targets(
    assignments: Sequence[Mapping[str, Any]],
    published: Mapping[tuple[str, str, str], Mapping[str, Any]],
) -> None:
    for assignment in assignments:
        target_key = _target_key(assignment["target"])
        if target_key not in published:
            raise GovernanceAssignmentError("unpublished bundle target")


def _target_key(target: Mapping[str, Any]) -> tuple[str, str, str]:
    try:
        return (target["bundle_id"], target["version"], target["channel"])
    except KeyError as exc:
        raise GovernanceAssignmentError(f"missing assignment target field: {exc.args[0]}") from exc


def _assignment_matches(match: Any, broker_context: Mapping[str, Any]) -> bool:
    if not isinstance(match, Mapping):
        raise GovernanceAssignmentError("assignment match must be an object")
    return (
        _matches_scalar(match, "broker_ids", broker_context.get("broker_id", ""))
        and _matches_scalar(match, "users", broker_context.get("user", ""))
        and _matches_any(match, "teams", broker_context.get("teams", []))
        and _matches_scalar(match, "channels", broker_context.get("channel", ""))
        and _matches_scalar(match, "rings", broker_context.get("ring", ""))
    )


def _matches_scalar(match: Mapping[str, Any], field_name: str, value: Any) -> bool:
    allowed = _allowed_match_values(match, field_name)
    if allowed is None:
        return True
    return str(value) in {str(item) for item in allowed}


def _matches_any(match: Mapping[str, Any], field_name: str, values: Any) -> bool:
    allowed = _allowed_match_values(match, field_name)
    if allowed is None:
        return True
    value_set = {str(item) for item in values}
    return bool(value_set.intersection({str(item) for item in allowed}))


def _allowed_match_values(match: Mapping[str, Any], field_name: str) -> list[Any] | None:
    allowed = match.get(field_name)
    if allowed is None:
        return None
    if not isinstance(allowed, list):
        raise GovernanceAssignmentError("assignment match field must be a list")
    return allowed


def _assignment_priority(assignment: Mapping[str, Any]) -> int:
    return int(assignment.get("priority", 0))


def _reject_private_values(value: Mapping[str, Any]) -> None:
    for child_name, child_value in value.items():
        _reject_private_value(child_value, field_name=str(child_name))


def _reject_private_value(value: Any, *, field_name: str) -> None:
    if isinstance(value, Mapping):
        for child_name, child_value in value.items():
            _reject_private_value(child_value, field_name=str(child_name))
        return
    if isinstance(value, list):
        for child_value in value:
            _reject_private_value(child_value, field_name=field_name)
        return
    if not isinstance(value, str):
        return
    if _is_local_path(value):
        raise GovernanceAssignmentError("local paths are not allowed")
    if EMAIL_PATTERN.fullmatch(value):
        raise GovernanceAssignmentError("account names are not allowed")
    if _is_secret_value(field_name, value):
        raise GovernanceAssignmentError("secret values are not allowed")


def _is_local_path(value: str) -> bool:
    return value.startswith(("/", "~/")) or bool(WINDOWS_ABSOLUTE_PATH_PATTERN.match(value))


def _is_secret_value(field_name: str, value: str) -> bool:
    if SECRET_VALUE_PATTERN.search(value):
        return True
    return bool(value.strip() and SECRET_FIELD_PATTERN.search(field_name))
