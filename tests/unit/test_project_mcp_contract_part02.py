from pathlib import Path
import json
import re
import pytest
import yaml
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

def test_project_mcp_file_discovery_continues_after_ignored_paths(tmp_path: Path) -> None:
    from mcp_broker.project_mcp import _find_project_mcp_files

    backup_dir = tmp_path / ".backup" / "snapshot"
    git_dir = tmp_path / ".git" / "fixtures"
    valid_dir = tmp_path / "zz-valid-project"
    backup_dir.mkdir(parents=True)
    git_dir.mkdir(parents=True)
    valid_dir.mkdir()
    backup_file = backup_dir / ".mcp.json"
    ignored_file = git_dir / ".mcp.json"
    valid_file = valid_dir / ".mcp.json"
    _write_project_mcp(backup_file, {"backup-tool": {"command": "backup-tool"}})
    _write_project_mcp(ignored_file, {"ignored-tool": {"command": "ignored-tool"}})
    _write_project_mcp(valid_file, {"covered-tool": {"command": "covered-tool"}})

    assert _find_project_mcp_files([tmp_path], tmp_path / ".backup") == [valid_file]

def test_project_mcp_empty_file_does_not_stop_later_file_changes(tmp_path: Path) -> None:
    from mcp_broker.project_mcp import audit_project_mcp_files

    config_path = tmp_path / "broker.yaml"
    _write_broker_config(config_path)
    empty_project = tmp_path / "10-empty"
    covered_project = tmp_path / "20-covered"
    empty_project.mkdir()
    covered_project.mkdir()
    empty_file = empty_project / ".mcp.json"
    covered_file = covered_project / ".mcp.json"
    _write_project_mcp(empty_file, {})
    _write_project_mcp(covered_file, {"covered-tool": {"command": "covered-tool"}})

    report = audit_project_mcp_files(
        config_path=config_path,
        roots=[tmp_path],
        backup_root=tmp_path / "backups",
        import_missing=False,
        apply=True,
        profiles=["codex", "claude"],
    )

    assert report.files_scanned == 2
    assert report.files_changed == [covered_file]
    assert report.files_blocked == []
    assert json.loads(empty_file.read_text(encoding="utf-8")) == {"mcpServers": {}}
    assert json.loads(covered_file.read_text(encoding="utf-8")) == {"mcpServers": {}}

def test_project_mcp_missing_mcp_servers_field_is_treated_as_empty(tmp_path: Path) -> None:
    from mcp_broker.project_mcp import audit_project_mcp_files

    config_path = tmp_path / "broker.yaml"
    _write_broker_config(config_path)
    project = tmp_path / "project"
    covered_project = tmp_path / "covered"
    project.mkdir()
    covered_project.mkdir()
    missing_field_file = project / ".mcp.json"
    covered_file = covered_project / ".mcp.json"
    missing_field_file.write_text(json.dumps({"otherSetting": True}) + "\n", encoding="utf-8")
    _write_project_mcp(covered_file, {"covered-tool": {"command": "covered-tool"}})

    report = audit_project_mcp_files(
        config_path=config_path,
        roots=[tmp_path],
        backup_root=tmp_path / "backups",
        import_missing=False,
        apply=True,
        profiles=["codex", "claude"],
    )

    assert report.files_scanned == 2
    assert report.files_changed == [covered_file]
    assert report.files_blocked == []
    assert json.loads(missing_field_file.read_text(encoding="utf-8")) == {"otherSetting": True}

