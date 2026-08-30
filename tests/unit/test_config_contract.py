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


def test_broker_config_checks_mutating_allowlists_after_skipped_upstream(
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
  off-reader:
    command: reader
    enabled: false
    profiles: [codex]
  active-writer:
    command: writer
    mutating: true
    profiles: [codex]
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="mutating upstream active-writer requires profile allowlist entry: codex",
    ):
        BrokerConfig.from_file(config_path)


def test_broker_config_expands_upstream_working_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_broker.config import BrokerConfig

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WORKSPACE_ROOT", "$HOME/workspaces")
    config_file = tmp_path / "broker.yaml"
    config_file.write_text(
        """
runtime:
  root: $HOME/mcp/mcp-broker
upstreams:
  knowledge-service:
    command: python
    args:
      - -m
      - src
    working_dir: $WORKSPACE_ROOT/knowledge-service
    state_dir: upstreams/knowledge-service
    tool_prefix: knowledge
""".strip(),
        encoding="utf-8",
    )

    config = BrokerConfig.from_file(config_file)

    assert config.upstreams["knowledge-service"].working_dir == home / (
        "workspaces/knowledge-service"
    )


def test_broker_config_expands_runtime_placeholders_in_upstream_paths(
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
upstreams:
  file-auth:
    command: "{runtime.root}/bin/file-auth"
    args:
      - "{runtime.state_dir}/bootstrap.json"
    working_dir: "{runtime.root}/vendor/file-auth"
    env_files:
      FILE_AUTH_TOKEN: "{runtime.secrets_dir}/FILE_AUTH_TOKEN"
""".strip(),
        encoding="utf-8",
    )

    config = BrokerConfig.from_file(config_file)

    assert config.upstreams["file-auth"].command == str(home / "mcp/mcp-broker/bin/file-auth")
    assert config.upstreams["file-auth"].args == [
        str(home / "mcp/mcp-broker/state/bootstrap.json")
    ]
    assert config.upstreams["file-auth"].working_dir == home / "mcp/mcp-broker/vendor/file-auth"
    assert config.upstreams["file-auth"].env_files == {
        "FILE_AUTH_TOKEN": home / "mcp/mcp-broker/secrets/FILE_AUTH_TOKEN"
    }


def test_broker_config_expands_runtime_log_dir_placeholder(
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
upstreams:
  file-auth:
    command: "{runtime.log_dir}/file-auth"
""".strip(),
        encoding="utf-8",
    )

    config = BrokerConfig.from_file(config_file)

    assert config.upstreams["file-auth"].command == str(
        home / "mcp/mcp-broker/logs/file-auth"
    )


