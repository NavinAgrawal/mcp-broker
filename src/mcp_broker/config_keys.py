"""Schema key inventories for broker config parsing."""

from __future__ import annotations

import re

ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SESSION_ENV_SOURCES = frozenset({"client_cwd"})
SESSION_ENV_ALLOWED_MESSAGE = ", ".join(sorted(SESSION_ENV_SOURCES))

TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "runtime",
        "broker",
        "profiles",
        "clients",
        "upstreams",
    }
)
RUNTIME_KEYS = frozenset({"root", "socket_path", "log_dir", "state_dir", "secrets_dir"})
BROKER_KEYS = frozenset(
    {
        "identity",
        "tool_namespace_separator",
        "idle_timeout_seconds",
        "cpu_watchdog_percent",
        "cpu_watchdog_seconds",
        "remote_auth",
    }
)
BROKER_IDENTITY_KEYS = frozenset({"broker_id", "environment", "bundle_version"})
REMOTE_AUTH_KEYS = frozenset({"enabled", "required", "token_env", "token_file"})
PROFILE_KEYS = frozenset(
    {
        "max_tools",
        "compact_tools_enabled",
        "broker_tool_name_style",
        "allow_mutating_upstreams",
        "client_root_match",
    }
)
UPSTREAM_KEYS = frozenset(
    {
        "enabled",
        "mode",
        "transport",
        "purpose",
        "tags",
        "tool_prefix",
        "command",
        "args",
        "working_dir",
        "state_dir",
        "profiles",
        "env",
        "env_files",
        "session_env",
        "request_meta",
        "mutating",
        "serialize_calls",
        "strict_initialization",
        "inject_cwd_project",
        "inject_cwd_project_exclude",
        "startup_timeout_seconds",
        "tool_timeouts",
        "restart",
        "health",
        "resources",
        "auth_repair",
        "auth_probe",
        "smoke",
    }
)
