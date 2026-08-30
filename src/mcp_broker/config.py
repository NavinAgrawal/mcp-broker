"""Central configuration model for mcp-broker."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Any, TypedDict

import yaml

from mcp_broker.client_config import ClientRenderConfig
from mcp_broker.config_keys import (
    BROKER_KEYS,
    ENV_NAME_PATTERN,
    META_NAME_PATTERN,
    PROFILE_KEYS,
    REMOTE_AUTH_KEYS,
    RUNTIME_KEYS,
    SESSION_ENV_ALLOWED_MESSAGE,
    SESSION_ENV_SOURCES,
    TOP_LEVEL_KEYS,
    UPSTREAM_KEYS,
)
from mcp_broker.config_identity import (
    BrokerIdentityConfig,
    CONFIG_SCHEMA_VERSION,
    DEFAULT_BROKER_ID,
    DEFAULT_BROKER_ENVIRONMENT,
    DEFAULT_BUNDLE_VERSION,
)
from mcp_broker.schema import (
    AuthProbePolicy,
    AuthRepairPolicy,
    DEFAULT_CPU_WATCHDOG_PERCENT,
    DEFAULT_CPU_WATCHDOG_SECONDS,
    DEFAULT_IDLE_TIMEOUT_SECONDS,
    HealthPolicy,
    ResourcePolicy,
    RestartPolicy,
    SmokeProbe,
    parse_mode,
    parse_profiles,
    parse_startup_timeout,
    parse_transport,
)
from mcp_broker.profiles import ToolExposureProfile


def _parse_path(path: str, value: Any) -> Path:
    if not isinstance(value, str | Path):
        raise ValueError(f"{path} must be a path string")
    if not str(value):
        raise ValueError(f"{path} must be a path string")
    return _expand_path(value)


def _parse_config_path(path: str, value: Any, runtime: "RuntimeConfig") -> Path:
    if not isinstance(value, str | Path):
        raise ValueError(f"{path} must be a path string")
    if not str(value):
        raise ValueError(f"{path} must be a path string")
    return Path(_expand_config_text(value, runtime)).expanduser()


def _parse_optional_config_path(
    path: str,
    value: Any,
    runtime: "RuntimeConfig",
) -> Path | None:
    if value is None:
        return None
    return _parse_config_path(path, value, runtime)


def _expand_path(value: str | Path) -> Path:
    return Path(_expand_text(value)).expanduser()


def _expand_text(value: str | Path) -> str:
    expanded = str(value)
    for _ in range(5):
        next_value = os.path.expandvars(expanded)
        if next_value == expanded:
            break
        expanded = next_value
    return os.path.expanduser(expanded)


@dataclass(frozen=True)
class RuntimeConfig:
    root: Path
    socket_path: Path
    log_dir: Path
    state_dir: Path
    secrets_dir: Path

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "RuntimeConfig":
        _validate_keys("runtime", data, RUNTIME_KEYS)
        if "root" not in data:
            raise ValueError("missing required config key: runtime.root")
        root = _parse_path("runtime.root", data["root"])
        return cls(
            root=root,
            socket_path=_parse_path(
                "runtime.socket_path",
                data.get("socket_path", root / "sockets" / "broker.sock"),
            ),
            log_dir=_parse_path("runtime.log_dir", data.get("log_dir", root / "logs")),
            state_dir=_parse_path("runtime.state_dir", data.get("state_dir", root / "state")),
            secrets_dir=_parse_path(
                "runtime.secrets_dir",
                data.get("secrets_dir", root / "secrets"),
            ),
        )


@dataclass(frozen=True)
class RemoteBrokerAuthConfig:
    enabled: bool = False
    required: bool = True
    token_env: str | None = None
    token_file: Path | None = None

    @classmethod
    def from_mapping(
        cls,
        data: dict[str, Any] | None,
        *,
        runtime: RuntimeConfig,
    ) -> "RemoteBrokerAuthConfig":
        raw = {} if data is None else data
        if not isinstance(raw, dict):
            raise ValueError("broker.remote_auth must be a mapping")
        _validate_keys("broker.remote_auth", raw, REMOTE_AUTH_KEYS)
        enabled = _parse_bool("broker.remote_auth.enabled", raw.get("enabled", False))
        required = _parse_bool("broker.remote_auth.required", raw.get("required", True))
        if not required:
            raise ValueError("broker.remote_auth.required must be true")
        token_env = _parse_optional_env_name(
            "broker.remote_auth.token_env",
            raw.get("token_env"),
        )
        token_file = _parse_optional_config_path(
            "broker.remote_auth.token_file",
            raw.get("token_file"),
            runtime,
        )
        if enabled and token_env is None and token_file is None:
            raise ValueError("broker.remote_auth requires token_env or token_file when enabled")
        return cls(
            enabled=enabled,
            required=required,
            token_env=token_env,
            token_file=token_file,
        )


@dataclass(frozen=True)
class BrokerSettings:
    identity: BrokerIdentityConfig = field(default_factory=BrokerIdentityConfig)
    tool_namespace_separator: str = "."
    idle_timeout_seconds: int = DEFAULT_IDLE_TIMEOUT_SECONDS
    cpu_watchdog_percent: int = DEFAULT_CPU_WATCHDOG_PERCENT
    cpu_watchdog_seconds: int = DEFAULT_CPU_WATCHDOG_SECONDS
    remote_auth: RemoteBrokerAuthConfig = field(default_factory=RemoteBrokerAuthConfig)

    @classmethod
    def from_mapping(cls, data: dict[str, Any], *, runtime: RuntimeConfig) -> "BrokerSettings":
        _validate_keys("broker", data, BROKER_KEYS)
        return cls(
            identity=BrokerIdentityConfig.from_mapping(data.get("identity")),
            tool_namespace_separator=str(data.get("tool_namespace_separator", ".")),
            idle_timeout_seconds=int(data.get("idle_timeout_seconds", DEFAULT_IDLE_TIMEOUT_SECONDS)),
            cpu_watchdog_percent=int(data.get("cpu_watchdog_percent", DEFAULT_CPU_WATCHDOG_PERCENT)),
            cpu_watchdog_seconds=int(data.get("cpu_watchdog_seconds", DEFAULT_CPU_WATCHDOG_SECONDS)),
            remote_auth=RemoteBrokerAuthConfig.from_mapping(
                data.get("remote_auth"),
                runtime=runtime,
            ),
        )


@dataclass(frozen=True)
class UpstreamConfig:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    mode: str = "shared"
    transport: str = "stdio"
    enabled: bool = True
    working_dir: Path | None = None
    state_dir: str | None = None
    tool_prefix: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    env_files: dict[str, Path] = field(default_factory=dict)
    session_env: dict[str, str] = field(default_factory=dict)
    request_meta: dict[str, str] = field(default_factory=dict)
    purpose: str = ""
    tags: tuple[str, ...] = ()
    profiles: tuple[str, ...] = ("manual-test",)
    mutating: bool = False
    serialize_calls: bool = False
    strict_initialization: bool = False
    inject_cwd_project: bool = False
    inject_cwd_project_exclude: tuple[str, ...] = ()
    startup_timeout_seconds: int = 60
    restart: RestartPolicy = field(default_factory=RestartPolicy)
    health: HealthPolicy = field(default_factory=HealthPolicy)
    tool_timeouts: dict[str, int] = field(default_factory=dict)
    resources: ResourcePolicy = field(default_factory=ResourcePolicy)
    auth_repair: AuthRepairPolicy | None = None
    auth_probe: AuthProbePolicy | None = None
    smoke: SmokeProbe | None = None

    @classmethod
    def from_mapping(
        cls,
        name: str,
        data: dict[str, Any],
        *,
        runtime: RuntimeConfig,
    ) -> "UpstreamConfig":
        _validate_keys(f"upstreams.{name}", data, UPSTREAM_KEYS)
        if "command" not in data:
            raise ValueError(f"missing required config key: upstreams.{name}.command")
        env, env_files = _parse_upstream_environment(name, data, runtime=runtime)
        mode = parse_mode(f"upstreams.{name}.mode", data.get("mode", "shared"))
        session_env = _parse_session_env(
            f"upstreams.{name}.session_env",
            data.get("session_env", {}),
        )
        if session_env and mode != "per_session":
            raise ValueError(f"upstreams.{name}.session_env requires mode: per_session")
        return cls(
            name=name,
            command=_expand_config_text(str(data["command"]), runtime),
            args=_parse_upstream_args(name, data, runtime),
            mode=mode,
            transport=parse_transport(f"upstreams.{name}.transport", data.get("transport", "stdio")),
            enabled=bool(data.get("enabled", True)),
            working_dir=_parse_upstream_working_dir(name, data, runtime),
            state_dir=data.get("state_dir"),
            tool_prefix=str(data.get("tool_prefix", name)),
            env=env,
            env_files=env_files,
            session_env=session_env,
            request_meta=_parse_request_meta(
                f"upstreams.{name}.request_meta",
                data.get("request_meta", {}),
                configured_env_names=set(env) | set(env_files),
            ),
            purpose=str(data.get("purpose", "")),
            tags=_parse_string_tuple(f"upstreams.{name}.tags", data.get("tags", [])),
            profiles=parse_profiles(f"upstreams.{name}.profiles", data.get("profiles")),
            mutating=_parse_bool(f"upstreams.{name}.mutating", data.get("mutating", False)),
            serialize_calls=_parse_bool(
                f"upstreams.{name}.serialize_calls",
                data.get("serialize_calls", False),
            ),
            strict_initialization=_parse_bool(
                f"upstreams.{name}.strict_initialization",
                data.get("strict_initialization", False),
            ),
            inject_cwd_project=_parse_bool(
                f"upstreams.{name}.inject_cwd_project",
                data.get("inject_cwd_project", False),
            ),
            inject_cwd_project_exclude=_parse_string_tuple(
                f"upstreams.{name}.inject_cwd_project_exclude",
                data.get("inject_cwd_project_exclude", []),
            ),
            **_parse_upstream_policies(name, data),
            auth_repair=AuthRepairPolicy.from_mapping(
                f"upstreams.{name}.auth_repair",
                data.get("auth_repair"),
            ),
            auth_probe=_parse_auth_probe(
                f"upstreams.{name}.auth_probe",
                data.get("auth_probe"),
                runtime=runtime,
            ),
            smoke=SmokeProbe.from_mapping(f"upstreams.{name}.smoke", data.get("smoke")),
        )

    def call_timeout_for_tool(self, tool_name: str) -> int:
        return self.tool_timeouts.get(tool_name, self.health.call_timeout_seconds)

    def resolve_environment(self, environ: Mapping[str, str]) -> dict[str, str]:
        resolved: dict[str, str] = {}
        missing_env: list[str] = []
        missing_files: list[str] = []
        for target_name, source_name in self.env.items():
            value = environ.get(source_name)
            if value is None:
                missing_env.append(source_name)
            else:
                resolved[target_name] = value
        for target_name, secret_path in self.env_files.items():
            if not secret_path.exists():
                missing_files.append(str(secret_path))
                continue
            value = secret_path.read_text(encoding="utf-8").rstrip("\r\n")
            if value == "":
                missing_files.append(str(secret_path))
                continue
            resolved[target_name] = value
        if missing_env:
            joined = ", ".join(missing_env)
            raise ValueError(f"missing environment variable for upstream {self.name}: {joined}")
        if missing_files:
            joined = ", ".join(missing_files)
            raise ValueError(f"missing secret file for upstream {self.name}: {joined}")
        return resolved

    def resolve_session_environment(self, session_context: Mapping[str, str]) -> dict[str, str]:
        resolved: dict[str, str] = {}
        missing_sources: list[str] = []
        for target_name, source_name in self.session_env.items():
            value = session_context.get(source_name)
            if value is None:
                missing_sources.append(source_name)
            else:
                resolved[target_name] = value
        if missing_sources:
            joined = ", ".join(missing_sources)
            raise ValueError(f"missing session context for upstream {self.name}: {joined}")
        return resolved

    def resolve_request_meta(self, environ: Mapping[str, str]) -> dict[str, str]:
        if not self.request_meta:
            return {}
        resolved_env = self.resolve_environment(environ)
        return {
            meta_name: resolved_env[source_name]
            for meta_name, source_name in self.request_meta.items()
        }


@dataclass(frozen=True)
class BrokerConfig:
    runtime: RuntimeConfig
    broker: BrokerSettings
    upstreams: dict[str, UpstreamConfig]
    profiles: dict[str, ToolExposureProfile] = field(default_factory=dict)
    clients: dict[str, ClientRenderConfig] = field(default_factory=dict)

    @classmethod
    def from_file(cls, path: Path) -> "BrokerConfig":
        with path.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle)
        raw = {} if loaded is None else loaded
        if not isinstance(raw, dict):
            raise ValueError("broker config must be a mapping")
        _validate_keys("", raw, TOP_LEVEL_KEYS)
        _validate_schema_version(raw.get("schema_version"))
        runtime_raw = raw.get("runtime", {})
        broker_raw = raw.get("broker", {})
        profiles_raw = raw.get("profiles", {})
        upstreams_raw = raw.get("upstreams", {})
        clients_raw = raw.get("clients", {})
        if not isinstance(runtime_raw, dict):
            raise ValueError("runtime must be a mapping")
        if not isinstance(broker_raw, dict):
            raise ValueError("broker must be a mapping")
        if not isinstance(profiles_raw, dict):
            raise ValueError("profiles must be a mapping")
        if not isinstance(upstreams_raw, dict):
            raise ValueError("upstreams must be a mapping")
        if not isinstance(clients_raw, dict):
            raise ValueError("clients must be a mapping")
        runtime = RuntimeConfig.from_mapping(runtime_raw)
        return cls(
            runtime=runtime,
            broker=BrokerSettings.from_mapping(broker_raw, runtime=runtime),
            profiles={
                name: _parse_profile(name, profile)
                for name, profile in profiles_raw.items()
            },
            upstreams={
                name: UpstreamConfig.from_mapping(name, upstream, runtime=runtime)
                for name, upstream in upstreams_raw.items()
            },
            clients=_parse_clients(clients_raw, runtime),
        )

    def __post_init__(self) -> None:
        _validate_unique_profile_prefixes(self.upstreams)
        _validate_upstream_profile_references(self.upstreams, self.profiles)
        _validate_mutating_upstream_allowlists(self.upstreams, self.profiles)

    def identity_status_payload(self, *, active_profile: str | None) -> dict[str, object]:
        return self.broker.identity.status_payload(
            active_profile=active_profile,
            active_profiles=sorted(self.profiles),
        )


def _validate_unique_profile_prefixes(upstreams: dict[str, UpstreamConfig]) -> None:
    seen: dict[tuple[str, str], str] = {}
    for upstream_name, upstream in upstreams.items():
        if not upstream.enabled or upstream.mode == "disabled":
            continue
        prefix = upstream.tool_prefix or upstream.name
        for profile in upstream.profiles:
            key = (profile, prefix)
            previous = seen.get(key)
            if previous is not None:
                raise ValueError(f"duplicate tool prefix for profile {profile}: {prefix}")
            seen[key] = upstream_name


def _parse_env_names(path: str, value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be a mapping")
    parsed: dict[str, str] = {}
    for target_name, source_name in value.items():
        if not isinstance(target_name, str) or not ENV_NAME_PATTERN.match(target_name):
            raise ValueError(f"{path} keys must be environment variable names")
        if not isinstance(source_name, str) or not ENV_NAME_PATTERN.match(source_name):
            raise ValueError(f"{path}.{target_name} must name a host environment variable")
        parsed[target_name] = source_name
    return parsed


def _parse_optional_env_name(path: str, value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not ENV_NAME_PATTERN.match(value):
        raise ValueError(f"{path} must name a host environment variable")
    return value


def _parse_upstream_environment(
    name: str,
    data: dict[str, Any],
    *,
    runtime: RuntimeConfig,
) -> tuple[dict[str, str], dict[str, Path]]:
    env = _parse_env_names(f"upstreams.{name}.env", data.get("env", {}))
    env_files = _parse_env_files(
        f"upstreams.{name}.env_files",
        data.get("env_files", {}),
        runtime=runtime,
    )
    duplicate_env_targets = sorted(set(env) & set(env_files))
    if duplicate_env_targets:
        joined = ", ".join(duplicate_env_targets)
        raise ValueError(f"duplicate env source for upstream {name}: {joined}")
    return env, env_files


def _parse_session_env(path: str, value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be a mapping")
    parsed: dict[str, str] = {}
    for target_name, source_name in value.items():
        if not isinstance(target_name, str) or not ENV_NAME_PATTERN.match(target_name):
            raise ValueError(f"{path} keys must be environment variable names")
        if not isinstance(source_name, str) or source_name not in SESSION_ENV_SOURCES:
            raise ValueError(f"{path}.{target_name} must be one of: {SESSION_ENV_ALLOWED_MESSAGE}")
        parsed[target_name] = source_name
    return parsed


def _parse_upstream_args(
    name: str,
    data: dict[str, Any],
    runtime: RuntimeConfig,
) -> list[str]:
    args = data.get("args", [])
    if not isinstance(args, list):
        raise ValueError(f"upstreams.{name}.args must be a list")
    return [_expand_config_text(str(arg), runtime) for arg in args]


def _parse_upstream_working_dir(
    name: str,
    data: dict[str, Any],
    runtime: RuntimeConfig,
) -> Path | None:
    return _parse_optional_config_path(
        f"upstreams.{name}.working_dir",
        data.get("working_dir"),
        runtime,
    )


def _parse_request_meta(
    path: str,
    value: Any,
    *,
    configured_env_names: set[str],
) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be a mapping")
    parsed: dict[str, str] = {}
    for meta_name, source_name in value.items():
        if not isinstance(meta_name, str) or not META_NAME_PATTERN.fullmatch(meta_name):
            raise ValueError(f"{path} keys must be request metadata names")
        if not isinstance(source_name, str) or not ENV_NAME_PATTERN.match(source_name):
            raise ValueError(f"{path}.{meta_name} must name a configured environment variable")
        if source_name not in configured_env_names:
            raise ValueError(f"{path}.{meta_name} must reference env or env_files")
        parsed[meta_name] = source_name
    return parsed


class _UpstreamPolicies(TypedDict):
    startup_timeout_seconds: int
    restart: RestartPolicy
    health: HealthPolicy
    tool_timeouts: dict[str, int]
    resources: ResourcePolicy


def _parse_upstream_policies(name: str, data: dict[str, Any]) -> _UpstreamPolicies:
    return {
        "startup_timeout_seconds": parse_startup_timeout(
            f"upstreams.{name}.startup_timeout_seconds",
            data.get("startup_timeout_seconds"),
        ),
        "restart": RestartPolicy.from_mapping(f"upstreams.{name}.restart", data.get("restart")),
        "health": HealthPolicy.from_mapping(f"upstreams.{name}.health", data.get("health")),
        "tool_timeouts": _parse_tool_timeouts(
            f"upstreams.{name}.tool_timeouts",
            data.get("tool_timeouts", {}),
        ),
        "resources": ResourcePolicy.from_mapping(
            f"upstreams.{name}.resources",
            data.get("resources"),
        ),
    }


def _parse_tool_timeouts(path: str, value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be a mapping")
    parsed: dict[str, int] = {}
    for tool_name, timeout_seconds in value.items():
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError(f"{path} keys must be non-empty tool names")
        if isinstance(timeout_seconds, bool):
            raise ValueError(f"{path}.{tool_name} must be greater than 0")
        try:
            parsed_timeout = int(timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{path}.{tool_name} must be greater than 0") from exc
        if parsed_timeout <= 0:
            raise ValueError(f"{path}.{tool_name} must be greater than 0")
        parsed[tool_name] = parsed_timeout
    return parsed


def _parse_auth_probe(
    path: str,
    value: Any,
    *,
    runtime: RuntimeConfig,
) -> AuthProbePolicy | None:
    policy = AuthProbePolicy.from_mapping(path, value)
    if policy is None:
        return None
    return AuthProbePolicy(
        type=policy.type,
        token_file=Path(_expand_config_text(policy.token_file, runtime)).expanduser(),
        required_fields=policy.required_fields,
        refresh_token_expiry_field=policy.refresh_token_expiry_field,
    )


def _parse_env_files(path: str, value: Any, *, runtime: RuntimeConfig) -> dict[str, Path]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be a mapping")
    parsed: dict[str, Path] = {}
    for target_name, source_path in value.items():
        if not isinstance(target_name, str) or not ENV_NAME_PATTERN.match(target_name):
            raise ValueError(f"{path} keys must be environment variable names")
        parsed[target_name] = _parse_config_path(f"{path}.{target_name}", source_path, runtime)
    return parsed


def _expand_config_text(value: str | Path, runtime: RuntimeConfig) -> str:
    expanded = _expand_text(value)
    runtime_values = {
        "{runtime.root}": str(runtime.root),
        "{runtime.socket_path}": str(runtime.socket_path),
        "{runtime.log_dir}": str(runtime.log_dir),
        "{runtime.state_dir}": str(runtime.state_dir),
        "{runtime.secrets_dir}": str(runtime.secrets_dir),
    }
    for token, replacement in runtime_values.items():
        expanded = expanded.replace(token, replacement)
    return expanded


def _parse_string_tuple(path: str, value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{path} must be a list")
    parsed = tuple(str(item) for item in value)
    if any(not item for item in parsed):
        raise ValueError(f"{path} must contain non-empty strings")
    return parsed


def _parse_bool(path: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{path} must be a boolean")
    return value


def _parse_profile(name: str, value: Any) -> ToolExposureProfile:
    if not isinstance(value, dict):
        raise ValueError(f"profiles.{name} must be a mapping")
    _validate_keys(f"profiles.{name}", value, PROFILE_KEYS)
    return ToolExposureProfile.from_mapping(name, value)


def _parse_clients(
    clients_raw: dict[str, Any],
    runtime: RuntimeConfig,
) -> dict[str, ClientRenderConfig]:
    return {
        name: ClientRenderConfig.from_mapping(
            name,
            client,
            runtime=runtime,
            parse_path=_parse_path,
            expand_config_text=_expand_config_text,
            validate_keys=_validate_keys,
        )
        for name, client in clients_raw.items()
    }


def _validate_schema_version(value: Any) -> None:
    if value is None:
        return
    if value != 1:
        raise ValueError("schema_version must be 1")


def _validate_keys(path: str, data: dict[str, Any], allowed_keys: frozenset[str]) -> None:
    for key in data:
        if key not in allowed_keys:
            dotted = f"{path}.{key}" if path else str(key)
            raise ValueError(f"unknown config key: {dotted}")


def _validate_upstream_profile_references(
    upstreams: dict[str, UpstreamConfig],
    profiles: dict[str, ToolExposureProfile],
) -> None:
    if not profiles:
        return
    defined = set(profiles)
    for upstream_name, upstream in upstreams.items():
        for profile_name in upstream.profiles:
            if profile_name not in defined:
                raise ValueError(
                    f"upstreams.{upstream_name}.profiles references undefined profile: {profile_name}"
                )


def _validate_mutating_upstream_allowlists(
    upstreams: dict[str, UpstreamConfig],
    profiles: dict[str, ToolExposureProfile],
) -> None:
    if not profiles:
        return
    for upstream_name, upstream in upstreams.items():
        if not upstream.enabled or upstream.mode == "disabled" or not upstream.mutating:
            continue
        for profile_name in upstream.profiles:
            profile = profiles[profile_name]
            if not profile.allows_mutating_upstream(upstream_name):
                raise ValueError(
                    f"mutating upstream {upstream_name} requires profile allowlist entry: "
                    f"{profile_name}"
                )
