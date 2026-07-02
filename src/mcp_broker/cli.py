"""Top-level CLI for package installs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import os
import shutil
import sys
from threading import Event
from typing import Callable, Sequence

from mcp_broker.bootstrap_transactions import main as bootstrap_transactions_main
from mcp_broker.break_glass import main as break_glass_main
from mcp_broker.client import ClientShim, ClientShimError
from mcp_broker.bundle_loader import main as bundle_loader_main
from mcp_broker.config import BrokerConfig
from mcp_broker.config_layers import main as config_layers_main
from mcp_broker.config_render import main as config_render_main
from mcp_broker.daemon import BrokerDaemon, BrokerDaemonError, main as daemon_main
from mcp_broker.deployments import main as deployments_main
from mcp_broker.cli_fleet_status import add_fleet_status_parser
from mcp_broker.cli_governance import add_governance_parser
from mcp_broker.rollout_simulator import main as rollout_simulator_main
from mcp_broker.runtime_artifact import RuntimeArtifactError, RuntimeArtifactVerifier
from mcp_broker.runtime_launcher import ActiveRuntimeLauncher, RuntimeLauncherError
from mcp_broker.service_templates import main as service_templates_main


DaemonRunner = Callable[[Sequence[str] | None], int]
RenderRunner = Callable[[Sequence[str] | None], int]


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Initialize, run, and inspect mcp-broker")
    subparsers = parser.add_subparsers(dest="command", required=True)

    _add_init_parser(subparsers)
    _add_daemon_parsers(subparsers)
    _add_config_parser(subparsers)
    _add_render_parser(subparsers)
    _add_bundle_parser(subparsers)
    _add_deployment_parser(subparsers)
    _add_break_glass_parser(subparsers)
    add_governance_parser(subparsers)
    add_fleet_status_parser(subparsers)
    _add_rollout_parser(subparsers)
    _add_runtime_parser(subparsers)
    _add_service_parser(subparsers)

    return parser


def _add_init_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    init_parser = subparsers.add_parser("init", help="Create a private config from the public example")
    init_parser.add_argument("--config", type=Path, default=default_config_path())
    init_parser.add_argument("--template", type=Path)
    init_parser.add_argument("--force", action="store_true")
    init_parser.set_defaults(handler=handle_init)


def _add_daemon_parsers(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    start_parser = _daemon_parser(subparsers, "start", "Start the broker daemon in the foreground")
    start_parser.add_argument("--config", type=Path, default=default_config_path())
    start_parser.set_defaults(handler=handle_daemon)

    stdio_parser = _daemon_parser(
        subparsers,
        "stdio",
        "Run the broker daemon and stdio client in one process",
    )
    stdio_parser.add_argument("--config", type=Path, default=default_config_path())
    stdio_parser.add_argument("--profile", default=os.environ.get("MCP_BROKER_PROFILE"))
    stdio_parser.add_argument("--init-if-missing", action="store_true")
    stdio_parser.add_argument(
        "--ready-attempts",
        type=int,
        default=int(os.environ.get("MCP_BROKER_READY_ATTEMPTS", "50")),
    )
    stdio_parser.set_defaults(handler=handle_stdio)

    status_parser = _daemon_parser(subparsers, "status", "Query broker daemon status")
    status_parser.set_defaults(handler=handle_daemon)

    stop_parser = _daemon_parser(subparsers, "stop", "Ask the broker daemon to stop")
    stop_parser.set_defaults(handler=handle_daemon)


def _add_render_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    render_parser = subparsers.add_parser("render", help="Render one client config")
    render_parser.add_argument("client")
    render_parser.add_argument("--config", type=Path, default=default_config_path())
    render_parser.add_argument("--dry-run", action="store_true", default=True)
    render_parser.add_argument("--apply", action="store_true")
    render_parser.add_argument("--target-path", type=Path)
    render_parser.set_defaults(handler=handle_render)


def _add_config_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    config_parser = subparsers.add_parser("config", help="Inspect and compose broker config")
    config_subparsers = config_parser.add_subparsers(dest="config_command", required=True)
    compose_parser = config_subparsers.add_parser(
        "compose",
        help="Compose org, team, add-on, and user config layers without changing runtime state",
    )
    compose_parser.add_argument("--org", type=Path)
    compose_parser.add_argument("--team", type=Path)
    compose_parser.add_argument("--addon", action="append", default=[], type=Path)
    compose_parser.add_argument("--user", type=Path)
    compose_parser.set_defaults(handler=handle_config_compose)


def _add_bundle_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    bundle_parser = subparsers.add_parser("bundle", help="Inspect and validate desired-state bundles")
    bundle_subparsers = bundle_parser.add_subparsers(dest="bundle_command", required=True)
    bundle_validate_parser = bundle_subparsers.add_parser(
        "validate",
        help="Validate a desired-state bundle without changing runtime state",
    )
    bundle_validate_parser.add_argument("--bundle", required=True, type=Path)
    bundle_validate_parser.set_defaults(handler=handle_bundle_validate)


def _add_deployment_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    deployment_parser = subparsers.add_parser(
        "deployment",
        help="Manage desired-state deployment records",
    )
    deployment_subparsers = deployment_parser.add_subparsers(
        dest="deployment_command",
        required=True,
    )
    stage_parser = deployment_subparsers.add_parser(
        "stage",
        help="Validate and record a bundle deployment",
    )
    stage_parser.add_argument("--bundle", required=True, type=Path)
    stage_parser.add_argument("--state-dir", required=True, type=Path)
    stage_parser.add_argument("--dry-run", action="store_true")
    stage_parser.set_defaults(handler=handle_deployment)
    rollback_parser = deployment_subparsers.add_parser(
        "rollback",
        help="Roll back to the previous deployment",
    )
    rollback_parser.add_argument("--state-dir", required=True, type=Path)
    rollback_parser.set_defaults(handler=handle_deployment)
    recover_parser = deployment_subparsers.add_parser(
        "recover",
        help="Recover deployment state after partial writes",
    )
    recover_parser.add_argument("--state-dir", required=True, type=Path)
    recover_parser.set_defaults(handler=handle_deployment)


def _add_break_glass_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    break_glass_parser = subparsers.add_parser(
        "break-glass",
        help="Manage local break-glass audit records",
    )
    break_glass_subparsers = break_glass_parser.add_subparsers(
        dest="break_glass_command",
        required=True,
    )
    create_parser = break_glass_subparsers.add_parser(
        "create",
        help="Create an expiring break-glass audit record",
    )
    create_parser.add_argument("--state-dir", required=True, type=Path)
    create_parser.add_argument("--reason", required=True)
    create_parser.add_argument("--operator", required=True)
    create_parser.add_argument("--expires-at", required=True)
    create_parser.add_argument("--bypass-policy", action="append", required=True, default=[])
    create_parser.set_defaults(handler=handle_break_glass)
    status_parser = break_glass_subparsers.add_parser(
        "status",
        help="Report active break-glass status",
    )
    status_parser.add_argument("--state-dir", required=True, type=Path)
    status_parser.set_defaults(handler=handle_break_glass)


def _add_rollout_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    rollout_parser = subparsers.add_parser(
        "rollout",
        help="Simulate governance rollout decisions locally",
    )
    rollout_subparsers = rollout_parser.add_subparsers(
        dest="rollout_command",
        required=True,
    )
    simulate_parser = rollout_subparsers.add_parser(
        "simulate",
        help="Simulate canary, staged rollout, rollback, and compatibility decisions",
    )
    simulate_parser.add_argument("--bundle", required=True, type=Path)
    simulate_parser.add_argument("--fleet-status", required=True, type=Path)
    simulate_parser.add_argument("--approved", action="store_true")
    simulate_parser.set_defaults(handler=handle_rollout_simulator)


def _add_runtime_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    runtime_parser = subparsers.add_parser(
        "runtime",
        help="Inspect installed runtime launcher state",
    )
    runtime_subparsers = runtime_parser.add_subparsers(
        dest="runtime_command",
        required=True,
    )
    launch_plan_parser = runtime_subparsers.add_parser(
        "launch-plan",
        help="Print the active installed runtime argv without executing it",
    )
    launch_plan_parser.add_argument("--state-dir", required=True, type=Path)
    launch_plan_parser.add_argument("runtime_args", nargs=argparse.REMAINDER)
    launch_plan_parser.set_defaults(handler=handle_runtime_launch_plan)
    artifact_parser = runtime_subparsers.add_parser(
        "artifact-verify",
        help="Verify runtime artifact digest and archive safety",
    )
    artifact_parser.add_argument("--artifact", type=Path)
    artifact_parser.add_argument("--digest")
    artifact_parser.add_argument("--metadata", type=Path)
    artifact_parser.set_defaults(handler=handle_runtime_artifact_verify)
    bootstrap_parser = runtime_subparsers.add_parser(
        "bootstrap",
        help="Run approval-gated runtime bootstrap transactions",
    )
    bootstrap_subparsers = bootstrap_parser.add_subparsers(
        dest="bootstrap_command",
        required=True,
    )
    for command in ("preflight", "plan", "apply"):
        command_parser = bootstrap_subparsers.add_parser(command)
        command_parser.add_argument("--metadata", required=True, type=Path)
        command_parser.add_argument("--state-dir", required=True, type=Path)
        if command == "apply":
            command_parser.add_argument("--approved", action="store_true")
        command_parser.set_defaults(handler=handle_runtime_bootstrap)
    status_parser = bootstrap_subparsers.add_parser("status")
    status_parser.add_argument("--state-dir", required=True, type=Path)
    status_parser.set_defaults(handler=handle_runtime_bootstrap)
    for command in ("rollback", "uninstall"):
        command_parser = bootstrap_subparsers.add_parser(command)
        command_parser.add_argument("--state-dir", required=True, type=Path)
        command_parser.add_argument("--approved", action="store_true")
        command_parser.set_defaults(handler=handle_runtime_bootstrap)


def _add_service_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    service_parser = subparsers.add_parser(
        "service",
        help="Render dry-run service manager plans",
    )
    service_subparsers = service_parser.add_subparsers(
        dest="service_command",
        required=True,
    )
    plan_parser = service_subparsers.add_parser(
        "plan",
        help="Print a non-mutating service manager plan",
    )
    plan_parser.add_argument(
        "--platform",
        required=True,
        choices=("macos", "linux", "windows"),
    )
    plan_parser.add_argument("--runtime-root", required=True, type=Path)
    plan_parser.add_argument("--socket-path", required=True, type=Path)
    plan_parser.add_argument("--config", required=True, type=Path)
    plan_parser.add_argument("--daemon-command", required=True)
    plan_parser.add_argument("--home-dir", required=True, type=Path)
    plan_parser.set_defaults(handler=handle_service_plan)


def _daemon_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    command: str,
    help_text: str,
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(command, help=help_text)
    parser.add_argument("--runtime-root", type=Path, default=default_runtime_root())
    parser.add_argument("--socket-path", type=Path, default=default_socket_path())
    return parser


def default_runtime_root() -> Path:
    configured = os.environ.get("MCP_BROKER_RUNTIME_ROOT")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "mcp" / "mcp-broker"


def default_socket_path() -> Path:
    configured = os.environ.get("MCP_BROKER_SOCKET")
    if configured:
        return Path(configured).expanduser()
    return default_runtime_root() / "sockets" / "broker.sock"


def default_config_path() -> Path:
    configured = os.environ.get("MCP_BROKER_CONFIG")
    if configured:
        return Path(configured).expanduser()
    return default_runtime_root() / "config" / "broker.yaml"


def default_template_path() -> Path:
    source_tree_template = Path(__file__).resolve().parents[2] / "config" / "broker.example.yaml"
    current_tree_template = Path.cwd() / "config" / "broker.example.yaml"
    installed_template = Path(sys.prefix) / "share" / "mcp-broker" / "config" / "broker.example.yaml"
    return template_path_from_candidates(source_tree_template, current_tree_template, installed_template)


def template_path_from_candidates(*candidates: Path) -> Path:
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[-1]


def handle_init(args: argparse.Namespace) -> int:
    config_path = args.config.expanduser()
    template_path = (args.template or default_template_path()).expanduser()
    return initialize_config(config_path, template_path=template_path, force=args.force)


def initialize_config(config_path: Path, *, template_path: Path, force: bool) -> int:
    if config_path.exists() and not force:
        sys.stdout.write(f"config already exists: {config_path}\n")
        return 0
    if not template_path.exists():
        sys.stderr.write(f"missing config template: {template_path}\n")
        return 1
    config_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template_path, config_path)
    sys.stdout.write(f"created config: {config_path}\n")
    return 0


def daemon_argv(
    *,
    command: str,
    runtime_root: Path,
    socket_path: Path,
    config_path: Path | None,
) -> list[str]:
    daemon_command = "serve" if command == "start" else command
    argv = [
        daemon_command,
        "--runtime-root",
        str(runtime_root.expanduser()),
        "--socket-path",
        str(socket_path.expanduser()),
    ]
    if daemon_command == "serve" and config_path is not None:
        argv.extend(["--config", str(config_path.expanduser())])
    return argv


def handle_daemon(args: argparse.Namespace) -> int:
    return daemon_main(
        daemon_argv(
            command=args.command,
            runtime_root=args.runtime_root,
            socket_path=args.socket_path,
            config_path=getattr(args, "config", None),
        )
    )


def stdio_argv(
    *,
    runtime_root: Path,
    socket_path: Path,
    config_path: Path,
    profile: str | None,
    init_if_missing: bool,
    ready_attempts: int | None = None,
) -> list[str]:
    argv = [
        "--runtime-root",
        str(runtime_root.expanduser()),
        "--socket-path",
        str(socket_path.expanduser()),
        "--config",
        str(config_path.expanduser()),
    ]
    if profile is not None:
        argv.extend(["--profile", profile])
    if init_if_missing:
        argv.append("--init-if-missing")
    if ready_attempts is not None:
        argv.extend(["--ready-attempts", str(ready_attempts)])
    return argv


def handle_stdio(args: argparse.Namespace) -> int:
    return stdio_main(
        stdio_argv(
            runtime_root=args.runtime_root,
            socket_path=args.socket_path,
            config_path=args.config,
            profile=args.profile,
            init_if_missing=args.init_if_missing,
            ready_attempts=args.ready_attempts,
        )
    )


def stdio_main(argv: Sequence[str] | None = None) -> int:
    args = _stdio_parser().parse_args(argv)

    runtime_root = Path(os.path.expandvars(args.runtime_root)).expanduser()
    socket_path = Path(os.path.expandvars(args.socket_path)).expanduser()
    config_path = Path(os.path.expandvars(args.config)).expanduser()

    if args.init_if_missing and not config_path.exists():
        initialized = initialize_config(
            config_path,
            template_path=default_template_path().expanduser(),
            force=False,
        )
        if initialized != 0:
            return initialized

    if not config_path.exists():
        sys.stderr.write(f"missing config: {config_path}\n")
        return 1

    daemon = BrokerDaemon(
        runtime_root=runtime_root,
        socket_path=socket_path,
        broker_config=BrokerConfig.from_file(config_path),
    )
    daemon_to_stop, daemon_error = _start_stdio_daemon(daemon)
    if daemon_error is not None:
        sys.stderr.write(f"{daemon_error}\n")
        return 1
    try:
        if not _wait_for_socket(socket_path, attempts=args.ready_attempts):
            sys.stderr.write(f"broker socket did not become ready: {socket_path}\n")
            return 1
        ClientShim(socket_path=socket_path, profile=args.profile).run_stdio(
            sys.stdin.buffer,
            sys.stdout.buffer,
        )
    except ClientShimError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    finally:
        if daemon_to_stop is not None:
            daemon_to_stop.stop()
    return 0


def _stdio_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run mcp-broker as a one-process stdio server")
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--socket-path", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--profile")
    parser.add_argument("--init-if-missing", action="store_true")
    parser.add_argument("--ready-attempts", type=int, default=50)
    return parser


def _start_stdio_daemon(daemon: BrokerDaemon) -> tuple[BrokerDaemon | None, str | None]:
    try:
        daemon.start()
    except BrokerDaemonError as exc:
        message = str(exc)
        if "broker daemon already running" in message:
            return None, None
        return None, message
    return daemon, None


def _wait_for_socket(
    socket_path: Path,
    *,
    attempts: int,
    wait: Callable[[float], object] | None = None,
) -> bool:
    waiter = wait or Event().wait
    for attempt in range(attempts):
        if socket_path.exists():
            return True
        if attempt < attempts - 1:
            waiter(0.1)
    return False


def handle_render(args: argparse.Namespace) -> int:
    argv = [
        "render",
        "--config",
        str(args.config.expanduser()),
        "--client",
        args.client,
    ]
    if args.apply:
        argv.append("--apply")
    if args.target_path is not None:
        argv.extend(["--target-path", str(args.target_path.expanduser())])
    return config_render_main(argv)


def handle_config_compose(args: argparse.Namespace) -> int:
    argv: list[str] = []
    if args.org is not None:
        argv.extend(["--org", str(args.org.expanduser())])
    if args.team is not None:
        argv.extend(["--team", str(args.team.expanduser())])
    for add_on in args.addon:
        argv.extend(["--addon", str(add_on.expanduser())])
    if args.user is not None:
        argv.extend(["--user", str(args.user.expanduser())])
    return config_layers_main(argv)


def handle_bundle_validate(args: argparse.Namespace) -> int:
    return bundle_loader_main(["--bundle", str(args.bundle.expanduser())])


def handle_deployment(args: argparse.Namespace) -> int:
    argv = [args.deployment_command, "--state-dir", str(args.state_dir.expanduser())]
    if args.deployment_command == "stage":
        argv.extend(["--bundle", str(args.bundle.expanduser())])
        if args.dry_run:
            argv.append("--dry-run")
    return deployments_main(argv)


def handle_break_glass(args: argparse.Namespace) -> int:
    argv = [args.break_glass_command, "--state-dir", str(args.state_dir.expanduser())]
    if args.break_glass_command == "create":
        argv.extend(
            [
                "--reason",
                args.reason,
                "--operator",
                args.operator,
                "--expires-at",
                args.expires_at,
            ]
        )
        for policy_path in args.bypass_policy:
            argv.extend(["--bypass-policy", policy_path])
    return break_glass_main(argv)


def handle_rollout_simulator(args: argparse.Namespace) -> int:
    argv = [
        "--bundle",
        str(args.bundle.expanduser()),
        "--fleet-status",
        str(args.fleet_status.expanduser()),
    ]
    if args.approved:
        argv.append("--approved")
    return rollout_simulator_main(argv)


def handle_runtime_launch_plan(args: argparse.Namespace) -> int:
    runtime_args = list(args.runtime_args)
    if runtime_args[:1] == ["--"]:
        runtime_args = runtime_args[1:]
    try:
        launch_plan = ActiveRuntimeLauncher(args.state_dir).launch_plan(runtime_args)
    except RuntimeLauncherError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    sys.stdout.write(json.dumps(launch_plan, sort_keys=True) + "\n")
    return 0


def handle_runtime_artifact_verify(args: argparse.Namespace) -> int:
    try:
        verifier = RuntimeArtifactVerifier()
        if args.metadata is not None:
            if args.artifact is not None or args.digest is not None:
                raise RuntimeArtifactError(
                    "runtime artifact verification accepts either --metadata or --artifact with --digest"
                )
            report = verifier.verify_metadata_file(args.metadata)
        else:
            if args.artifact is None or args.digest is None:
                raise RuntimeArtifactError(
                    "runtime artifact verification requires --artifact and --digest"
                )
            report = verifier.verify(
                artifact_path=args.artifact,
                expected_digest=args.digest,
            )
    except RuntimeArtifactError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    sys.stdout.write(json.dumps(report, sort_keys=True) + "\n")
    return 0


def handle_runtime_bootstrap(args: argparse.Namespace) -> int:
    argv = [args.bootstrap_command, "--state-dir", str(args.state_dir.expanduser())]
    if getattr(args, "metadata", None) is not None:
        argv.extend(["--metadata", str(args.metadata.expanduser())])
    if getattr(args, "approved", False):
        argv.append("--approved")
    return bootstrap_transactions_main(argv)


def handle_service_plan(args: argparse.Namespace) -> int:
    return service_templates_main(
        [
            "--platform",
            args.platform,
            "--runtime-root",
            str(args.runtime_root.expanduser()),
            "--socket-path",
            str(args.socket_path.expanduser()),
            "--config",
            str(args.config.expanduser()),
            "--daemon-command",
            args.daemon_command,
            "--home-dir",
            str(args.home_dir.expanduser()),
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
