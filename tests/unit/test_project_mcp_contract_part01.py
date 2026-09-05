from pathlib import Path
import json
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

def test_project_mcp_audit_reports_covered_and_missing_servers(tmp_path: Path) -> None:
    from mcp_broker.project_mcp import audit_project_mcp_files

    config_path = tmp_path / "broker.yaml"
    _write_broker_config(config_path)
    project = tmp_path / "project"
    project.mkdir()
    _write_project_mcp(
        project / ".mcp.json",
        {
            "covered-tool": {"command": "covered-tool"},
            "missing-tool": {"command": "missing-tool", "args": ["--serve"]},
        },
    )

    report = audit_project_mcp_files(
        config_path=config_path,
        roots=[tmp_path],
        backup_root=tmp_path / "backups",
        import_missing=False,
        apply=False,
        profiles=["codex", "claude"],
    )

    assert report.apply is False
    assert report.files_scanned == 1
    assert report.covered_servers == ["covered-tool"]
    assert report.missing_servers == ["missing-tool"]
    assert report.files_changed == []
    assert report.files_blocked == [project / ".mcp.json"]

def test_project_mcp_apply_empties_only_fully_covered_files_and_creates_backup(
    tmp_path: Path,
) -> None:
    from mcp_broker.project_mcp import audit_project_mcp_files

    config_path = tmp_path / "broker.yaml"
    _write_broker_config(config_path)
    backup_root = tmp_path / "00-backups"
    covered_project = tmp_path / "covered"
    blocked_project = tmp_path / "blocked"
    covered_project.mkdir()
    blocked_project.mkdir()
    covered_file = covered_project / ".mcp.json"
    blocked_file = blocked_project / ".mcp.json"
    _write_project_mcp(covered_file, {"covered-tool": {"command": "covered-tool"}})
    _write_project_mcp(blocked_file, {"missing-tool": {"command": "missing-tool"}})

    report = audit_project_mcp_files(
        config_path=config_path,
        roots=[tmp_path],
        backup_root=backup_root,
        import_missing=False,
        apply=True,
        profiles=["codex", "claude"],
    )

    assert report.files_changed == [covered_file]
    assert report.files_blocked == [blocked_file]
    assert json.loads(covered_file.read_text(encoding="utf-8")) == {"mcpServers": {}}
    assert json.loads(blocked_file.read_text(encoding="utf-8"))["mcpServers"] == {
        "missing-tool": {"command": "missing-tool"}
    }
    assert len(report.backups) == 1
    assert report.backups[0].exists()
    assert "covered-tool" in report.backups[0].read_text(encoding="utf-8")

def test_project_mcp_apply_creates_nested_backup_root(tmp_path: Path) -> None:
    from mcp_broker.project_mcp import audit_project_mcp_files

    config_path = tmp_path / "broker.yaml"
    _write_broker_config(config_path)
    project = tmp_path / "project"
    project.mkdir()
    covered_file = project / ".mcp.json"
    backup_root = tmp_path / "nested" / "backup" / "root"
    _write_project_mcp(covered_file, {"covered-tool": {"command": "covered-tool"}})

    report = audit_project_mcp_files(
        config_path=config_path,
        roots=[project],
        backup_root=backup_root,
        import_missing=False,
        apply=True,
        profiles=["codex", "claude"],
    )

    assert len(report.backups) == 1
    assert report.backups[0].parent == backup_root
    assert report.backups[0].read_text(encoding="utf-8") == (
        '{\n  "mcpServers": {\n    "covered-tool": {\n      "command": "covered-tool"\n    }\n  }\n}\n'
    )

def test_project_mcp_dry_run_fully_covered_file_changes_nothing(tmp_path: Path) -> None:
    from mcp_broker.project_mcp import audit_project_mcp_files

    config_path = tmp_path / "broker.yaml"
    _write_broker_config(config_path)
    project = tmp_path / "project"
    project.mkdir()
    project_file = project / ".mcp.json"
    _write_project_mcp(project_file, {"covered-tool": {"command": "covered-tool"}})
    before = project_file.read_text(encoding="utf-8")

    report = audit_project_mcp_files(
        config_path=config_path,
        roots=[project],
        backup_root=tmp_path / "backups",
        import_missing=False,
        apply=False,
        profiles=["codex", "claude"],
    )

    assert report.files_changed == []
    assert report.files_blocked == []
    assert report.backups == []
    assert project_file.read_text(encoding="utf-8") == before