def test_broker_config_rejects_duplicate_env_sources_with_sorted_targets(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig

    config_file = tmp_path / "broker.yaml"
    config_file.write_text(
        f"""
runtime:
  root: {tmp_path}/runtime
upstreams:
  file-auth:
    command: file-auth
    env:
      B_TOKEN: HOST_B_TOKEN
      A_TOKEN: HOST_A_TOKEN
    env_files:
      B_TOKEN: "{{runtime.secrets_dir}}/B_TOKEN"
      A_TOKEN: "{{runtime.secrets_dir}}/A_TOKEN"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="duplicate env source for upstream file-auth: A_TOKEN, B_TOKEN",
    ):
        BrokerConfig.from_file(config_file)


def test_broker_config_rejects_invalid_session_env_source_with_allowed_name(
    tmp_path: Path,
) -> None:
    from mcp_broker.config import BrokerConfig

    config_file = tmp_path / "broker.yaml"
    config_file.write_text(
        f"""
runtime:
  root: {tmp_path}/runtime
upstreams:
  session-auth:
    command: session-auth
    mode: per_session
    session_env:
      CLIENT_CWD: request_cwd
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="upstreams.session-auth.session_env.CLIENT_CWD must be one of: client_cwd",
    ):
        BrokerConfig.from_file(config_file)


def test_broker_config_rejects_invalid_startup_timeout_with_path(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig

    config_file = tmp_path / "broker.yaml"
    config_file.write_text(
        f"""
runtime:
  root: {tmp_path}/runtime
upstreams:
  file-auth:
    command: file-auth
    startup_timeout_seconds: 0
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="upstreams.file-auth.startup_timeout_seconds must be greater than 0",
    ):
        BrokerConfig.from_file(config_file)


def test_broker_config_expands_auth_probe_token_file_from_runtime(
    tmp_path: Path,
) -> None:
    from mcp_broker.config import BrokerConfig

    config_file = tmp_path / "broker.yaml"
    config_file.write_text(
        f"""
runtime:
  root: {tmp_path}/runtime
upstreams:
  file-auth:
    command: file-auth
    auth_probe:
      type: oauth_token_file
      token_file: "{{runtime.secrets_dir}}/oauth-token.json"
""".strip(),
        encoding="utf-8",
    )

    config = BrokerConfig.from_file(config_file)

    assert config.upstreams["file-auth"].auth_probe is not None
    assert config.upstreams["file-auth"].auth_probe.token_file == (
        tmp_path / "runtime/secrets/oauth-token.json"
    )


def test_broker_config_rejects_unsupported_schema_version_with_exact_message(
    tmp_path: Path,
) -> None:
    from mcp_broker.config import BrokerConfig

    config_file = tmp_path / "broker.yaml"
    config_file.write_text(
        f"""
schema_version: 2
runtime:
  root: {tmp_path}/runtime
upstreams: {{}}
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="^schema_version must be 1$"):
        BrokerConfig.from_file(config_file)


def test_upstream_request_meta_sources_from_configured_env_file(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig

    token_file = tmp_path / "runtime" / "secrets" / "NLMCP_AUTH_TOKEN"
    token_file.parent.mkdir(parents=True)
    token_file.write_text("secret-token\n", encoding="utf-8")
    config_file = tmp_path / "broker.yaml"
    config_file.write_text(
        f"""
schema_version: 1
runtime:
  root: {tmp_path / "runtime"}
upstreams:
  notebook:
    command: npx
    env_files:
      NLMCP_AUTH_TOKEN: "{{runtime.secrets_dir}}/NLMCP_AUTH_TOKEN"
    request_meta:
      authToken: NLMCP_AUTH_TOKEN
      x-codex-turn-metadata: NLMCP_AUTH_TOKEN
""".strip(),
        encoding="utf-8",
    )

    config = BrokerConfig.from_file(config_file)
    upstream = config.upstreams["notebook"]

    assert upstream.request_meta == {
        "authToken": "NLMCP_AUTH_TOKEN",
        "x-codex-turn-metadata": "NLMCP_AUTH_TOKEN",
    }
    assert upstream.resolve_request_meta({}) == {
        "authToken": "secret-token",
        "x-codex-turn-metadata": "secret-token",
    }


def test_upstream_session_env_maps_client_context_to_child_environment(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig

    config_file = tmp_path / "broker.yaml"
    config_file.write_text(
        """
schema_version: 1
runtime:
  root: /tmp/mcp-broker-test
upstreams:
  session-tool:
    command: session-tool
    mode: per_session
    session_env:
      PROJECT_DIR: client_cwd
""".strip(),
        encoding="utf-8",
    )

    config = BrokerConfig.from_file(config_file)
    upstream = config.upstreams["session-tool"]

    assert upstream.session_env == {"PROJECT_DIR": "client_cwd"}
    assert upstream.resolve_session_environment({"client_cwd": "/tmp/project"}) == {
        "PROJECT_DIR": "/tmp/project"
    }


@pytest.mark.parametrize(
    ("session_env", "message"),
    [
        ("[]", "upstreams.session-tool.session_env must be a mapping"),
        (
            "{1: client_cwd}",
            "upstreams.session-tool.session_env keys must be environment variable names",
        ),
        (
            "{PROJECT_DIR: bad_source}",
            "upstreams.session-tool.session_env.PROJECT_DIR must be one of: client_cwd",
        ),
    ],
)
def test_upstream_session_env_rejects_invalid_shapes(
    tmp_path: Path,
    session_env: str,
    message: str,
) -> None:
    from mcp_broker.config import BrokerConfig

    config_file = tmp_path / "broker.yaml"
    config_file.write_text(
        f"""
schema_version: 1
runtime:
  root: /tmp/mcp-broker-test
upstreams:
  session-tool:
    command: session-tool
    mode: per_session
    session_env: {session_env}
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=message):
        BrokerConfig.from_file(config_file)


def test_upstream_session_env_requires_per_session_mode(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig

    config_file = tmp_path / "broker.yaml"
    config_file.write_text(
        """
schema_version: 1
runtime:
  root: /tmp/mcp-broker-test
upstreams:
  session-tool:
    command: session-tool
    mode: shared
    session_env:
      PROJECT_DIR: client_cwd
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="upstreams.session-tool.session_env requires mode: per_session",
    ):
        BrokerConfig.from_file(config_file)


def test_upstream_request_meta_must_reference_configured_env(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig

    config_file = tmp_path / "broker.yaml"
    config_file.write_text(
        """
schema_version: 1
runtime:
  root: /tmp/mcp-broker-test
upstreams:
  notebook:
    command: npx
    request_meta:
      authToken: NLMCP_AUTH_TOKEN
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="upstreams.notebook.request_meta.authToken must reference env or env_files",
    ):
        BrokerConfig.from_file(config_file)


@pytest.mark.parametrize(
    ("request_meta", "message"),
    [
        (
            "[]",
            "upstreams.notebook.request_meta must be a mapping",
        ),
        (
            "{1: NLMCP_AUTH_TOKEN}",
            "upstreams.notebook.request_meta keys must be request metadata names",
        ),
        (
            "{authToken: 1}",
            "upstreams.notebook.request_meta.authToken must name a configured environment variable",
        ),
    ],
)
def test_upstream_request_meta_rejects_invalid_shapes(
    tmp_path: Path,
    request_meta: str,
    message: str,
) -> None:
    from mcp_broker.config import BrokerConfig

    config_file = tmp_path / "broker.yaml"
    config_file.write_text(
        f"""
schema_version: 1
runtime:
  root: /tmp/mcp-broker-test
upstreams:
  notebook:
    command: npx
    env_files:
      NLMCP_AUTH_TOKEN: "{{runtime.secrets_dir}}/NLMCP_AUTH_TOKEN"
    request_meta: {request_meta}
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=message):
        BrokerConfig.from_file(config_file)


def test_upstream_auth_repair_contract_loads_from_yaml(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig

    config_file = tmp_path / "broker.yaml"
    config_file.write_text(
        """
schema_version: 1
runtime:
  root: /tmp/mcp-broker-test
upstreams:
  notebook:
    command: npx
    auth_repair:
      tool: setup_auth
      arguments:
        show_browser: true
        headless: false
      trigger_errors:
        - "Not authenticated"
        - "setup_auth"
      retry_original: true
      timeout_seconds: 300
""".strip(),
        encoding="utf-8",
    )

    config = BrokerConfig.from_file(config_file)
    repair = config.upstreams["notebook"].auth_repair

    assert repair is not None
    assert repair.tool == "setup_auth"
    assert repair.arguments == {"show_browser": True, "headless": False}
    assert repair.trigger_errors == ("Not authenticated", "setup_auth")
    assert repair.retry_original is True
    assert repair.timeout_seconds == 300


def test_upstream_oauth_token_file_auth_probe_loads_from_yaml(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig

    token_file = tmp_path / "runtime" / "secrets" / "oauth.json"
    config_file = tmp_path / "broker.yaml"
    config_file.write_text(
        f"""
schema_version: 1
runtime:
  root: {tmp_path / "runtime"}
upstreams:
  oauth:
    command: oauth-server
    auth_probe:
      type: oauth_token_file
      token_file: "{{runtime.secrets_dir}}/oauth.json"
      required_fields:
        - access_token
        - refresh_token
      refresh_token_expiry_field: refresh_token_expires_at
""".strip(),
        encoding="utf-8",
    )

    config = BrokerConfig.from_file(config_file)
    probe = config.upstreams["oauth"].auth_probe

    assert probe is not None
    assert probe.type == "oauth_token_file"
    assert probe.token_file == token_file
    assert probe.required_fields == ("access_token", "refresh_token")
    assert probe.refresh_token_expiry_field == "refresh_token_expires_at"


@pytest.mark.parametrize(
    ("auth_probe", "message"),
    [
        (
            """
      type: unsupported
      token_file: "{runtime.secrets_dir}/oauth.json"
""",
            "upstreams.oauth.auth_probe.type must be one of: oauth_token_file",
        ),
        (
            """
      type: oauth_token_file
      token_file: ""
""",
            "upstreams.oauth.auth_probe.token_file must be a non-empty string",
        ),
        (
            """
      type: oauth_token_file
      token_file: "{runtime.secrets_dir}/oauth.json"
      required_fields: nope
""",
            "upstreams.oauth.auth_probe.required_fields must be a list",
        ),
        (
            """
      type: oauth_token_file
      token_file: "{runtime.secrets_dir}/oauth.json"
      required_fields:
        - 7
""",
            "upstreams.oauth.auth_probe.required_fields must contain strings",
        ),
        (
            """
      type: oauth_token_file
      token_file: "{runtime.secrets_dir}/oauth.json"
      required_fields:
        - ""
""",
            "upstreams.oauth.auth_probe.required_fields cannot contain empty values",
        ),
        (
            """
      type: oauth_token_file
      token_file: "{runtime.secrets_dir}/oauth.json"
      refresh_token_expiry_field: ""
""",
            "upstreams.oauth.auth_probe.refresh_token_expiry_field must be a non-empty string",
        ),
    ],
)
def test_upstream_oauth_token_file_auth_probe_rejects_invalid_values(
    tmp_path: Path,
    auth_probe: str,
    message: str,
) -> None:
    from mcp_broker.config import BrokerConfig

    config_file = tmp_path / "broker.yaml"
    config_file.write_text(
        f"""
schema_version: 1
runtime:
  root: {tmp_path / "runtime"}
upstreams:
  oauth:
    command: oauth-server
    auth_probe:
{auth_probe}
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=message):
        BrokerConfig.from_file(config_file)


@pytest.mark.parametrize(
    ("auth_repair", "message"),
    [
        (
            """
      tool: ""
      trigger_errors:
        - "Not authenticated"
""",
            "upstreams.notebook.auth_repair.tool must be a non-empty string",
        ),
        (
            """
      tool: setup_auth
      arguments: []
      trigger_errors:
        - "Not authenticated"
""",
            "upstreams.notebook.auth_repair.arguments must be a mapping",
        ),
        (
            """
      tool: setup_auth
      trigger_errors: []
""",
            "upstreams.notebook.auth_repair.trigger_errors must be a non-empty list",
        ),
        (
            """
      tool: setup_auth
      trigger_errors:
        - 123
""",
            "upstreams.notebook.auth_repair.trigger_errors must contain strings",
        ),
        (
            """
      tool: setup_auth
      trigger_errors:
        - ""
""",
            "upstreams.notebook.auth_repair.trigger_errors cannot contain empty values",
        ),
        (
            """
      tool: setup_auth
      trigger_errors:
        - "Not authenticated"
      retry_original: "yes"
""",
            "upstreams.notebook.auth_repair.retry_original must be a boolean",
        ),
    ],
)
def test_upstream_auth_repair_contract_rejects_invalid_values(
    tmp_path: Path,
    auth_repair: str,
    message: str,
) -> None:
    from mcp_broker.config import BrokerConfig

    config_file = tmp_path / "broker.yaml"
    config_file.write_text(
        f"""
schema_version: 1
runtime:
  root: /tmp/mcp-broker-test
upstreams:
  notebook:
    command: npx
    auth_repair:
{auth_repair}
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=message):
        BrokerConfig.from_file(config_file)


def test_private_enabled_upstreams_have_bounded_live_call_timeout() -> None:
    from mcp_broker.config import BrokerConfig

    private_config = ROOT / "config" / "broker.private.yaml"
    if not private_config.exists():
        pytest.skip("private config is optional and ignored")
    config = BrokerConfig.from_file(private_config)
    unbounded = sorted(
        name
        for name, upstream in config.upstreams.items()
        if upstream.enabled
        and upstream.mode != "disabled"
        and upstream.health.call_timeout_seconds > 60
    )

    assert unbounded == []


def test_broker_config_rejects_missing_upstream_command(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig

    config_file = tmp_path / "broker.yaml"
    config_file.write_text(
        """
runtime:
  root: /tmp/mcp-broker-test
broker: {}
upstreams:
  broken:
    args: []
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="upstreams.broken.command"):
        BrokerConfig.from_file(config_file)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("[]", "broker config must be a mapping"),
        ("runtime: []", "runtime must be a mapping"),
        ("runtime:\n  root: /tmp/x\nbroker: []", "broker must be a mapping"),
        ("runtime:\n  root: /tmp/x\nupstreams: []", "upstreams must be a mapping"),
        ("runtime:\n  root: /tmp/x\nclients: []", "clients must be a mapping"),
        ("runtime: {}", "runtime.root"),
        ("runtime:\n  root: []", "runtime.root must be a path string"),
        ('runtime:\n  root: ""', "runtime.root must be a path string"),
        (
            """
runtime:
  root: /tmp/x
clients:
  codex:
    config_path: /tmp/codex.toml
""".strip(),
            "clients.codex.format",
        ),
        (
            """
runtime:
  root: /tmp/x
clients:
  codex:
    format: codex-toml
""".strip(),
            "clients.codex.config_path",
        ),
        (
            """
runtime:
  root: /tmp/x
clients:
  codex:
    format: xml
    config_path: /tmp/codex.toml
""".strip(),
            "clients.codex.format must be one of: claude-json, codex-toml, mcp-settings-json",
        ),
        (
            """
runtime:
  root: /tmp/x
clients:
  codex:
    format: codex-toml
    config_path: []
""".strip(),
            "clients.codex.config_path must be a path string",
        ),
        (
            """
runtime:
  root: /tmp/x
clients:
  codex:
    format: codex-toml
    config_path: /tmp/codex.toml
    args: bad
""".strip(),
            "clients.codex.args",
        ),
        (
            """
runtime:
  root: /tmp/x
clients:
  codex:
    format: mcp-settings-json
    config_path: /tmp/settings.json
    mcp_allowed_servers: bad
""".strip(),
            "clients.codex.mcp_allowed_servers",
        ),
        (
            """
runtime:
  root: /tmp/x
clients:
  codex:
    format: codex-toml
    config_path: /tmp/codex.toml
    backup_paths: bad
""".strip(),
            "clients.codex.backup_paths",
        ),
        (
            """
runtime:
  root: /tmp/x
upstreams:
  broken:
    command: node
    mode: global
""".strip(),
            "upstreams.broken.mode",
        ),
        (
            """
runtime:
  root: /tmp/x
upstreams:
  broken:
    command: node
    args: nope
""".strip(),
            "upstreams.broken.args",
        ),
        (
            """
schema_version: 1
runtime:
  root: /tmp/x
unknown: true
""".strip(),
            "unknown config key: unknown",
        ),
        (
            """
schema_version: 2
runtime:
  root: /tmp/x
""".strip(),
            "schema_version must be 1",
        ),
        (
            """
runtime:
  root: /tmp/x
  typo: true
""".strip(),
            "unknown config key: runtime.typo",
        ),
        (
            """
runtime:
  root: /tmp/x
broker:
  typo: true
""".strip(),
            "unknown config key: broker.typo",
        ),
        (
            """
runtime:
  root: /tmp/x
profiles:
  codex:
    max_tools: 80
    typo: true
""".strip(),
            "unknown config key: profiles.codex.typo",
        ),
        (
            """
runtime:
  root: /tmp/x
clients:
  codex:
    format: codex-toml
    config_path: /tmp/codex.toml
    typo: true
""".strip(),
            "unknown config key: clients.codex.typo",
        ),
        (
            """
runtime:
  root: /tmp/x
upstreams:
  read-store:
    command: npx
    typo: true
""".strip(),
            "unknown config key: upstreams.read-store.typo",
        ),
        (
            """
runtime:
  root: /tmp/x
upstreams:
  read-store:
    command: npx
    restart:
      typo: true
""".strip(),
            "unknown config key: upstreams.read-store.restart.typo",
        ),
        (
            """
runtime:
  root: /tmp/x
upstreams:
  read-store:
    command: npx
    working_dir: []
""".strip(),
            "upstreams.read-store.working_dir must be a path string",
        ),
        (
            """
runtime:
  root: /tmp/x
upstreams:
  read-store:
    command: npx
    env_files:
      TOKEN: ""
""".strip(),
            "upstreams.read-store.env_files.TOKEN must be a path string",
        ),
    ],
)
def test_broker_config_rejects_invalid_shapes(tmp_path: Path, body: str, message: str) -> None:
    from mcp_broker.config import BrokerConfig

    config_file = tmp_path / "broker.yaml"
    config_file.write_text(body, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        BrokerConfig.from_file(config_file)
