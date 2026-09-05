import os
from argparse import Namespace
from io import BytesIO
from pathlib import Path
import pytest
pytestmark = [pytest.mark.unit, pytest.mark.error_simulation]
class _BinaryConsole:
    def __init__(self, payload: bytes = b"") -> None:
        self.buffer = BytesIO(payload)
        self.text = ""

    def write(self, text: str) -> int:
        self.text += text
        return len(text)

    def flush(self) -> None:
        return None
def _stdio_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "broker.yaml"
    runtime_root = tmp_path / "runtime"
    config_path.write_text(
        f"""
runtime:
  root: {runtime_root}
  socket_path: {_stdio_socket(tmp_path)}
  state_dir: {runtime_root / "state"}
broker:
  tool_namespace_separator: "."
profiles:
  generic-client:
    max_tools: 200
    compact_tools_enabled: false
upstreams: {{}}
""".strip(),
        encoding="utf-8",
    )
    return config_path
def _stdio_socket(tmp_path: Path) -> Path:
    return Path("/tmp") / f"mcp-broker-cli-{os.getpid()}-{tmp_path.name}.sock"

def test_top_level_cli_deployment_stage_delegates_bundle_and_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import cli

    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "deployments_main", lambda argv: calls.append(argv) or 0)

    assert (
        cli.handle_deployment(
            Namespace(
                deployment_command="stage",
                state_dir=tmp_path / "state",
                bundle=tmp_path / "bundle.json",
                dry_run=True,
            )
        )
        == 0
    )
    assert (
        cli.handle_deployment(
            Namespace(
                deployment_command="rollback",
                state_dir=tmp_path / "state",
                bundle=None,
                dry_run=False,
            )
        )
        == 0
    )

    assert calls == [
        [
            "stage",
            "--state-dir",
            str(tmp_path / "state"),
            "--bundle",
            str(tmp_path / "bundle.json"),
            "--dry-run",
        ],
        ["rollback", "--state-dir", str(tmp_path / "state")],
    ]

def test_top_level_cli_deployment_stage_omits_dry_run_when_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import cli

    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "deployments_main", lambda argv: calls.append(argv) or 0)

    assert (
        cli.handle_deployment(
            Namespace(
                deployment_command="stage",
                state_dir=tmp_path / "state",
                bundle=tmp_path / "bundle.json",
                dry_run=False,
            )
        )
        == 0
    )

    assert calls == [
        [
            "stage",
            "--state-dir",
            str(tmp_path / "state"),
            "--bundle",
            str(tmp_path / "bundle.json"),
        ]
    ]

def test_top_level_cli_break_glass_create_delegates_control_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import cli

    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "break_glass_main", lambda argv: calls.append(argv) or 0)

    assert (
        cli.handle_break_glass(
            Namespace(
                break_glass_command="create",
                state_dir=tmp_path / "state",
                reason="maintenance window",
                operator="release-operator",
                expires_at="2026-07-04T06:30:00Z",
                bypass_policy=["policy-a", "policy-b"],
            )
        )
        == 0
    )

    assert calls == [
        [
            "create",
            "--state-dir",
            str(tmp_path / "state"),
            "--reason",
            "maintenance window",
            "--operator",
            "release-operator",
            "--expires-at",
            "2026-07-04T06:30:00Z",
            "--bypass-policy",
            "policy-a",
            "--bypass-policy",
            "policy-b",
        ]
    ]

def test_top_level_cli_rollout_simulator_delegates_approved_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import cli

    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "rollout_simulator_main", lambda argv: calls.append(argv) or 0)

    assert (
        cli.handle_rollout_simulator(
            Namespace(
                bundle=tmp_path / "bundle.json",
                fleet_status=tmp_path / "fleet.json",
                approved=True,
            )
        )
        == 0
    )

    assert calls == [
        [
            "--bundle",
            str(tmp_path / "bundle.json"),
            "--fleet-status",
            str(tmp_path / "fleet.json"),
            "--approved",
        ]
    ]

def test_top_level_cli_rollout_simulator_omits_approved_flag_when_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import cli

    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "rollout_simulator_main", lambda argv: calls.append(argv) or 0)

    assert (
        cli.handle_rollout_simulator(
            Namespace(
                bundle=tmp_path / "bundle.json",
                fleet_status=tmp_path / "fleet.json",
                approved=False,
            )
        )
        == 0
    )

    assert calls == [
        [
            "--bundle",
            str(tmp_path / "bundle.json"),
            "--fleet-status",
            str(tmp_path / "fleet.json"),
        ]
    ]

def test_top_level_cli_runtime_artifact_verify_reports_exclusive_input_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mcp_broker import cli

    assert (
        cli.handle_runtime_artifact_verify(
            Namespace(
                metadata=tmp_path / "runtime-metadata.json",
                artifact=tmp_path / "runtime.zip",
                digest="sha256:abc123",
            )
        )
        == 1
    )
    assert "either --metadata or --artifact with --digest" in capsys.readouterr().err

    assert (
        cli.handle_runtime_artifact_verify(
            Namespace(metadata=None, artifact=None, digest=None)
        )
        == 1
    )
    assert "requires --artifact and --digest" in capsys.readouterr().err

def test_top_level_cli_service_plan_delegates_service_template_args(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_broker import cli

    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "service_templates_main", lambda argv: calls.append(argv) or 0)

    assert (
        cli.handle_service_plan(
            Namespace(
                platform="linux",
                runtime_root=tmp_path / "runtime",
                socket_path=tmp_path / "runtime" / "broker.sock",
                config=tmp_path / "broker.yaml",
                daemon_command="mcp-broker-daemon",
                home_dir=tmp_path / "home",
            )
        )
        == 0
    )

    assert calls == [
        [
            "--platform",
            "linux",
            "--runtime-root",
            str(tmp_path / "runtime"),
            "--socket-path",
            str(tmp_path / "runtime" / "broker.sock"),
            "--config",
            str(tmp_path / "broker.yaml"),
            "--daemon-command",
            "mcp-broker-daemon",
            "--home-dir",
            str(tmp_path / "home"),
        ]
    ]