def test_project_mcp_imports_missing_stdio_and_http_servers_before_emptying(
    tmp_path: Path,
) -> None:
    from mcp_broker.config import BrokerConfig
    from mcp_broker.project_mcp import audit_project_mcp_files

    config_path = tmp_path / "broker.yaml"
    _write_broker_config(config_path)
    project = tmp_path / "project"
    project.mkdir()
    mcp_file = project / ".mcp.json"
    _write_project_mcp(
        mcp_file,
        {
            "local-tool": {
                "command": "local-tool",
                "args": ["serve"],
                "env": {"LOCAL_TOKEN": "${LOCAL_TOKEN}"},
            },
            "remote-tool": {
                "type": "http",
                "url": "https://example.invalid/mcp",
                "headers": {"Authorization": "Bearer ${REMOTE_TOKEN}"},
            },
        },
    )

    report = audit_project_mcp_files(
        config_path=config_path,
        roots=[tmp_path],
        backup_root=tmp_path / "backups",
        import_missing=True,
        apply=True,
        profiles=["codex", "claude"],
    )

    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert sorted(report.imported_servers) == ["local-tool", "remote-tool"]
    assert json.loads(mcp_file.read_text(encoding="utf-8")) == {"mcpServers": {}}
    assert loaded["upstreams"]["local-tool"]["command"] == "local-tool"
    assert loaded["upstreams"]["local-tool"]["args"] == ["serve"]
    assert loaded["upstreams"]["local-tool"]["env"] == {"LOCAL_TOKEN": "LOCAL_TOKEN"}
    assert loaded["upstreams"]["local-tool"]["tool_prefix"] == "local-tool"
    assert loaded["upstreams"]["local-tool"]["state_dir"] == "upstreams/local-tool"
    assert (
        loaded["upstreams"]["local-tool"]["purpose"]
        == "Imported from project-local .mcp.json entry local-tool."
    )
    assert loaded["upstreams"]["remote-tool"]["transport"] == "http"
    assert loaded["upstreams"]["remote-tool"]["command"] == "https://example.invalid/mcp"
    assert loaded["upstreams"]["remote-tool"]["env"] == {
        "AUTHORIZATION": "REMOTE_TOKEN",
    }
    assert loaded["upstreams"]["remote-tool"]["tool_prefix"] == "remote-tool"
    assert loaded["upstreams"]["remote-tool"]["state_dir"] == "upstreams/remote-tool"
    assert (
        loaded["upstreams"]["remote-tool"]["purpose"]
        == "Imported from project-local .mcp.json entry remote-tool."
    )
    BrokerConfig.from_file(config_path)

def test_project_mcp_import_keeps_first_definition_for_duplicate_missing_server(
    tmp_path: Path,
) -> None:
    from mcp_broker.project_mcp import audit_project_mcp_files

    config_path = tmp_path / "broker.yaml"
    _write_broker_config(config_path)
    first_project = tmp_path / "a-first-project"
    second_project = tmp_path / "b-second-project"
    first_project.mkdir()
    second_project.mkdir()
    _write_project_mcp(
        first_project / ".mcp.json",
        {"local-tool": {"command": "first-command", "args": ["first"]}},
    )
    _write_project_mcp(
        second_project / ".mcp.json",
        {"local-tool": {"command": "second-command", "args": ["second"]}},
    )

    report = audit_project_mcp_files(
        config_path=config_path,
        roots=[tmp_path],
        backup_root=tmp_path / "backups",
        import_missing=True,
        apply=True,
        profiles=["codex", "claude"],
    )

    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert report.imported_servers == ["local-tool"]
    assert loaded["upstreams"]["local-tool"]["command"] == "first-command"
    assert loaded["upstreams"]["local-tool"]["args"] == ["first"]

def test_project_mcp_imports_http_server_without_headers(tmp_path: Path) -> None:
    from mcp_broker.project_mcp import audit_project_mcp_files

    config_path = tmp_path / "broker.yaml"
    _write_broker_config(config_path)
    project = tmp_path / "project"
    project.mkdir()
    _write_project_mcp(
        project / ".mcp.json",
        {"remote-tool": {"url": "https://example.invalid/mcp"}},
    )

    report = audit_project_mcp_files(
        config_path=config_path,
        roots=[project],
        backup_root=tmp_path / "backups",
        import_missing=True,
        apply=True,
        profiles=["codex"],
    )

    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert report.imported_servers == ["remote-tool"]
    assert "env" not in loaded["upstreams"]["remote-tool"]

def test_project_mcp_refuses_literal_secret_import(tmp_path: Path) -> None:
    from mcp_broker.project_mcp import audit_project_mcp_files

    config_path = tmp_path / "broker.yaml"
    _write_broker_config(config_path)
    project = tmp_path / "project"
    project.mkdir()
    mcp_file = project / ".mcp.json"
    _write_project_mcp(
        mcp_file,
        {"bad-secret": {"command": "bad-secret", "env": {"TOKEN": "literal-value"}}},
    )

    report = audit_project_mcp_files(
        config_path=config_path,
        roots=[tmp_path],
        backup_root=tmp_path / "backups",
        import_missing=True,
        apply=True,
        profiles=["codex", "claude"],
    )

    assert report.files_changed == []
    assert report.files_blocked == [mcp_file]
    assert report.import_errors == {
        "bad-secret": "env.TOKEN must reference an environment variable"
    }
    assert json.loads(mcp_file.read_text(encoding="utf-8"))["mcpServers"]["bad-secret"]

def test_project_mcp_main_outputs_json_for_dry_run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.project_mcp import main

    config_path = tmp_path / "broker.yaml"
    _write_broker_config(config_path)

    assert (
        main(
            [
                "--config",
                str(config_path),
                "--root",
                str(tmp_path),
                "--backup-root",
                str(tmp_path / "backups"),
            ]
        )
        == 0
    )

    assert json.loads(capsys.readouterr().out)["files_scanned"] == 0

