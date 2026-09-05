from pathlib import Path
import pytest
from tests.support.repo_paths import repo_root
pytestmark = pytest.mark.unit
ROOT = repo_root()

def test_broker_config_loads_runtime_and_upstream_paths_from_yaml(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig

    config_file = tmp_path / "broker.yaml"
    config_file.write_text(
        """
runtime:
  root: /tmp/mcp-broker-test
  socket_path: /tmp/mcp-broker-test/sockets/broker.sock
  log_dir: /tmp/mcp-broker-test/logs
  state_dir: /tmp/mcp-broker-test/state
  secrets_dir: /tmp/mcp-broker-test/secrets
broker:
  tool_namespace_separator: "."
  idle_timeout_seconds: 900
  socket_read_timeout_seconds: 45
  socket_max_request_bytes: 16777216
  cpu_watchdog_percent: 80
  cpu_watchdog_seconds: 10
upstreams:
  read-store:
    command: /tmp/mcp/vendor/read-store-mcp/dist/index.js
    args: []
    mode: shared
    enabled: true
    state_dir: upstreams/read-store
    tool_prefix: read-store
    strict_initialization: true
""".strip(),
        encoding="utf-8",
    )

    config = BrokerConfig.from_file(config_file)

    assert config.runtime.root == Path("/tmp/mcp-broker-test")
    assert config.runtime.socket_path == Path("/tmp/mcp-broker-test/sockets/broker.sock")
    assert config.broker.cpu_watchdog_percent == 80
    assert config.broker.socket_read_timeout_seconds == 45
    assert config.broker.socket_max_request_bytes == 16_777_216
    assert config.upstreams["read-store"].mode == "shared"
    assert config.upstreams["read-store"].tool_prefix == "read-store"
    assert config.upstreams["read-store"].strict_initialization is True

def test_broker_config_derives_runtime_child_paths_from_root(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig

    runtime_root = tmp_path / "runtime"
    config_file = tmp_path / "broker.yaml"
    config_file.write_text(
        f"""
runtime:
  root: {runtime_root}
broker: {{}}
upstreams: {{}}
""".strip(),
        encoding="utf-8",
    )

    config = BrokerConfig.from_file(config_file)

    assert config.runtime.socket_path == runtime_root / "sockets" / "broker.sock"
    assert config.runtime.log_dir == runtime_root / "logs"
    assert config.runtime.state_dir == runtime_root / "state"
    assert config.runtime.secrets_dir == runtime_root / "secrets"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("socket_read_timeout_seconds", 0),
        ("socket_max_request_bytes", 0),
        ("socket_read_timeout_seconds", True),
        ("socket_max_request_bytes", False),
        ("socket_read_timeout_seconds", "not-an-integer"),
        ("socket_max_request_bytes", None),
    ],
)
def test_broker_settings_rejects_nonpositive_socket_limits(field: str, value: object) -> None:
    from mcp_broker.config import BrokerSettings

    with pytest.raises(ValueError, match=f"broker.{field} must be greater than 0"):
        BrokerSettings(**{field: value})


def test_broker_settings_accepts_one_as_socket_limit_boundary() -> None:
    from mcp_broker.config import BrokerSettings

    settings = BrokerSettings(socket_read_timeout_seconds=1, socket_max_request_bytes=1)

    assert settings.socket_read_timeout_seconds == 1
    assert settings.socket_max_request_bytes == 1


