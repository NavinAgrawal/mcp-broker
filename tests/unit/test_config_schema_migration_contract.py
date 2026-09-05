from pathlib import Path
from typing import Any
import json

import pytest

from tests.support.repo_paths import repo_root


pytestmark = pytest.mark.unit

ROOT = repo_root()
SCHEMA_FILE = ROOT / "config" / "broker.schema.json"


def test_legacy_config_without_schema_version_defaults_forward(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig

    config_file = tmp_path / "legacy-broker.yaml"
    config_file.write_text(
        """
runtime:
  root: /tmp/mcp-broker-test
broker: {}
upstreams:
  read-store:
    command: read-store
""".strip(),
        encoding="utf-8",
    )

    config = BrokerConfig.from_file(config_file)

    assert config.runtime.root == Path("/tmp/mcp-broker-test")
    assert config.runtime.socket_path == Path("/tmp/mcp-broker-test/sockets/broker.sock")
    assert config.broker.tool_namespace_separator == "."
    assert config.upstreams["read-store"].mode == "shared"
    assert config.upstreams["read-store"].transport == "stdio"


def test_future_schema_version_fails_with_specific_error(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig

    config_file = tmp_path / "future-broker.yaml"
    config_file.write_text(
        """
schema_version: 2
runtime:
  root: /tmp/mcp-broker-test
upstreams: {}
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="schema_version must be 1"):
        BrokerConfig.from_file(config_file)


def test_broker_identity_rejects_non_mapping_and_unknown_keys() -> None:
    from mcp_broker.config_identity import BrokerIdentityConfig

    with pytest.raises(ValueError, match="broker.identity must be a mapping"):
        BrokerIdentityConfig.from_mapping([])

    with pytest.raises(ValueError, match="unknown config key: broker.identity.unknown"):
        BrokerIdentityConfig.from_mapping({"unknown": "value"})


def test_full_v1_config_exercises_every_schema_field(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig

    config_file = tmp_path / "full-broker.yaml"
    config_file.write_text(
        """
schema_version: 1
runtime:
  root: /tmp/mcp-broker-test
  socket_path: /tmp/mcp-broker-test/sockets/broker.sock
  log_dir: /tmp/mcp-broker-test/logs
  state_dir: /tmp/mcp-broker-test/state
  secrets_dir: /tmp/mcp-broker-test/secrets
broker:
  identity:
    broker_id: engineer-laptop
    environment: local
    bundle_version: unbundled
  tool_namespace_separator: "."
  idle_timeout_seconds: 900
  cpu_watchdog_percent: 80
  cpu_watchdog_seconds: 10
  remote_auth:
    enabled: true
    required: true
    token_file: "{runtime.secrets_dir}/broker-remote-token"
profiles:
  codex:
    max_tools: 80
    compact_tools_enabled: true
    broker_tool_name_style: snake
    allow_mutating_upstreams:
      - writer
clients:
  codex:
    format: codex-toml
    config_path: /tmp/mcp-broker-test/codex.toml
    entry_name: mcp-broker
    command: mcp-broker-client
    args:
      - --socket-path
      - "{runtime.socket_path}"
    mcp_allowed_servers:
      - mcp-broker
    backup_paths:
      - /tmp/mcp-broker-test/codex.backup.toml
    codex_apps_policy:
      enabled: true
      app_directory_globs:
        - /tmp/mcp-broker-test/apps/*.json
      tools_cache_globs:
        - /tmp/mcp-broker-test/tools/*.json
      disable_connectors:
        - id: remote-repo
          name: GitHub
          reason: Broker owns GitHub.
upstreams:
  writer:
    enabled: true
    mode: per_session
    transport: stdio
    purpose: migration fixture
    tags:
      - migration
    tool_prefix: writer
    command: writer
    args:
      - --state
      - "{runtime.state_dir}"
    working_dir: /tmp/mcp-broker-test/work
    state_dir: upstreams/writer
    profiles:
      - codex
    env:
      API_TOKEN: API_TOKEN
    env_files:
      FILE_TOKEN: "{runtime.secrets_dir}/FILE_TOKEN"
    session_env:
      PROJECT_DIR: client_cwd
    request_meta:
      authToken: FILE_TOKEN
    mutating: true
    serialize_calls: true
    inject_cwd_project: true
    inject_cwd_project_exclude:
      - list_projects
    startup_timeout_seconds: 60
    restart:
      max_attempts: 3
      backoff_seconds: 2
    health:
      ready_timeout_seconds: 10
      call_timeout_seconds: 60
      http_retry_attempts: 2
      http_retry_backoff_seconds: 0
    tool_timeouts:
      create-draft-email: 300
    resources:
      idle_timeout_seconds: 900
      cpu_watchdog_percent: 80
      cpu_watchdog_seconds: 10
      memory_ceiling_mb: 512
    auth_repair:
      tool: setup_auth
      arguments:
        show_browser: false
      trigger_errors:
        - "Not authenticated"
      retry_original: true
      timeout_seconds: 300
    auth_probe:
      type: oauth_token_file
      token_file: "{runtime.secrets_dir}/oauth.json"
      required_fields:
        - access_token
        - refresh_token
      refresh_token_expiry_field: refresh_token_expires_at
    smoke:
      query: writer status
      tool: writer.status
      arguments: {}
      call: true
""".strip(),
        encoding="utf-8",
    )

    config = BrokerConfig.from_file(config_file)

    assert config.runtime.secrets_dir == Path("/tmp/mcp-broker-test/secrets")
    assert config.broker.identity.broker_id == "engineer-laptop"
    assert config.broker.identity.environment == "local"
    assert config.broker.identity.bundle_version == "unbundled"
    assert config.profiles["codex"].allows_mutating_upstream("writer")
    assert config.profiles["codex"].broker_tool_name_style == "snake"
    assert config.broker.remote_auth.enabled is True
    assert config.broker.remote_auth.token_file == Path(
        "/tmp/mcp-broker-test/secrets/broker-remote-token"
    )
    assert config.clients["codex"].codex_apps_policy is not None
    assert config.clients["codex"].codex_apps_policy.enabled is True
    assert config.clients["codex"].mcp_allowed_servers == ("mcp-broker",)
    assert config.upstreams["writer"].mutating is True
    assert config.upstreams["writer"].inject_cwd_project is True
    assert config.upstreams["writer"].inject_cwd_project_exclude == ("list_projects",)
    assert config.upstreams["writer"].auth_repair is not None
    assert config.upstreams["writer"].auth_probe is not None
    assert config.upstreams["writer"].health.http_retry_attempts == 2
    assert config.upstreams["writer"].health.http_retry_backoff_seconds == 0
    assert config.upstreams["writer"].tool_timeouts == {"create-draft-email": 300}
    assert config.upstreams["writer"].smoke is not None
    assert config.upstreams["writer"].smoke.tool == "writer.status"


def test_schema_field_inventory_matches_migration_fixture() -> None:
    schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
    fields = _schema_fields(schema)

    assert fields == {
        "broker.cpu_watchdog_percent",
        "broker.cpu_watchdog_seconds",
        "broker.identity",
        "broker.identity.broker_id",
        "broker.identity.bundle_version",
        "broker.identity.environment",
        "broker.idle_timeout_seconds",
        "broker.remote_auth",
        "broker.remote_auth.enabled",
        "broker.remote_auth.required",
        "broker.remote_auth.token_env",
        "broker.remote_auth.token_file",
        "broker.socket_max_request_bytes",
        "broker.socket_read_timeout_seconds",
        "broker.tool_namespace_separator",
        "broker",
        "clients",
        "clients.*.args",
        "clients.*.backup_paths",
        "clients.*.codex_apps_policy",
        "clients.*.codex_apps_policy.app_directory_globs",
        "clients.*.codex_apps_policy.disable_connectors",
        "clients.*.codex_apps_policy.disable_connectors[].id",
        "clients.*.codex_apps_policy.disable_connectors[].name",
        "clients.*.codex_apps_policy.disable_connectors[].reason",
        "clients.*.codex_apps_policy.enabled",
        "clients.*.codex_apps_policy.tools_cache_globs",
        "clients.*.command",
        "clients.*.config_path",
        "clients.*.entry_name",
        "clients.*.format",
        "clients.*.mcp_allowed_servers",
        "profiles.*.allow_mutating_upstreams",
        "profiles.*.broker_tool_name_style",
        "profiles.*.client_root_match",
        "profiles.*.compact_tools_enabled",
        "profiles.*.max_tools",
        "profiles",
        "runtime",
        "runtime.log_dir",
        "runtime.root",
        "runtime.secrets_dir",
        "runtime.socket_path",
        "runtime.state_dir",
        "schema_version",
        "upstreams",
        "upstreams.*.args",
        "upstreams.*.auth_repair",
        "upstreams.*.auth_repair.arguments",
        "upstreams.*.auth_repair.retry_original",
        "upstreams.*.auth_repair.timeout_seconds",
        "upstreams.*.auth_repair.tool",
        "upstreams.*.auth_repair.trigger_errors",
        "upstreams.*.auth_probe",
        "upstreams.*.auth_probe.refresh_token_expiry_field",
        "upstreams.*.auth_probe.required_fields",
        "upstreams.*.auth_probe.token_file",
        "upstreams.*.auth_probe.type",
        "upstreams.*.command",
        "upstreams.*.enabled",
        "upstreams.*.env",
        "upstreams.*.env_files",
        "upstreams.*.health",
        "upstreams.*.health.call_timeout_seconds",
        "upstreams.*.health.http_retry_attempts",
        "upstreams.*.health.http_retry_backoff_seconds",
        "upstreams.*.health.ready_timeout_seconds",
        "upstreams.*.inject_cwd_project",
        "upstreams.*.inject_cwd_project_exclude",
        "upstreams.*.mode",
        "upstreams.*.mutating",
        "upstreams.*.profiles",
        "upstreams.*.purpose",
        "upstreams.*.request_meta",
        "upstreams.*.session_env",
        "upstreams.*.resources",
        "upstreams.*.resources.cpu_watchdog_percent",
        "upstreams.*.resources.cpu_watchdog_seconds",
        "upstreams.*.resources.idle_timeout_seconds",
        "upstreams.*.resources.memory_ceiling_mb",
        "upstreams.*.restart",
        "upstreams.*.restart.backoff_seconds",
        "upstreams.*.restart.max_attempts",
        "upstreams.*.serialize_calls",
        "upstreams.*.strict_initialization",
        "upstreams.*.smoke",
        "upstreams.*.smoke.arguments",
        "upstreams.*.smoke.call",
        "upstreams.*.smoke.query",
        "upstreams.*.smoke.tool",
        "upstreams.*.startup_timeout_seconds",
        "upstreams.*.state_dir",
        "upstreams.*.tags",
        "upstreams.*.tool_timeouts",
        "upstreams.*.tool_prefix",
        "upstreams.*.transport",
        "upstreams.*.working_dir",
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("broker_id", "", "broker.identity.broker_id must be a non-empty string"),
        ("environment", "", "broker.identity.environment must be a non-empty string"),
        ("bundle_version", "", "broker.identity.bundle_version must be a non-empty string"),
    ],
)
def test_broker_identity_rejects_empty_fields(
    tmp_path: Path,
    field: str,
    value: str,
    message: str,
) -> None:
    from mcp_broker.config import BrokerConfig

    config_file = tmp_path / "invalid-identity.yaml"
    config_file.write_text(
        f"""
schema_version: 1
runtime:
  root: /tmp/mcp-broker-test
broker:
  identity:
    broker_id: local-broker
    environment: local
    bundle_version: unbundled
upstreams: {{}}
""".replace(f"{field}: local-broker", f"{field}: {value}")
        .replace(f"{field}: local", f"{field}: {value}")
        .replace(f"{field}: unbundled", f"{field}: {value}")
        .strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=message):
        BrokerConfig.from_file(config_file)


def test_broker_identity_config_rejects_non_mapping_input() -> None:
    from mcp_broker.config_identity import BrokerIdentityConfig

    with pytest.raises(ValueError, match="broker.identity must be a mapping"):
        BrokerIdentityConfig.from_mapping([])


def test_broker_identity_config_rejects_unknown_keys() -> None:
    from mcp_broker.config_identity import BrokerIdentityConfig

    with pytest.raises(ValueError, match="unknown config key: broker.identity.extra"):
        BrokerIdentityConfig.from_mapping({"extra": "value"})


def _schema_fields(schema: dict[str, Any]) -> set[str]:
    definitions = schema["$defs"]
    fields = set(schema["properties"])
    for section in ["runtime"]:
        fields.update(f"{section}.{field}" for field in definitions[section]["properties"])
    fields.update(_prefixed_fields("broker", definitions["broker"], definitions))
    fields.update(f"profiles.*.{field}" for field in definitions["profile"]["properties"])
    fields.update(_prefixed_fields("clients.*", definitions["client"], definitions))
    fields.update(_prefixed_fields("upstreams.*", definitions["upstream"], definitions))
    return fields


def _prefixed_fields(prefix: str, definition: dict[str, Any], definitions: dict[str, Any]) -> set[str]:
    fields = set()
    for field, spec in definition["properties"].items():
        field_path = f"{prefix}.{field}"
        fields.add(field_path)
        reference = spec.get("$ref")
        if reference:
            name = reference.rsplit("/", 1)[-1]
            nested = definitions[name]
            if "properties" in nested:
                nested_prefix = f"{prefix}.{field}"
                fields.update(_nested_fields(nested_prefix, nested, definitions))
    return fields


def _nested_fields(prefix: str, definition: dict[str, Any], definitions: dict[str, Any]) -> set[str]:
    fields = set()
    for field, spec in definition["properties"].items():
        field_path = f"{prefix}.{field}"
        fields.add(field_path)
        reference = spec.get("$ref")
        if reference:
            name = reference.rsplit("/", 1)[-1]
            nested = definitions[name]
            if "properties" in nested:
                fields.update(_nested_fields(field_path, nested, definitions))
        if spec.get("type") == "array" and "$ref" in spec.get("items", {}):
            name = spec["items"]["$ref"].rsplit("/", 1)[-1]
            nested = definitions[name]
            fields.update(_nested_fields(f"{field_path}[]", nested, definitions))
    return fields