def test_project_mcp_migrates_claude_project_state_entries(tmp_path: Path) -> None:
    from mcp_broker.project_mcp import audit_project_mcp_files

    config_path = tmp_path / "broker.yaml"
    _write_broker_config(config_path)
    project = tmp_path / "project"
    sibling_project = tmp_path / "sibling"
    other_project = tmp_path / "other"
    outside_project = tmp_path.parent / "outside-project"
    project.mkdir()
    sibling_project.mkdir()
    other_project.mkdir()
    outside_project.mkdir(exist_ok=True)
    claude_config = tmp_path / "claude.json"
    claude_config.write_text(
        json.dumps(
            {
                "projects": {
                    str(project): {
                        "mcpServers": {"covered-tool": {"command": "covered-tool"}},
                        "otherSetting": True,
                    },
                    str(sibling_project): {
                        "mcpServers": {"covered-tool": {"command": "covered-tool"}},
                    },
                    str(other_project): {
                        "mcpServers": {"covered-tool": {"command": "covered-tool"}},
                    },
                    str(outside_project): {
                        "mcpServers": {"covered-tool": {"command": "covered-tool"}},
                    },
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    report = audit_project_mcp_files(
        config_path=config_path,
        roots=[tmp_path],
        backup_root=tmp_path / "backups",
        import_missing=False,
        apply=True,
        profiles=["codex", "claude"],
        claude_config_path=claude_config,
    )

    loaded = json.loads(claude_config.read_text(encoding="utf-8"))
    assert report.files_scanned == 3
    assert report.files_changed == [claude_config, claude_config, claude_config]
    assert len(report.backups) == 1
    assert loaded["projects"][str(project)]["mcpServers"] == {}
    assert loaded["projects"][str(project)]["otherSetting"] is True
    assert loaded["projects"][str(sibling_project)]["mcpServers"] == {}
    assert loaded["projects"][str(other_project)]["mcpServers"] == {}
    assert loaded["projects"][str(outside_project)]["mcpServers"] == {
        "covered-tool": {"command": "covered-tool"}
    }

def test_project_mcp_nonmatching_claude_project_does_not_stop_later_matching_entry(
    tmp_path: Path,
) -> None:
    from mcp_broker.project_mcp import audit_project_mcp_files

    config_path = tmp_path / "broker.yaml"
    _write_broker_config(config_path)
    project = tmp_path / "project"
    outside_project = tmp_path.parent / "outside-project"
    project.mkdir()
    outside_project.mkdir(exist_ok=True)
    claude_config = tmp_path / "claude.json"
    claude_config.write_text(
        json.dumps(
            {
                "projects": {
                    str(outside_project): {
                        "mcpServers": {"outside-tool": {"command": "outside-tool"}},
                    },
                    str(project): {
                        "mcpServers": {"covered-tool": {"command": "covered-tool"}},
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    report = audit_project_mcp_files(
        config_path=config_path,
        roots=[project],
        backup_root=tmp_path / "backups",
        import_missing=False,
        apply=True,
        profiles=["claude"],
        claude_config_path=claude_config,
    )

    loaded = json.loads(claude_config.read_text(encoding="utf-8"))
    assert report.files_scanned == 1
    assert report.files_changed == [claude_config]
    assert loaded["projects"][str(project)]["mcpServers"] == {}
    assert loaded["projects"][str(outside_project)]["mcpServers"] == {
        "outside-tool": {"command": "outside-tool"}
    }

def test_project_mcp_imports_missing_claude_project_state_entry(tmp_path: Path) -> None:
    from mcp_broker.project_mcp import audit_project_mcp_files

    config_path = tmp_path / "broker.yaml"
    _write_broker_config(config_path)
    project = tmp_path / "project"
    project.mkdir()
    claude_config = tmp_path / "claude.json"
    claude_config.write_text(
        json.dumps(
            {
                "projects": {
                    str(project): {
                        "mcpServers": {"local-tool": {"command": "local-tool"}},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    report = audit_project_mcp_files(
        config_path=config_path,
        roots=[project],
        backup_root=tmp_path / "backups",
        import_missing=True,
        apply=True,
        profiles=["claude"],
        claude_config_path=claude_config,
    )

    assert report.imported_servers == ["local-tool"]
    assert json.loads(claude_config.read_text(encoding="utf-8"))["projects"][str(project)]["mcpServers"] == {}
    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert loaded["upstreams"]["local-tool"]["profiles"] == ["claude"]

@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "must contain a JSON object"),
        ({"projects": []}, "projects must be an object"),
        ({"projects": {"__PROJECT__": []}}, "must be an object"),
        ({"projects": {"__PROJECT__": {"mcpServers": []}}}, "mcpServers must be an object"),
    ],
)
def test_project_mcp_rejects_invalid_claude_config_shape(
    tmp_path: Path,
    payload: object,
    message: str,
) -> None:
    from mcp_broker.project_mcp import audit_project_mcp_files

    config_path = tmp_path / "broker.yaml"
    _write_broker_config(config_path)
    project = tmp_path / "project"
    project.mkdir()
    claude_config = tmp_path / "claude.json"
    materialized_payload = json.loads(json.dumps(payload).replace("__PROJECT__", str(project)))
    claude_config.write_text(json.dumps(materialized_payload), encoding="utf-8")

    with pytest.raises(ValueError, match=re.escape(message)):
        audit_project_mcp_files(
            config_path=config_path,
            roots=[tmp_path],
            backup_root=tmp_path / "backups",
            import_missing=False,
            apply=False,
            profiles=["codex", "claude"],
            claude_config_path=claude_config,
        )

def test_project_mcp_ignores_missing_claude_config_and_missing_project_path(tmp_path: Path) -> None:
    from mcp_broker.project_mcp import _load_claude_project_entries, _path_matches_roots

    missing_projects_config = tmp_path / "empty-claude.json"
    missing_projects_config.write_text("{}", encoding="utf-8")
    missing_servers_config = tmp_path / "missing-servers-claude.json"
    missing_servers_config.write_text(
        json.dumps({"projects": {str(tmp_path): {"otherSetting": True}}}),
        encoding="utf-8",
    )

    assert _load_claude_project_entries(None, [tmp_path]) == []
    assert _load_claude_project_entries(tmp_path / "missing.json", [tmp_path]) == []
    assert _load_claude_project_entries(missing_projects_config, [tmp_path]) == []
    entries = _load_claude_project_entries(missing_servers_config, [tmp_path])
    assert len(entries) == 1
    assert entries[0].servers == {}
    assert entries[0].data == {"projects": {str(tmp_path): {"otherSetting": True}}}
    assert _path_matches_roots(tmp_path / "missing-project", [tmp_path]) is False

@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "must contain a JSON object"),
        ({}, "projects.missing must be an object"),
        ({"projects": []}, "projects must be an object"),
        ({"projects": {"missing": []}}, "projects.missing must be an object"),
    ],
)
def test_project_mcp_empty_claude_writer_revalidates_current_file(
    tmp_path: Path,
    payload: object,
    message: str,
) -> None:
    from mcp_broker.project_mcp import _ProjectMcpFile, _write_empty_mcp_servers

    claude_config = tmp_path / "claude.json"
    claude_config.write_text(json.dumps(payload), encoding="utf-8")
    project_file = _ProjectMcpFile(
        path=claude_config,
        data={},
        servers={"covered-tool": {"command": "covered-tool"}},
        claude_project_path="missing",
    )

    with pytest.raises(ValueError, match=re.escape(message)):
        _write_empty_mcp_servers(project_file)

def test_project_mcp_covered_names_include_upstreams_without_tool_prefix(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig
    from mcp_broker.project_mcp import _covered_server_names

    config = BrokerConfig(
        runtime=RuntimeConfig.from_mapping({"root": str(tmp_path / "runtime")}),
        broker=BrokerSettings(),
        profiles={},
        upstreams={
            "covered-no-prefix": UpstreamConfig(
                name="covered-no-prefix",
                command="covered-no-prefix",
                tool_prefix=None,
            )
        },
    )

    assert _covered_server_names(config) == {"covered-no-prefix"}

def test_project_mcp_covered_names_include_tool_prefix_aliases(tmp_path: Path) -> None:
    from mcp_broker.config import BrokerConfig, BrokerSettings, RuntimeConfig, UpstreamConfig
    from mcp_broker.project_mcp import _covered_server_names

    config = BrokerConfig(
        runtime=RuntimeConfig.from_mapping({"root": str(tmp_path / "runtime")}),
        broker=BrokerSettings(),
        profiles={},
        upstreams={
            "central-upstream-name": UpstreamConfig(
                name="central-upstream-name",
                command="tool",
                tool_prefix="project-local-name",
            )
        },
    )

    assert _covered_server_names(config) == {
        "central-upstream-name",
        "project-local-name",
    }

@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "must contain a JSON object"),
        ({"mcpServers": []}, "mcpServers must be an object"),
    ],
)
def test_project_mcp_rejects_invalid_project_file_shape(
    tmp_path: Path,
    payload: object,
    message: str,
) -> None:
    from mcp_broker.project_mcp import audit_project_mcp_files

    config_path = tmp_path / "broker.yaml"
    _write_broker_config(config_path)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".mcp.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        audit_project_mcp_files(
            config_path=config_path,
            roots=[project],
            backup_root=tmp_path / "backups",
            import_missing=False,
            apply=False,
            profiles=["codex", "claude"],
        )

@pytest.mark.parametrize(
    ("server_config", "message"),
    [
        ([], "server config must be an object"),
        ({"args": []}, "stdio server requires command"),
        ({"command": ["bad"]}, "stdio server requires command"),
        ({"command": "bad", "args": "--serve"}, "args must be a list"),
        ({"command": "bad", "env": []}, "env must be an object"),
        ({"command": "bad", "env": {"BAD-NAME": "GOOD_NAME"}}, "env keys must be environment variable names"),
        ({"command": "bad", "env": {"TOKEN": ""}}, "env.TOKEN must reference an environment variable"),
        ({"type": "http"}, "http server requires url"),
        ({"type": "sse"}, "http server requires url"),
        ({"type": "http", "url": ["bad"]}, "http server requires url"),
        ({"type": "http", "url": "https://example.invalid/mcp", "headers": []}, "headers must be an object"),
        (
            {
                "type": "http",
                "url": "https://example.invalid/mcp",
                "headers": {"Authorization": "Bearer literal"},
            },
            "headers.Authorization must reference an environment variable",
        ),
    ],
)
def test_project_mcp_import_reports_invalid_server_shapes(
    tmp_path: Path,
    server_config: object,
    message: str,
) -> None:
    from mcp_broker.project_mcp import audit_project_mcp_files

    config_path = tmp_path / "broker.yaml"
    _write_broker_config(config_path)
    project = tmp_path / "project"
    project.mkdir()
    _write_project_mcp(project / ".mcp.json", {"bad-tool": server_config})

    report = audit_project_mcp_files(
        config_path=config_path,
        roots=[project],
        backup_root=tmp_path / "backups",
        import_missing=True,
        apply=True,
        profiles=["codex", "claude"],
    )

    assert report.files_changed == []
    assert report.files_blocked == [project / ".mcp.json"]
    assert report.missing_servers == ["bad-tool"]
    assert report.import_errors == {"bad-tool": message}

def test_project_mcp_import_reports_later_invalid_servers_after_first_invalid(
    tmp_path: Path,
) -> None:
    from mcp_broker.project_mcp import audit_project_mcp_files

    config_path = tmp_path / "broker.yaml"
    _write_broker_config(config_path)
    project = tmp_path / "project"
    project.mkdir()
    _write_project_mcp(
        project / ".mcp.json",
        {
            "bad-stdio": {"args": []},
            "bad-http": {"type": "http"},
        },
    )

    report = audit_project_mcp_files(
        config_path=config_path,
        roots=[project],
        backup_root=tmp_path / "backups",
        import_missing=True,
        apply=True,
        profiles=["codex", "claude"],
    )

    assert report.files_changed == []
    assert report.files_blocked == [project / ".mcp.json"]
    assert report.missing_servers == ["bad-http", "bad-stdio"]
    assert report.import_errors == {
        "bad-http": "http server requires url",
        "bad-stdio": "stdio server requires command",
    }

def test_project_mcp_header_parser_rejects_programmatic_non_string_keys() -> None:
    from mcp_broker.project_mcp import _parse_header_mapping

    assert _parse_header_mapping({42: "TOKEN"}) == ({}, "header keys must be strings")

def test_project_mcp_env_parser_accepts_common_env_reference_forms() -> None:
    from mcp_broker.project_mcp import _parse_env_mapping

    parsed, error = _parse_env_mapping(
        {
            "TOKEN": "SOURCE_TOKEN",
            "ALT_TOKEN": "$ALT_SOURCE_TOKEN",
            "BRACED_TOKEN": "${BRACED_SOURCE_TOKEN}",
            "AUTH_HEADER": "Bearer ${AUTH_SOURCE_TOKEN}",
        }
    )

    assert error is None
    assert parsed == {
        "TOKEN": "SOURCE_TOKEN",
        "ALT_TOKEN": "ALT_SOURCE_TOKEN",
        "BRACED_TOKEN": "BRACED_SOURCE_TOKEN",
        "AUTH_HEADER": "AUTH_SOURCE_TOKEN",
    }

@pytest.mark.parametrize(
    ("env_value", "message"),
    [
        ("literal-secret-value", "env.TOKEN must reference an environment variable"),
        ("Bearer literal-secret-value", "env.TOKEN must reference an environment variable"),
        ("", "env.TOKEN must reference an environment variable"),
        (42, "env.TOKEN must reference an environment variable"),
    ],
)
def test_project_mcp_env_parser_rejects_literal_values(
    env_value: object,
    message: str,
) -> None:
    from mcp_broker.project_mcp import _parse_env_mapping

    assert _parse_env_mapping({"TOKEN": env_value}) == ({}, message)

def test_project_mcp_env_parser_rejects_invalid_target_names() -> None:
    from mcp_broker.project_mcp import _parse_env_mapping

    assert _parse_env_mapping({"not-valid-name": "SOURCE_TOKEN"}) == (
        {},
        "env keys must be environment variable names",
    )

def test_project_mcp_header_parser_normalizes_header_names() -> None:
    from mcp_broker.project_mcp import _parse_header_mapping

    parsed, error = _parse_header_mapping(
        {
            "Authorization": "AUTH_TOKEN",
            "x-api-key": "${API_KEY_TOKEN}",
            "X-Trace-Id": "TRACE_TOKEN",
            "!!!": "$FALLBACK_HEADER_TOKEN",
        }
    )

    assert error is None
    assert parsed == {
        "AUTHORIZATION": "AUTH_TOKEN",
        "X_API_KEY": "API_KEY_TOKEN",
        "X_TRACE_ID": "TRACE_TOKEN",
        "HEADER": "FALLBACK_HEADER_TOKEN",
    }

def test_project_mcp_header_parser_rejects_literal_header_values() -> None:
    from mcp_broker.project_mcp import _parse_header_mapping

    assert _parse_header_mapping({"Authorization": "Bearer literal-secret"}) == (
        {},
        "headers.Authorization must reference an environment variable",
    )

def test_project_mcp_import_extracts_bare_environment_source(tmp_path: Path) -> None:
    from mcp_broker.project_mcp import audit_project_mcp_files

    config_path = tmp_path / "broker.yaml"
    _write_broker_config(config_path)
    project = tmp_path / "project"
    project.mkdir()
    _write_project_mcp(
        project / ".mcp.json",
        {"local-tool": {"command": "local-tool", "env": {"TOKEN": "SOURCE_TOKEN"}}},
    )

    report = audit_project_mcp_files(
        config_path=config_path,
        roots=[project],
        backup_root=tmp_path / "backups",
        import_missing=True,
        apply=True,
        profiles=["manual-test"],
    )

    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert report.imported_servers == ["local-tool"]
    assert loaded["upstreams"]["local-tool"]["profiles"] == ["manual-test"]
    assert loaded["upstreams"]["local-tool"]["env"] == {"TOKEN": "SOURCE_TOKEN"}