def test_project_mcp_main_outputs_sorted_json_keys(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.project_mcp import main

    config_path = tmp_path / "broker.yaml"
    _write_broker_config(config_path)

    assert (
        main(
            [
                "--config",
                str(config_path),
                "--root",
                str(tmp_path),
                "--backup-root",
                str(tmp_path / "backups"),
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert output.index('"backups"') < output.index('"import_missing"')

def test_project_mcp_main_defaults_profiles_and_returns_blocked_exit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.project_mcp import main

    config_path = tmp_path / "broker.yaml"
    _write_broker_config(config_path)
    project = tmp_path / "project"
    project.mkdir()
    _write_project_mcp(project / ".mcp.json", {"missing-tool": {"command": "missing-tool"}})

    assert (
        main(
            [
                "--config",
                str(config_path),
                "--root",
                str(project),
                "--backup-root",
                str(tmp_path / "backups"),
            ]
        )
        == 2
    )

    output = json.loads(capsys.readouterr().out)
    assert output["files_blocked"] == [str(project / ".mcp.json")]

def test_project_mcp_report_preserves_import_missing_flag(tmp_path: Path) -> None:
    from mcp_broker.project_mcp import audit_project_mcp_files

    config_path = tmp_path / "broker.yaml"
    _write_broker_config(config_path)

    dry_report = audit_project_mcp_files(
        config_path=config_path,
        roots=[tmp_path],
        backup_root=tmp_path / "backups",
        import_missing=False,
        apply=False,
        profiles=["codex", "claude"],
    )
    import_report = audit_project_mcp_files(
        config_path=config_path,
        roots=[tmp_path],
        backup_root=tmp_path / "backups",
        import_missing=True,
        apply=False,
        profiles=["codex", "claude"],
    )

    assert dry_report.import_missing is False
    assert dry_report.to_jsonable()["import_missing"] is False
    assert import_report.import_missing is True
    assert import_report.to_jsonable()["import_missing"] is True

def test_project_mcp_main_accepts_explicit_profile(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.project_mcp import main

    config_path = tmp_path / "broker.yaml"
    _write_broker_config(config_path)
    project = tmp_path / "project"
    project.mkdir()
    _write_project_mcp(project / ".mcp.json", {"local-tool": {"command": "local-tool"}})

    assert (
        main(
            [
                "--config",
                str(config_path),
                "--root",
                str(project),
                "--backup-root",
                str(tmp_path / "backups"),
                "--profile",
                "manual-test",
                "--import-missing",
                "--apply",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["imported_servers"] == ["local-tool"]
    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert loaded["upstreams"]["local-tool"]["profiles"] == ["manual-test"]

def test_project_mcp_main_passes_claude_config_to_audit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker.project_mcp import main

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

    assert (
        main(
            [
                "--config",
                str(config_path),
                "--root",
                str(project),
                "--backup-root",
                str(tmp_path / "backups"),
                "--claude-config",
                str(claude_config),
                "--import-missing",
                "--apply",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["files_scanned"] == 1
    assert output["imported_servers"] == ["local-tool"]
    assert json.loads(claude_config.read_text(encoding="utf-8"))["projects"][str(project)]["mcpServers"] == {}

def test_project_mcp_scan_skips_missing_backup_and_dependency_paths(tmp_path: Path) -> None:
    from mcp_broker.project_mcp import audit_project_mcp_files

    config_path = tmp_path / "broker.yaml"
    _write_broker_config(config_path)
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    dependency_dir = tmp_path / "node_modules" / "package"
    dependency_dir.mkdir(parents=True)
    git_dir = tmp_path / ".git" / "fixtures"
    git_dir.mkdir(parents=True)
    venv_dir = tmp_path / "venv-mcp-broker" / "fixtures"
    venv_dir.mkdir(parents=True)
    empty_project = tmp_path / "10-empty"
    empty_project.mkdir()
    covered_project = tmp_path / "20-covered"
    covered_project.mkdir()
    _write_project_mcp(backup_root / ".mcp.json", {"covered-tool": {"command": "covered-tool"}})
    _write_project_mcp(dependency_dir / ".mcp.json", {"covered-tool": {"command": "covered-tool"}})
    _write_project_mcp(git_dir / ".mcp.json", {"covered-tool": {"command": "covered-tool"}})
    _write_project_mcp(venv_dir / ".mcp.json", {"covered-tool": {"command": "covered-tool"}})
    _write_project_mcp(empty_project / ".mcp.json", {})
    covered_file = covered_project / ".mcp.json"
    _write_project_mcp(covered_file, {"covered-tool": {"command": "covered-tool"}})

    report = audit_project_mcp_files(
        config_path=config_path,
        roots=[tmp_path / "does-not-exist", tmp_path],
        backup_root=backup_root,
        import_missing=False,
        apply=True,
        profiles=["codex", "claude"],
    )

    assert report.files_scanned == 2
    assert report.files_changed == [covered_file]
    assert report.files_blocked == []
    assert len(report.backups) == 1
