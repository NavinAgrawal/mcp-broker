"""Daemon helper functions kept separate from the socket lifecycle."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from typing import Mapping, Protocol, Sequence, runtime_checkable

from mcp_broker.broker import BrokerToolError
from mcp_broker.config import UpstreamConfig
from mcp_broker.upstream_protocols import StdioUpstreamClientProtocol


SENSITIVE_LOG_KEY_PARTS = (
    "access_id",
    "api_key",
    "authorization",
    "credential",
    "env",
    "password",
    "secret",
    "token",
)


def health_profile(request: dict[str, object]) -> str:
    params = request.get("params")
    if isinstance(params, dict) and isinstance(params.get("profile"), str):
        return params["profile"]
    return "default"


def configured_upstream_health(upstream: UpstreamConfig) -> dict[str, object]:
    state = "disabled" if not upstream.enabled or upstream.mode == "disabled" else "configured"
    return {
        "state": state,
        "pid": None,
        "cpu_percent": None,
        "memory_mb": None,
        "restarts": 0,
        "last_error": None,
    }


def per_call_health_snapshot(
    upstream: UpstreamConfig,
    registered_calls: Sequence[tuple[str, StdioUpstreamClientProtocol]],
) -> dict[str, object]:
    count = sum(1 for owner, _client in registered_calls if owner == upstream.name)
    snapshot = configured_upstream_health(upstream)
    snapshot["active_call_count"] = count
    if count:
        snapshot["state"] = "running"
    return snapshot


def passive_auth_probe(
    upstream: UpstreamConfig,
    *,
    environ: Mapping[str, str],
    now: datetime | None = None,
) -> dict[str, object]:
    if not upstream.enabled or upstream.mode == "disabled":
        return {"auth_probe": "none"}
    if upstream.auth_probe is not None:
        return _passive_configured_auth_probe(upstream, now=now or datetime.now(timezone.utc))
    missing_sources: list[str] = []
    for source_name in upstream.env.values():
        if not environ.get(source_name):
            missing_sources.append(f"env:{source_name}")
    for target_name, secret_path in upstream.env_files.items():
        if not _secret_file_has_value(secret_path):
            missing_sources.append(f"secret_file:{target_name}")
    if missing_sources:
        return {
            "auth_probe": "credentials_missing",
            "auth_state": "unauthenticated",
            "last_error": (
                f"missing auth source for upstream {upstream.name}: "
                f"{', '.join(missing_sources)}"
            ),
        }
    if upstream.env or upstream.env_files or upstream.request_meta:
        return {"auth_probe": "credentials_present"}
    if upstream.auth_repair is not None:
        return {"auth_probe": "auth_repair_configured"}
    return {"auth_probe": "none"}


def _passive_configured_auth_probe(
    upstream: UpstreamConfig,
    *,
    now: datetime,
) -> dict[str, object]:
    probe = upstream.auth_probe
    assert probe is not None
    raw = _read_secret_text(probe.token_file)
    if raw is None or not any(char not in "\r\n" for char in raw):
        return {
            "auth_probe": "credentials_missing",
            "auth_state": "unauthenticated",
            "last_error": f"missing OAuth token file for upstream {upstream.name}",
        }
    try:
        token_data = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "auth_probe": "credentials_missing",
            "auth_state": "unauthenticated",
            "last_error": f"invalid OAuth token file for upstream {upstream.name}",
        }
    if not isinstance(token_data, dict):
        return {
            "auth_probe": "credentials_missing",
            "auth_state": "unauthenticated",
            "last_error": f"invalid OAuth token file for upstream {upstream.name}",
        }
    missing_fields = [
        field
        for field in probe.required_fields
        if not isinstance(token_data.get(field), str) or not token_data.get(field)
    ]
    if missing_fields:
        return {
            "auth_probe": "credentials_missing",
            "auth_state": "unauthenticated",
            "last_error": (
                f"missing OAuth token field for upstream {upstream.name}: "
                f"{', '.join(missing_fields)}"
            ),
        }
    expiry_field = probe.refresh_token_expiry_field
    if expiry_field:
        expiry = _parse_oauth_expiry(token_data.get(expiry_field))
        if expiry is None:
            return {
                "auth_probe": "credentials_missing",
                "auth_state": "unauthenticated",
                "last_error": (
                    f"invalid OAuth refresh-token expiry for upstream {upstream.name}: "
                    f"{expiry_field}"
                ),
            }
        if expiry <= now:
            return {
                "auth_probe": "oauth_refresh_expired",
                "auth_state": "unauthenticated",
                "last_error": f"expired OAuth refresh token for upstream {upstream.name}",
            }
    return {"auth_probe": "credentials_present"}


def _parse_oauth_expiry(value: object) -> datetime | None:
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return datetime.fromtimestamp(parsed.timestamp(), timezone.utc)


def merge_passive_auth_probe(
    snapshot: dict[str, object],
    probe: dict[str, object],
) -> dict[str, object]:
    merged = snapshot | {"auth_probe": probe.get("auth_probe", "none")}
    if merged.get("last_error") is None and probe.get("last_error") is not None:
        merged["last_error"] = probe["last_error"]
    if merged.get("auth_state") in {None, "unknown"} and probe.get("auth_state") is not None:
        merged["auth_state"] = probe["auth_state"]
    return merged


@runtime_checkable
class _ReadableTextFile(Protocol):
    def read_text(self, *, encoding: str) -> str: ...


def _read_secret_text(secret_path: object) -> str | None:
    if not isinstance(secret_path, _ReadableTextFile):
        return None
    try:
        return secret_path.read_text(encoding="utf-8")
    except OSError:
        return None


def _secret_file_has_value(secret_path: object) -> bool:
    value = _read_secret_text(secret_path)
    return value is not None and any(char not in "\r\n" for char in value)


def stdio_client_key(upstream: UpstreamConfig, *, session_id: str | None) -> str | tuple[str, str]:
    if upstream.mode == "per_session":
        if not session_id:
            raise BrokerToolError(
                code="missing_session",
                message=f"broker_session_id is required for per_session upstream: {upstream.name}",
                upstream_name=upstream.name,
            )
        return (upstream.name, session_id)
    return upstream.name


def stdio_client_name(key: str | tuple[str, str]) -> str:
    if isinstance(key, tuple):
        return f"{key[0]}:{key[1]}"
    return key


def per_session_health_snapshot(
    clients: list[StdioUpstreamClientProtocol],
) -> dict[str, object]:
    snapshots = [client.health_snapshot() for client in clients]
    states = [snapshot.get("state") for snapshot in snapshots]
    errors = [
        snapshot.get("last_error")
        for snapshot in snapshots
        if snapshot.get("last_error") is not None
    ]
    restart_total = sum(
        restart
        for snapshot in snapshots
        if isinstance((restart := snapshot.get("restarts")), int)
    )
    return {
        "state": "running" if "running" in states else str(states[0]),
        "pid": None,
        "cpu_percent": None,
        "memory_mb": None,
        "restarts": restart_total,
        "last_error": errors[0] if errors else None,
        "sessions": len(clients),
    }


def result_matches_auth_repair(upstream: UpstreamConfig, result: dict[str, object]) -> bool:
    repair = upstream.auth_repair
    if repair is None:
        return False
    text = _result_content_text(result)
    if not text:
        return False
    if result.get("isError") is not True and not text.startswith("Error:"):
        return False
    return any(trigger in text for trigger in repair.trigger_errors)


def redact_log_field(key: str, value: object) -> object:
    normalized = key.lower().replace("-", "_")
    if any(part in normalized for part in SENSITIVE_LOG_KEY_PARTS):
        return "[redacted]"
    return redact_log_value(value)


def redact_log_value(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): redact_log_field(str(key), item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_log_value(item) for item in value]
    if isinstance(value, tuple):
        return [redact_log_value(item) for item in value]
    if isinstance(value, str):
        if "://" in value:
            return "[redacted:url]"
        if looks_like_filesystem_path(value):
            return "[redacted:path]"
    return value


def looks_like_filesystem_path(value: str) -> bool:
    return value.startswith(("/", "~/", "$HOME/", "${HOME}/")) or "/Users/" in value


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _result_content_text(result: dict[str, object]) -> str:
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)
