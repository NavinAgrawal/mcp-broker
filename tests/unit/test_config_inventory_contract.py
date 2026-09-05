from pathlib import Path

import pytest
import yaml

from tests.support.makefiles import read_combined_makefiles
from tests.support.repo_paths import private_config_path, repo_root


pytestmark = pytest.mark.unit

ROOT = repo_root()
PUBLIC_CONFIG_FILE = ROOT / "config" / "broker.example.yaml"

REQUIRED_CLIENT_PROFILES = {"codex", "claude", "agy", "manual-test", "maintenance"}
LLM_PROFILES = ("codex", "claude", "agy")
PUBLIC_EXAMPLE_UPSTREAMS = [
    "example-env-auth",
    "example-file-auth",
    "example-http",
    "example-mutating",
    "example-python",
    "example-request-meta-auth",
    "example-store",
    "github",
]


def test_makefile_defaults_to_private_config_not_public_example() -> None:
    makefile_text = read_combined_makefiles(ROOT)

    assert "RUNTIME_CONFIG_RELATIVE_PATH ?= config/broker.private.yaml" in makefile_text
    assert "CONFIG_PRIVATE_PATH ?= $(RUNTIME_ROOT)/$(RUNTIME_CONFIG_RELATIVE_PATH)" in makefile_text
    assert "CONFIG_PATH       ?= $(CONFIG_PRIVATE_PATH)" in makefile_text


