"""Local-only fleet-status collection envelope preparation."""

from __future__ import annotations

from datetime import datetime, timedelta
import re
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlparse

from mcp_broker.daemon_helpers import looks_like_filesystem_path


COLLECTION_SCHEMA_VERSION = 1
COLLECTION_KIND = "mcp-broker.fleet-status.collection"
COLLECTION_MODE = "prepare-only"
UPLOAD_METHOD = "POST"
RETRY_MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 15
RETENTION_MIN_DAYS = 1
RETENTION_MAX_DAYS = 365
FAILURE_ON_UPLOAD = "mark_degraded"
LOCAL_SPOOL_ENABLED = False
_REDACTED = "[redacted]"
_EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_SAFE_COLLECTOR_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")
_SAFE_AUTH_REF_PATTERN = re.compile(r"^(?:env|keychain):[A-Za-z0-9_.-]+$")
_SENSITIVE_WORDS = ("secret", "token", "credential", "password", "key")
_SENSITIVE_QUERY_KEYS = frozenset(_SENSITIVE_WORDS)
_STATUS_ROOT_FIELDS = frozenset(("identity", "health", "request_counters", "upstreams"))
_IDENTITY_FIELDS = frozenset(
    (
        "active_profiles",
        "broker_id",
        "bundle_version",
        "environment",
        "schema_version",
    )
)
_HEALTH_FIELDS = frozenset(("last_request_status", "started_at", "status", "updated_at"))
_COUNTER_FIELDS = frozenset(("request_errors_total", "requests_total"))
_UPSTREAM_FIELDS = frozenset(
    (
        "auth_state",
        "enabled",
        "last_error",
        "mode",
        "mutating",
        "restarts",
        "state",
        "transport",
    )
)


class FleetCollectionError(ValueError):
    """Raised when a fleet-status collection envelope is unsafe."""


def prepare_collection_envelope(
    status_payload: Mapping[str, Any],
    *,
    target_url: str,
    auth_ref: str,
    retention_days: int,
    generated_at: str,
    collector_id: str,
) -> dict[str, Any]:
    """Return a central-safe collection envelope without uploading it."""

    if not isinstance(status_payload, Mapping):
        raise FleetCollectionError("unsafe fleet status payload: root must be an object")
    _validate_target_url(target_url)
    _validate_auth_ref(auth_ref)
    _validate_retention_days(retention_days)
    _validate_collector_id(collector_id)
    generated_dt = _parse_generated_at(generated_at)
    safe_payload = _collection_payload(status_payload)
    delete_after = generated_dt + timedelta(days=retention_days)

    return {
        "schema_version": COLLECTION_SCHEMA_VERSION,
        "kind": COLLECTION_KIND,
        "generated_at": generated_at,
        "collector": {
            "id": collector_id,
            "mode": COLLECTION_MODE,
        },
        "upload": {
            "target_url": target_url,
            "method": UPLOAD_METHOD,
            "attempted": False,
            "auth_ref": auth_ref,
        },
        "retention": {
            "days": retention_days,
            "delete_after": delete_after.isoformat(),
        },
        "retry": {
            "max_attempts": RETRY_MAX_ATTEMPTS,
            "backoff_seconds": RETRY_BACKOFF_SECONDS,
        },
        "failure_handling": {
            "on_upload_failure": FAILURE_ON_UPLOAD,
            "local_spool": LOCAL_SPOOL_ENABLED,
        },
        "payload": safe_payload,
    }