def test_broker_config_rejects_invalid_upstream_tags(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig

    config_file = tmp_path / "broker.yaml"
    config_file.write_text(
        """
runtime:
  root: /tmp/mcp-broker-test
broker: {}
upstreams:
  read-store:
    command: read-store
    tags: read-store
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="upstreams.read-store.tags must be a list"):
        BrokerConfig.from_file(config_file)

    config_file.write_text(
        """
runtime:
  root: /tmp/mcp-broker-test
broker: {}
upstreams:
  read-store:
    command: read-store
    tags:
      - ""
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="upstreams.read-store.tags must contain non-empty strings"):
        BrokerConfig.from_file(config_file)

def test_broker_config_expands_home_and_environment_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_broker.config import BrokerConfig

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MCP_RUNTIME", "$HOME/mcp/mcp-broker")
    config_file = tmp_path / "broker.yaml"
    config_file.write_text(
        """
runtime:
  root: $MCP_RUNTIME
  socket_path: $MCP_RUNTIME/sockets/broker.sock
broker: {}
upstreams: {}
""".strip(),
        encoding="utf-8",
    )

    config = BrokerConfig.from_file(config_file)

    assert config.runtime.root == home / "mcp" / "mcp-broker"
    assert config.runtime.socket_path == home / "mcp" / "mcp-broker" / "sockets" / "broker.sock"

def test_broker_config_expands_upstream_command_and_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_broker.config import BrokerConfig

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TOOL_ROOT", "$HOME/mcp/local")
    config_file = tmp_path / "broker.yaml"
    config_file.write_text(
        """
runtime:
  root: /tmp/mcp-broker-test
broker: {}
upstreams:
  read-store:
    command: $TOOL_ROOT/read-store-mcp/bin/read-store-mcp-launcher.sh
    args:
      - $TOOL_ROOT/read-store-mcp
    mode: shared
    enabled: true
    state_dir: upstreams/read-store
    tool_prefix: read-store
""".strip(),
        encoding="utf-8",
    )

    config = BrokerConfig.from_file(config_file)

    assert config.upstreams["read-store"].command == str(
        home / "mcp" / "local" / "read-store-mcp" / "bin" / "read-store-mcp-launcher.sh"
    )
    assert config.upstreams["read-store"].args == [str(home / "mcp" / "local" / "read-store-mcp")]

def test_broker_config_parses_upstream_smoke_probe(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig

    config_file = tmp_path / "broker.yaml"
    config_file.write_text(
        """
runtime:
  root: /tmp/mcp-broker-test
broker: {}
upstreams:
  read-store:
    command: read-store
    profiles:
      - codex
    smoke:
      query: read-store scope
      tool: read-store.get_project_scope
      arguments:
        project: demo
      call: true
""".strip(),
        encoding="utf-8",
    )

    config = BrokerConfig.from_file(config_file)

    assert config.upstreams["read-store"].smoke is not None
    assert config.upstreams["read-store"].smoke.query == "read-store scope"
    assert config.upstreams["read-store"].smoke.tool == "read-store.get_project_scope"
    assert config.upstreams["read-store"].smoke.arguments == {"project": "demo"}
    assert config.upstreams["read-store"].smoke.call is True

def test_broker_config_rejects_invalid_upstream_smoke_probe(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig

    config_file = tmp_path / "broker.yaml"
    base_config = """
runtime:
  root: /tmp/mcp-broker-test
broker: {{}}
upstreams:
  read-store:
    command: read-store
    smoke:
      {body}
""".strip()

    config_file.write_text(
        base_config.format(
            body="""
      query: read-store
      tool: read-store.get_project_scope
      arguments: []
""".rstrip()
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="upstreams.read-store.smoke.arguments must be a mapping"):
        BrokerConfig.from_file(config_file)

    config_file.write_text(
        base_config.format(
            body="""
      query: ""
      tool: read-store.get_project_scope
      arguments: {}
""".rstrip()
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="upstreams.read-store.smoke.query must be a non-empty string"):
        BrokerConfig.from_file(config_file)

    config_file.write_text(
        base_config.format(
            body="""
      query: read-store
      tool: ""
      arguments: {}
""".rstrip()
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="upstreams.read-store.smoke.tool must be a non-empty string"):
        BrokerConfig.from_file(config_file)

    config_file.write_text(
        base_config.format(
            body="""
      query: read-store
      tool: read-store.get_project_scope
      arguments: {}
      call: "yes"
""".rstrip()
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="upstreams.read-store.smoke.call must be a boolean"):
        BrokerConfig.from_file(config_file)

def test_broker_config_expands_client_render_command_and_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_broker.config import BrokerConfig

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    config_file = tmp_path / "broker.yaml"
    config_file.write_text(
        """
runtime:
  root: $HOME/mcp/mcp-broker
clients:
  codex:
    format: codex-toml
    config_path: $HOME/.codex/config.toml
    command: mcp-broker-client
    args:
      - --socket-path
      - "{runtime.socket_path}"
upstreams: {}
""".strip(),
        encoding="utf-8",
    )

    config = BrokerConfig.from_file(config_file)

    assert config.clients["codex"].command == "mcp-broker-client"
    assert config.clients["codex"].args == (
        "--socket-path",
        str(home / "mcp/mcp-broker/sockets/broker.sock"),
    )

def test_broker_config_expands_client_related_backup_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_broker.config import BrokerConfig

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    config_file = tmp_path / "broker.yaml"
    config_file.write_text(
        """
runtime:
  root: $HOME/mcp/mcp-broker
clients:
  claude:
    format: claude-json
    config_path: $HOME/.claude.json
    backup_paths:
      - $HOME/.claude/settings.json
upstreams: {}
""".strip(),
        encoding="utf-8",
    )

    config = BrokerConfig.from_file(config_file)

    assert config.clients["claude"].backup_paths == (home / ".claude" / "settings.json",)

def test_broker_config_parses_codex_apps_policy(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig

    config_path = tmp_path / "broker.yaml"
    config_path.write_text(
        f"""
runtime:
  root: {tmp_path}/runtime
  socket_path: {tmp_path}/runtime/sockets/broker.sock
clients:
  llm-client:
    format: codex-toml
    config_path: {tmp_path}/codex.toml
    codex_apps_policy:
      enabled: true
      app_directory_globs:
        - {tmp_path}/codex-cache/app-directory/*.json
      tools_cache_globs:
        - {tmp_path}/codex-cache/tools/*.json
      disable_connectors:
        - id: connector_github
          name: GitHub
          reason: Broker owns GitHub across LLM clients.
        - name: Figma
          reason: Broker owns Figma across LLM clients.
upstreams: {{}}
""",
        encoding="utf-8",
    )

    config = BrokerConfig.from_file(config_path)

    policy = config.clients["llm-client"].codex_apps_policy
    assert policy is not None
    assert policy.enabled is True
    assert policy.app_directory_globs == (str(tmp_path / "codex-cache" / "app-directory" / "*.json"),)
    assert policy.tools_cache_globs == (str(tmp_path / "codex-cache" / "tools" / "*.json"),)
    assert [(selector.id, selector.name) for selector in policy.disable_connectors] == [
        ("connector_github", "GitHub"),
        (None, "Figma"),
    ]

@pytest.mark.parametrize(
    ("policy_yaml", "expected_error"),
    [
        ("codex_apps_policy: []", "clients.llm-client.codex_apps_policy must be a mapping"),
        (
            "codex_apps_policy:\n      disable_connectors: bad",
            "clients.llm-client.codex_apps_policy.disable_connectors",
        ),
        (
            "codex_apps_policy:\n      enabled: true\n      disable_connectors: []",
            "clients.llm-client.codex_apps_policy.disable_connectors must contain",
        ),
        (
            "codex_apps_policy:\n      disable_connectors:\n        - bad",
            "clients.llm-client.codex_apps_policy.disable_connectors\\[0\\] must be a mapping",
        ),
        (
            "codex_apps_policy:\n      disable_connectors:\n        - id: ''",
            "clients.llm-client.codex_apps_policy.disable_connectors\\[0\\].id",
        ),
        (
            "codex_apps_policy:\n      disable_connectors:\n        - name: ''",
            "clients.llm-client.codex_apps_policy.disable_connectors\\[0\\].name",
        ),
        (
            "codex_apps_policy:\n      disable_connectors:\n        - reason: no selector",
            "clients.llm-client.codex_apps_policy.disable_connectors\\[0\\] must define id or name",
        ),
        (
            "codex_apps_policy:\n      disable_connectors:\n        - id: connector_github\n          reason: 1",
            "clients.llm-client.codex_apps_policy.disable_connectors\\[0\\].reason",
        ),
        (
            "codex_apps_policy:\n      enabled: 1\n      disable_connectors:\n        - id: connector_github",
            "clients.llm-client.codex_apps_policy.enabled",
        ),
    ],
)
def test_broker_config_rejects_invalid_codex_apps_policy(
    tmp_path: Path,
    policy_yaml: str,
    expected_error: str,
) -> None:
    from mcp_broker.config import BrokerConfig

    config_path = tmp_path / "broker.yaml"
    config_path.write_text(
        f"""
runtime:
  root: {tmp_path}/runtime
  socket_path: {tmp_path}/runtime/sockets/broker.sock
clients:
  llm-client:
    format: codex-toml
    config_path: {tmp_path}/codex.toml
    {policy_yaml}
upstreams: {{}}
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=expected_error):
        BrokerConfig.from_file(config_path)

def test_broker_config_allows_duplicate_prefixes_for_disabled_upstreams(
    tmp_path: Path,
) -> None:
    from mcp_broker.config import BrokerConfig

    config_path = tmp_path / "broker.yaml"
    config_path.write_text(
        f"""
runtime:
  root: {tmp_path}/runtime
upstreams:
  active-search:
    command: search
    tool_prefix: search
    profiles: [codex]
  off-search:
    command: search
    enabled: false
    tool_prefix: search
    profiles: [codex]
  mode-disabled-search:
    command: search
    mode: disabled
    tool_prefix: search
    profiles: [codex]
""".strip(),
        encoding="utf-8",
    )

    config = BrokerConfig.from_file(config_path)

    assert set(config.upstreams) == {
        "active-search",
        "off-search",
        "mode-disabled-search",
    }

def test_broker_config_checks_duplicate_prefixes_after_disabled_upstream(
    tmp_path: Path,
) -> None:
    from mcp_broker.config import BrokerConfig

    config_path = tmp_path / "broker.yaml"
    config_path.write_text(
        f"""
runtime:
  root: {tmp_path}/runtime
upstreams:
  off-search:
    command: search
    enabled: false
    tool_prefix: search
    profiles: [codex]
  first-search:
    command: search
    tool_prefix: search
    profiles: [codex]
  second-search:
    command: search
    tool_prefix: search
    profiles: [codex]
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate tool prefix for profile codex: search"):
        BrokerConfig.from_file(config_path)

def test_broker_config_skips_disabled_mutating_upstream_allowlist_checks(
    tmp_path: Path,
) -> None:
    from mcp_broker.config import BrokerConfig

    config_path = tmp_path / "broker.yaml"
    config_path.write_text(
        f"""
runtime:
  root: {tmp_path}/runtime
profiles:
  codex:
    max_tools: 10
    allow_mutating_upstreams: []
upstreams:
  off-writer:
    command: writer
    enabled: false
    mutating: true
    profiles: [codex]
  mode-disabled-writer:
    command: writer
    mode: disabled
    mutating: true
    profiles: [codex]
""".strip(),
        encoding="utf-8",
    )

    config = BrokerConfig.from_file(config_path)

    assert set(config.upstreams) == {"off-writer", "mode-disabled-writer"}