def test_public_example_config_has_no_private_upstream_inventory() -> None:
    loaded = yaml.safe_load(PUBLIC_CONFIG_FILE.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    upstreams = loaded.get("upstreams")
    assert isinstance(upstreams, dict)

    assert sorted(upstreams) == PUBLIC_EXAMPLE_UPSTREAMS
    assert all(upstream["enabled"] is False for upstream in upstreams.values())


def test_public_example_config_shows_full_runtime_contract() -> None:
    loaded = yaml.safe_load(PUBLIC_CONFIG_FILE.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    runtime = loaded.get("runtime")
    assert isinstance(runtime, dict)

    assert loaded.get("schema_version") == 1
    assert runtime == {
        "root": "$HOME/mcp/mcp-broker",
        "socket_path": "$HOME/mcp/mcp-broker/sockets/broker.sock",
        "log_dir": "$HOME/mcp/mcp-broker/logs",
        "state_dir": "$HOME/mcp/mcp-broker/state",
        "secrets_dir": "$HOME/mcp/mcp-broker/secrets",
    }


@pytest.mark.private_contract
def test_public_and_private_runtime_contract_keys_match() -> None:
    public = yaml.safe_load(PUBLIC_CONFIG_FILE.read_text(encoding="utf-8"))
    private = yaml.safe_load(_private_config_text_or_skip())
    assert isinstance(public, dict)
    assert isinstance(private, dict)
    public_runtime = public.get("runtime")
    private_runtime = private.get("runtime")
    assert isinstance(public_runtime, dict)
    assert isinstance(private_runtime, dict)

    assert set(public_runtime) == set(private_runtime)


def test_public_example_client_commands_are_portable_and_runtime_derived() -> None:
    loaded = yaml.safe_load(PUBLIC_CONFIG_FILE.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    clients = loaded.get("clients")
    assert isinstance(clients, dict)

    for client_name in ["codex", "claude", "agy"]:
        client = clients.get(client_name)
        assert isinstance(client, dict), client_name
        assert client["command"] == "mcp-broker-client"
        assert client["args"] == [
            "--socket-path",
            "{runtime.socket_path}",
            "--profile",
            client_name,
        ]


def test_public_example_config_has_no_private_path_markers() -> None:
    public_text = PUBLIC_CONFIG_FILE.read_text(encoding="utf-8")

    private_markers = [
        "private-home-marker",
        "/Users/",
        "$HOME/Projects",
        "$HOME/private-workspace",
        "$HOME/Library",
        "$HOME/Documents",
        "CloudStorage",
    ]

    assert [marker for marker in private_markers if marker in public_text] == []


def test_public_example_mutating_upstreams_are_profile_allowlisted() -> None:
    raw_upstreams = _raw_upstreams()
    loaded = yaml.safe_load(PUBLIC_CONFIG_FILE.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    profiles = loaded.get("profiles")
    assert isinstance(profiles, dict)

    missing_allowlists = {
        profile_name: sorted(
            name
            for name, upstream in raw_upstreams.items()
            if upstream.get("mutating") is True
            and profile_name in upstream.get("profiles", [])
            and name not in profiles.get(profile_name, {}).get("allow_mutating_upstreams", [])
        )
        for profile_name in profiles
    }

    assert {name: missing for name, missing in missing_allowlists.items() if missing} == {}


def test_public_example_has_complete_upstream_metadata() -> None:
    raw_upstreams = _raw_upstreams()

    missing_metadata = {
        name
        for name, upstream in raw_upstreams.items()
        for required in ["command", "mode", "profiles", "state_dir", "tool_prefix", "transport"]
        if required not in upstream
    }

    assert missing_metadata == set()


def test_example_config_defines_required_client_profiles() -> None:
    from mcp_broker.config import BrokerConfig

    config = BrokerConfig.from_file(PUBLIC_CONFIG_FILE)
    missing = sorted(REQUIRED_CLIENT_PROFILES - set(config.profiles))

    assert missing == []


def test_public_config_loads_without_private_files() -> None:
    from mcp_broker.config import BrokerConfig

    config = BrokerConfig.from_file(PUBLIC_CONFIG_FILE)

    assert sorted(config.upstreams) == PUBLIC_EXAMPLE_UPSTREAMS
    assert config.runtime.socket_path == ROOT.home() / "mcp/mcp-broker/sockets/broker.sock"


def test_public_example_config_is_comment_rich_and_teaches_common_mcp_patterns() -> None:
    public_text = PUBLIC_CONFIG_FILE.read_text(encoding="utf-8")

    required_comments = [
        "# Contract version for this YAML shape.",
        "# CHANGE: keep this at 1 unless this repo documents a new schema.",
        "# Runtime state belongs outside the repo.",
        "# CHANGE: set root if you want runtime state somewhere else.",
        "# DEFAULT: these values can usually stay as-is.",
        "# Add upstream MCP servers under this mapping.",
        "# CHANGE: copy one of these patterns and edit name, command, args, and profiles.",
        "# Pattern: stdio MCP installed from npm.",
        "# Pattern: local Python MCP checked out elsewhere.",
        "# session_env maps broker session context into upstream environment.",
        "# Pattern: auth from host environment variable.",
        "# Pattern: auth from broker-owned secret file.",
        "# Pattern: upstream also expects a per-request MCP metadata token.",
        "# auth_repair runs a configured upstream auth tool after matching auth errors.",
        "# auth_probe can inspect broker-owned OAuth token JSON passively.",
        "# Pattern: HTTP or SSE MCP endpoint.",
        "# Pattern: remote SaaS MCP endpoint with write-capable tools.",
        "# Pattern: mutating upstream gated by profile allowlist.",
    ]
    required_contract_fields = [
        "schema_version: 1",
        "socket_path:",
        "log_dir:",
        "state_dir:",
        "secrets_dir:",
        "startup_timeout_seconds:",
        "restart:",
        "health:",
        "resources:",
        "env:",
        "env_files:",
        "session_env:",
        "request_meta:",
        "auth_probe:",
        "auth_repair:",
        "mutating:",
        "serialize_calls:",
        "allow_mutating_upstreams:",
    ]

    assert [comment for comment in required_comments if comment not in public_text] == []
    assert [field for field in required_contract_fields if field not in public_text] == []


def test_public_example_defines_agy_profile_and_renderer() -> None:
    loaded = yaml.safe_load(PUBLIC_CONFIG_FILE.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    profiles = loaded.get("profiles")
    clients = loaded.get("clients")
    assert isinstance(profiles, dict)
    assert isinstance(clients, dict)

    assert profiles["agy"] == {
        "max_tools": 80,
        "compact_tools_enabled": True,
        "broker_tool_name_style": "snake",
    }
    assert clients["agy"]["format"] == "mcp-settings-json"
    assert clients["agy"]["config_path"] == "$HOME/.gemini/config/mcp_config.json"
    assert clients["agy"]["mcp_allowed_servers"] == ["mcp-broker"]


@pytest.mark.private_contract
def test_private_config_preserves_public_contract_comments_and_agy_profile() -> None:
    from mcp_broker.config import BrokerConfig

    public_text = PUBLIC_CONFIG_FILE.read_text(encoding="utf-8")
    private_config_file = _required_private_config_file()
    private_text = _private_config_text_or_skip()
    required_comments = [
        line
        for line in public_text.splitlines()
        if line.startswith("# ")
        and (
            line.startswith("# CHANGE:")
            or line.startswith("# DEFAULT:")
            or line.startswith("# Pattern:")
            or line in {
                "# Contract version for this YAML shape.",
                "# Runtime state belongs outside the repo.",
                "# Client-facing exposure profiles.",
                "# Client config render targets.",
                "# Add upstream MCP servers under this mapping.",
            }
        )
    ]
    config = BrokerConfig.from_file(private_config_file)

    assert [comment for comment in required_comments if comment not in private_text] == []
    assert "agy" in config.profiles
    assert "agy" in config.clients
    missing_agy = sorted(
        name
        for name, upstream in config.upstreams.items()
        if upstream.enabled
        and "codex" in upstream.profiles
        and "claude" in upstream.profiles
        and "agy" not in upstream.profiles
    )
    assert missing_agy == []


@pytest.mark.private_contract
def test_private_config_keeps_llm_profile_exposure_in_parity() -> None:
    from mcp_broker.config import BrokerConfig

    private_config_file = _required_private_config_file()
    config = BrokerConfig.from_file(private_config_file)
    enabled_by_profile = {
        profile_name: {
            upstream.name
            for upstream in config.upstreams.values()
            if upstream.enabled and profile_name in upstream.profiles
        }
        for profile_name in LLM_PROFILES
    }

    assert enabled_by_profile["claude"] == enabled_by_profile["agy"]
    assert enabled_by_profile["claude"] <= enabled_by_profile["codex"]


def test_private_config_path_is_gitignored() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "config/broker.private.yaml" in gitignore


def test_public_example_has_no_private_path_markers_in_serialized_yaml() -> None:
    loaded = yaml.safe_load(PUBLIC_CONFIG_FILE.read_text(encoding="utf-8"))
    dumped = yaml.safe_dump(loaded, sort_keys=True)

    private_markers = [
        "/Users/",
        "$HOME/Projects",
        "$HOME/Library",
        "$HOME/Documents",
        "CloudStorage",
    ]

    assert [marker for marker in private_markers if marker in dumped] == []


def _raw_upstreams() -> dict[str, object]:
    loaded = yaml.safe_load(PUBLIC_CONFIG_FILE.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    upstreams = loaded.get("upstreams")
    assert isinstance(upstreams, dict)
    return upstreams


def _private_config_text_or_skip() -> str:
    return _required_private_config_file().read_text(encoding="utf-8")


def _required_private_config_file() -> Path:
    path = private_config_path()
    assert path is not None, "MCP_BROKER_CONFIG or MCP_BROKER_LIVE_CONFIG_PATH is required"
    assert path.is_file(), f"private broker config does not exist: {path}"
    return path