def _validate_target_url(target_url: object) -> None:
    if (
        not isinstance(target_url, str)
        or not target_url.strip()
    ):
        raise FleetCollectionError("collection target must be an https URL")
    parsed = urlparse(target_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise FleetCollectionError("collection target must be an https URL")
    if parsed.username or parsed.password:
        raise FleetCollectionError("collection target URL must not contain secret credentials")
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered_key = key.lower()
        lowered_value = value.lower()
        if lowered_key in _SENSITIVE_QUERY_KEYS or any(
            word in lowered_value for word in _SENSITIVE_WORDS
        ):
            raise FleetCollectionError("collection target URL must not contain secret query data")


def _validate_auth_ref(auth_ref: object) -> None:
    if isinstance(auth_ref, str) and _SAFE_AUTH_REF_PATTERN.fullmatch(auth_ref):
        return
    raise FleetCollectionError("collection auth_ref must be env:NAME or keychain:NAME")


def _validate_retention_days(retention_days: object) -> None:
    if (
        not isinstance(retention_days, int)
        or isinstance(retention_days, bool)
        or not RETENTION_MIN_DAYS <= retention_days <= RETENTION_MAX_DAYS
    ):
        raise FleetCollectionError("collection retention_days must be between 1 and 365")


def _validate_collector_id(collector_id: object) -> None:
    if (
        not isinstance(collector_id, str)
        or not collector_id.strip()
        or not _SAFE_COLLECTOR_ID_PATTERN.fullmatch(collector_id)
        or _is_unsafe_status_string(collector_id)
    ):
        raise FleetCollectionError("collection collector_id must be a safe identifier")


def _parse_generated_at(generated_at: object) -> datetime:
    if not isinstance(generated_at, str):
        raise FleetCollectionError("collection generated_at must be ISO-8601")
    try:
        return datetime.fromisoformat(generated_at)
    except ValueError as exc:
        raise FleetCollectionError("collection generated_at must be ISO-8601") from exc


def _collection_payload(status_payload: Mapping[str, Any]) -> dict[str, Any]:
    _reject_unknown_fields(status_payload, _STATUS_ROOT_FIELDS, "root")
    identity = _safe_mapping(status_payload.get("identity"), _IDENTITY_FIELDS, "identity")
    health = _safe_mapping(status_payload.get("health"), _HEALTH_FIELDS, "health")
    request_counters = _safe_mapping(
        status_payload.get("request_counters"),
        _COUNTER_FIELDS,
        "request_counters",
    )
    upstreams = status_payload.get("upstreams")
    if upstreams is None:
        upstream_payload = {}
    elif isinstance(upstreams, Mapping):
        upstream_payload = {
            f"upstream-{index:03d}": _safe_mapping(status, _UPSTREAM_FIELDS, "upstreams")
            for index, (_name, status) in enumerate(sorted(upstreams.items()), start=1)
        }
    else:
        raise FleetCollectionError("unsafe fleet status payload: upstreams must be an object")
    return {
        "identity": identity,
        "health": health,
        "request_counters": request_counters,
        "upstreams": upstream_payload,
    }


def _safe_mapping(value: Any, allowed_fields: frozenset[str], label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise FleetCollectionError(f"unsafe fleet status payload: {label} must be an object")
    _reject_unknown_fields(value, allowed_fields, label)
    return {str(key): _safe_status_value(item) for key, item in value.items()}


def _reject_unknown_fields(
    value: Mapping[str, Any],
    allowed_fields: frozenset[str],
    label: str,
) -> None:
    if any(str(key) not in allowed_fields for key in value):
        raise FleetCollectionError(
            f"unsafe fleet status payload: {label} contains disallowed fields"
        )


def _safe_status_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _safe_status_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_status_value(item) for item in value]
    if isinstance(value, tuple):
        return [_safe_status_value(item) for item in value]
    if isinstance(value, str) and _is_unsafe_status_string(value):
        raise FleetCollectionError("unsafe fleet status payload: redaction required")
    return value


def _is_unsafe_status_string(value: str) -> bool:
    if value == _REDACTED:
        return False
    lowered = value.lower()
    return (
        "://" in value
        or looks_like_filesystem_path(value)
        or _EMAIL_PATTERN.search(value) is not None
        or any(word in lowered for word in _SENSITIVE_WORDS)
    )
