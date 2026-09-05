from pathlib import Path
import pytest
from tests.support.repo_paths import private_config_path, repo_root
pytestmark = pytest.mark.unit
ROOT = repo_root()

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
@pytest.mark.private_contract
def test_private_enabled_upstreams_have_bounded_live_call_timeout() -> None:
    from mcp_broker.config import BrokerConfig
    from mcp_broker.schema import MAX_CALL_TIMEOUT_SECONDS

    private_config = private_config_path()
    assert private_config is not None, "MCP_BROKER_CONFIG or MCP_BROKER_LIVE_CONFIG_PATH is required"
    assert private_config.is_file(), f"private broker config does not exist: {private_config}"
    config = BrokerConfig.from_file(private_config)
    unbounded = sorted(
        name
        for name, upstream in config.upstreams.items()
        if upstream.enabled
        and upstream.mode != "disabled"
        and upstream.health.call_timeout_seconds > MAX_CALL_TIMEOUT_SECONDS
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
