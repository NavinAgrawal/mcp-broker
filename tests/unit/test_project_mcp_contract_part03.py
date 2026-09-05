from datetime import UTC
from pathlib import Path
import json
import pytest
pytestmark = [pytest.mark.unit, pytest.mark.error_simulation]
def _write_broker_config(path: Path) -> None:
    path.write_text(
        """
schema_version: 1
runtime:
  root: /tmp/mcp-broker-test
profiles:
  codex:
    max_tools: 80
    compact_tools_enabled: true
  claude:
    max_tools: 80
    compact_tools_enabled: true
  manual-test:
    max_tools: 200
    compact_tools_enabled: false
upstreams:
  covered-tool:
    command: covered-tool
    tool_prefix: covered-tool
    profiles:
      - codex
      - claude
  covered-no-prefix:
    command: covered-no-prefix
    profiles:
      - codex
      - claude
""".strip()
        + "\n",
        encoding="utf-8",
    )
def _write_project_mcp(path: Path, servers: dict[str, object]) -> None:
    path.write_text(json.dumps({"mcpServers": servers}, indent=2) + "\n", encoding="utf-8")

def test_project_mcp_import_appends_upstreams_when_config_has_no_upstreams(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig
    from mcp_broker.project_mcp import audit_project_mcp_files

    config_path = tmp_path / "broker.yaml"
    config_path.write_text(
        """
schema_version: 1
runtime:
  root: /tmp/mcp-broker-test
profiles:
  codex:
    max_tools: 80
    compact_tools_enabled: true
""".lstrip(),
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project.mkdir()
    _write_project_mcp(project / ".mcp.json", {"local-tool": {"command": "local-tool"}})

    report = audit_project_mcp_files(
        config_path=config_path,
        roots=[project],
        backup_root=tmp_path / "backups",
        import_missing=True,
        apply=True,
        profiles=["codex"],
    )

    assert report.files_changed == [project / ".mcp.json"]
    assert "upstreams:\n  local-tool:" in config_path.read_text(encoding="utf-8")
    BrokerConfig.from_file(config_path)

def test_project_mcp_insert_under_upstreams_before_next_top_level_without_newline() -> None:
    from mcp_broker.project_mcp import _insert_under_upstreams

    config_text = "schema_version: 1\nupstreams:\nprofiles:"

    assert _insert_under_upstreams(config_text, "  local-tool:\n    command: local-tool\n") == (
        "schema_version: 1\n"
        "upstreams:\n"
        "  local-tool:\n"
        "    command: local-tool\n"
        "profiles:"
    )

def test_project_mcp_insert_under_upstreams_preserves_comments_blanks_and_existing_entries() -> None:
    from mcp_broker.project_mcp import _insert_under_upstreams

    addition = "  local-tool:\n    command: local-tool\n"
    config_text = (
        "schema_version: 1\n"
        "upstreams:\n"
        "  existing-tool:\n"
        "    command: existing-tool\n"
        "# retained comment\n"
        "profiles:\n"
    )

    assert _insert_under_upstreams(config_text, addition) == (
        "schema_version: 1\n"
        "upstreams:\n"
        "  existing-tool:\n"
        "    command: existing-tool\n"
        "# retained comment\n"
        "  local-tool:\n"
        "    command: local-tool\n"
        "profiles:\n"
    )
    assert _insert_under_upstreams("upstreams:\n\nprofiles:\n", addition) == (
        "upstreams:\n"
        "\n"
        "  local-tool:\n"
        "    command: local-tool\n"
        "profiles:\n"
    )

def test_project_mcp_insert_under_missing_upstreams_preserves_trailing_newline() -> None:
    from mcp_broker.project_mcp import _insert_under_upstreams

    assert _insert_under_upstreams("schema_version: 1\n", "  local-tool: {}\n") == (
        "schema_version: 1\n"
        "upstreams:\n"
        "  local-tool: {}\n"
    )

def test_project_mcp_insert_under_bare_upstreams_line_and_addition_without_newline() -> None:
    from mcp_broker.project_mcp import _insert_under_upstreams

    assert _insert_under_upstreams("upstreams:", "  local-tool: {}") == (
        "upstreams:\n"
        "  local-tool: {}\n"
    )

def test_project_mcp_insert_under_terminal_upstreams_line_adds_newline() -> None:
    from mcp_broker.project_mcp import _insert_under_upstreams

    config_text = "schema_version: 1\nupstreams:"

    assert _insert_under_upstreams(config_text, "  local-tool:\n    command: local-tool\n") == (
        "schema_version: 1\n"
        "upstreams:\n"
        "  local-tool:\n"
        "    command: local-tool\n"
    )

def test_project_mcp_import_rolls_back_when_broker_validation_fails(tmp_path: Path) -> None:
    from mcp_broker.project_mcp import _append_missing_upstreams

    config_path = tmp_path / "broker.yaml"
    original = """
schema_version: 1
runtime:
  root: /tmp/mcp-broker-test
profiles:
  codex:
    max_tools: 80
    compact_tools_enabled: true
upstreams:
""".lstrip()
    config_path.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError):
        _append_missing_upstreams(
            config_path,
            {
                "local-tool": {
                    "enabled": True,
                    "mode": "shared",
                    "profiles": ["missing-profile"],
                    "transport": "stdio",
                    "command": "local-tool",
                }
            },
        )

    assert config_path.read_text(encoding="utf-8") == original

def test_project_mcp_parser_preserves_public_option_contract(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.project_mcp import _parse_args

    parsed = _parse_args(
        [
            "--config",
            str(tmp_path / "broker.yaml"),
            "--root",
            str(tmp_path / "one"),
            "--root",
            str(tmp_path / "two"),
            "--backup-root",
            str(tmp_path / "backups"),
            "--claude-config",
            str(tmp_path / "claude.json"),
            "--profile",
            "codex",
            "--profile",
            "claude",
            "--import-missing",
            "--apply",
        ]
    )

    assert parsed.config == tmp_path / "broker.yaml"
    assert parsed.root == [tmp_path / "one", tmp_path / "two"]
    assert parsed.backup_root == tmp_path / "backups"
    assert parsed.claude_config == tmp_path / "claude.json"
    assert parsed.profile == ["codex", "claude"]
    assert parsed.import_missing is True
    assert parsed.apply is True

    defaults = _parse_args(
        [
            "--config",
            str(tmp_path / "broker.yaml"),
            "--root",
            str(tmp_path),
            "--backup-root",
            str(tmp_path / "backups"),
        ]
    )
    assert defaults.profile == ["codex", "claude"]
    assert defaults.import_missing is False
    assert defaults.apply is False

    with pytest.raises(SystemExit) as help_exit:
        _parse_args(["--help"])
    assert help_exit.value.code == 0
    help_text = capsys.readouterr().out
    assert "XX" not in help_text
    for fragment in [
        "Audit and migrate project-local .mcp.json files",
        "Broker YAML config",
        "Root to scan recursively; repeat for multiple roots",
        "Backup directory",
        "Claude JSON config with per-project MCP entries",
        "Broker profile for imported upstreams",
        "Append missing entries to broker config",
        "Write backups, imports, and empty .mcp.json files",
    ]:
        assert fragment in help_text

@pytest.mark.parametrize(
    ("args", "missing_option"),
    [
        (["--root", "__ROOT__", "--backup-root", "__BACKUP__"], "--config"),
        (["--config", "__CONFIG__", "--backup-root", "__BACKUP__"], "--root"),
        (["--config", "__CONFIG__", "--root", "__ROOT__"], "--backup-root"),
    ],
)
def test_project_mcp_parser_requires_config_root_and_backup_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    args: list[str],
    missing_option: str,
) -> None:
    from mcp_broker.project_mcp import _parse_args

    materialized = [
        arg.replace("__CONFIG__", str(tmp_path / "broker.yaml"))
        .replace("__ROOT__", str(tmp_path))
        .replace("__BACKUP__", str(tmp_path / "backups"))
        for arg in args
    ]

    with pytest.raises(SystemExit) as exc:
        _parse_args(materialized)

    assert exc.value.code == 2
    assert missing_option in capsys.readouterr().err

def test_project_mcp_migration_helpers_preserve_file_format_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import project_mcp

    assert project_mcp._is_http_server({"type": "HTTP"}) is True
    assert project_mcp._is_http_server({"type": "sse"}) is True
    assert project_mcp._is_http_server({"url": "https://example.invalid/mcp"}) is True
    assert project_mcp._is_http_server({"type": "stdio"}) is False
    assert project_mcp._is_http_server({"type": None}) is False
    assert project_mcp._is_http_server({}) is False
    assert project_mcp._header_env_name("!!!") == "HEADER"
    assert project_mcp._header_env_name("x-api-key") == "X_API_KEY"
    assert project_mcp._base_import("local-tool", ("codex", "claude")) == {
        "enabled": True,
        "mode": "shared",
        "purpose": "Imported from project-local .mcp.json entry local-tool.",
        "tags": ["project-import"],
        "tool_prefix": "local-tool",
        "state_dir": "upstreams/local-tool",
        "profiles": ["codex", "claude"],
    }
    assert project_mcp._yaml_upstream_addition(
        {"b-tool": {"command": "b"}, "a-tool": {"command": "a"}}
    ) == "  b-tool:\n    command: b\n  a-tool:\n    command: a\n"
    assert project_mcp._insert_under_upstreams("schema_version: 1", "  local-tool: {}\n") == (
        "schema_version: 1\nupstreams:\n  local-tool: {}\n"
    )

    project_file = tmp_path / "Mixed Project" / ".mcp.json"
    project_file.parent.mkdir()
    _write_project_mcp(project_file, {"covered-tool": {"command": "covered-tool"}})
    backup_root = tmp_path / "backup-root"
    seen_timezone = []

    def fixed_now(timezone: object) -> object:
        seen_timezone.append(timezone)
        return type("FixedNow", (), {"strftime": lambda _self, fmt: f"{fmt}:fixed"})()

    fixed_datetime = type(
        "FixedDateTime",
        (),
        {"now": staticmethod(fixed_now)},
    )
    monkeypatch.setattr(project_mcp, "datetime", fixed_datetime)

    backup_path = project_mcp._backup_file(project_file, backup_root)

    assert seen_timezone == [UTC]
    assert backup_path.parent == backup_root
    assert backup_path.name.startswith("%Y%m%dT%H%M%SZ:fixed.")
    assert "Mixed__Project__.mcp.json" in backup_path.name
    assert backup_path.read_text(encoding="utf-8") == project_file.read_text(encoding="utf-8")

    mcp_state = project_mcp._ProjectMcpFile(
        path=project_file,
        data={"z": True, "mcpServers": {"covered-tool": {"command": "covered-tool"}}},
        servers={"covered-tool": {"command": "covered-tool"}},
    )
    project_mcp._write_empty_mcp_servers(mcp_state)
    assert project_file.read_text(encoding="utf-8") == (
        '{\n  "mcpServers": {},\n  "z": true\n}\n'
    )

def test_project_mcp_yaml_dump_contract_uses_insertion_order_and_block_style(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import project_mcp

    calls: list[dict[str, object]] = []

    def fake_safe_dump(value: object, **kwargs: object) -> str:
        calls.append({"value": value, **kwargs})
        return "b-tool:\n  command: b\n"

    monkeypatch.setattr(project_mcp.yaml, "safe_dump", fake_safe_dump)

    assert project_mcp._yaml_upstream_addition({"b-tool": {"command": "b"}}) == (
        "  b-tool:\n"
        "    command: b\n"
    )
    assert calls == [
        {
            "value": {"b-tool": {"command": "b"}},
            "sort_keys": False,
            "default_flow_style": False,
        }
    ]

def test_project_mcp_file_io_uses_explicit_text_encoding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import project_mcp

    config_path = tmp_path / "broker.yaml"
    _write_broker_config(config_path)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project_file = project_dir / ".mcp.json"
    _write_project_mcp(project_file, {"covered-tool": {"command": "covered-tool"}})
    claude_config = tmp_path / "claude.json"
    claude_config.write_text(
        json.dumps(
            {
                "projects": {
                    str(project_dir): {
                        "mcpServers": {"covered-tool": {"command": "covered-tool"}},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    real_read_text = Path.read_text
    real_write_text = Path.write_text
    checked_reads = {config_path, project_file, claude_config}
    read_calls: list[Path] = []
    write_calls: list[Path] = []

    def checked_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self in checked_reads:
            assert kwargs.get("encoding") == project_mcp.TEXT_ENCODING
            read_calls.append(self)
        return real_read_text(self, *args, **kwargs)

    def checked_write_text(self: Path, data: str, *args: object, **kwargs: object) -> int:
        backup_root = tmp_path / "backups"
        if self in checked_reads or self.parent == backup_root:
            assert kwargs.get("encoding") == project_mcp.TEXT_ENCODING
            write_calls.append(self)
        return real_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", checked_read_text)
    monkeypatch.setattr(Path, "write_text", checked_write_text)

    loaded_project = project_mcp._load_mcp_file(project_file)
    loaded_claude = project_mcp._load_claude_project_entries(claude_config, [project_dir])
    project_mcp._append_missing_upstreams(
        config_path,
        {"local-tool": {"command": "local-tool", "profiles": ["codex"]}},
    )
    backup_path = project_mcp._backup_file(project_file, tmp_path / "backups")
    project_mcp._write_empty_mcp_servers(loaded_project)
    project_mcp._write_empty_mcp_servers(loaded_claude[0])

    assert loaded_project.data == {
        "mcpServers": {"covered-tool": {"command": "covered-tool"}}
    }
    assert loaded_claude[0].data["projects"][str(project_dir)]["mcpServers"] == {
        "covered-tool": {"command": "covered-tool"}
    }
    assert backup_path in write_calls
    for path in [config_path, project_file, claude_config]:
        assert path in read_calls
        assert path in write_calls

def test_project_mcp_append_rollback_uses_explicit_text_encoding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import project_mcp

    config_path = tmp_path / "broker.yaml"
    _write_broker_config(config_path)
    original = config_path.read_text(encoding="utf-8")
    real_write_text = Path.write_text
    write_calls: list[Path] = []

    def checked_write_text(self: Path, data: str, *args: object, **kwargs: object) -> int:
        if self == config_path:
            assert kwargs.get("encoding") == project_mcp.TEXT_ENCODING
            write_calls.append(self)
        return real_write_text(self, data, *args, **kwargs)

    def reject_updated_config(path: Path) -> object:
        assert path == config_path
        raise ValueError("invalid rendered config")

    monkeypatch.setattr(Path, "write_text", checked_write_text)
    monkeypatch.setattr(project_mcp.BrokerConfig, "from_file", reject_updated_config)

    with pytest.raises(ValueError, match="invalid rendered config"):
        project_mcp._append_missing_upstreams(
            config_path,
            {"bad-tool": {"command": "bad-tool", "profiles": ["codex"]}},
        )

    assert write_calls == [config_path, config_path]
    assert config_path.read_text(encoding="utf-8") == original

def test_project_mcp_claude_writer_preserves_exact_json_format_contract(tmp_path: Path) -> None:
    from mcp_broker import project_mcp

    project = tmp_path / "project"
    project.mkdir()
    claude_config = tmp_path / "claude.json"
    claude_config.write_text(
        json.dumps(
            {
                "top": True,
                "projects": {
                    str(project): {
                        "z": True,
                        "mcpServers": {"covered-tool": {"command": "covered-tool"}},
                        "a": False,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    project_mcp._write_empty_mcp_servers(
        project_mcp._ProjectMcpFile(
            path=claude_config,
            data={},
            servers={"covered-tool": {"command": "covered-tool"}},
            claude_project_path=str(project),
        )
    )

    expected = {
        "top": True,
        "projects": {
            str(project): {
                "z": True,
                "mcpServers": {},
                "a": False,
            }
        },
    }
    assert claude_config.read_text(encoding="utf-8") == json.dumps(
        expected,
        indent=2,
        sort_keys=True,
    ) + "\n"
