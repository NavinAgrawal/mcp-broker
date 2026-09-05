"""Redacted fleet-status export payloads."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from typing import Any, Sequence

from mcp_broker.daemon_helpers import looks_like_filesystem_path
from mcp_broker.fleet_collection import FleetCollectionError, prepare_collection_envelope


_REDACTED = "[redacted]"
STATUS_TEXT_ENCODING = "utf-8"
_EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_SENSITIVE_WORDS = ("secret", "token", "credential", "password", "key")
_IDENTITY_FIELDS = (
    "active_profiles",
    "broker_id",
    "bundle_version",
    "environment",
    "schema_version",
)
_HEALTH_FIELDS = ("last_request_status", "started_at", "status", "updated_at")
_COUNTER_FIELDS = ("request_errors_total", "requests_total")
_UPSTREAM_FIELDS = (
    "auth_state",
    "enabled",
    "last_error",
    "mode",
    "mutating",
    "restarts",
    "state",
    "transport",
)


def export_fleet_status(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return a central-safe fleet status payload from a local broker snapshot."""

    identity = _mapping(snapshot.get("identity"))
    upstreams = _mapping(snapshot.get("upstreams"))
    return {
        "identity": _select_fields(identity, _IDENTITY_FIELDS),
        "health": _select_fields(snapshot, _HEALTH_FIELDS),
        "request_counters": _select_fields(snapshot, _COUNTER_FIELDS),
        "upstreams": {
            str(name): _redacted_upstream(_mapping(status))
            for name, status in sorted(upstreams.items())
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    snapshot = json.loads(
        args.status_file.expanduser().read_bytes().decode(STATUS_TEXT_ENCODING)
    )
    payload = export_fleet_status(snapshot)
    if args.target_url:
        try:
            payload = prepare_collection_envelope(
                payload,
                target_url=args.target_url,
                auth_ref=args.auth_ref,
                retention_days=args.retention_days,
                generated_at=args.generated_at or _current_timestamp(),
                collector_id=args.collector_id,
            )
        except FleetCollectionError as exc:
            raise SystemExit(str(exc)) from exc
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export a redacted fleet-status payload")
    parser.add_argument("--status-file", required=True, type=Path)
    parser.add_argument("--target-url")
    parser.add_argument("--auth-ref")
    parser.add_argument("--retention-days", type=int)
    parser.add_argument("--generated-at")
    parser.add_argument("--collector-id")
    return parser


def _current_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redacted_upstream(status: dict[str, Any]) -> dict[str, Any]:
    return {
        key: _redact_status_value(status.get(key))
        for key in _UPSTREAM_FIELDS
        if key in status
    }


def _select_fields(source: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {key: _redact_status_value(source[key]) for key in fields if key in source}


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _redact_status_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _redact_status_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_status_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_status_value(item) for item in value]
    if isinstance(value, str) and _is_sensitive_status_string(value):
        return _REDACTED
    return value


def _is_sensitive_status_string(value: str) -> bool:
    lowered = value.lower()
    return (
        "://" in value
        or looks_like_filesystem_path(value)
        or _EMAIL_PATTERN.search(value) is not None
        or any(word in lowered for word in _SENSITIVE_WORDS)
    )
