from pathlib import Path
import pytest
from tests.support.repo_paths import repo_root
pytestmark = pytest.mark.unit
ROOT = repo_root()

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
