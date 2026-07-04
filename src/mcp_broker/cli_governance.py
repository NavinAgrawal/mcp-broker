"""Governance CLI wiring for the top-level package command."""

from __future__ import annotations

import argparse
from pathlib import Path

from mcp_broker.governance_pull import main as governance_pull_main
from mcp_broker.governance_rollout_controller import main as rollout_controller_main


def add_governance_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    governance_parser = subparsers.add_parser(
        "governance",
        help="Pull, apply, and roll back governance bundles",
    )
    governance_subparsers = governance_parser.add_subparsers(
        dest="governance_command",
        required=True,
    )
    _add_pull_parser(governance_subparsers)
    _add_apply_parser(governance_subparsers)
    _add_rollback_parser(governance_subparsers)
    _add_rollout_control_parser(governance_subparsers)


def handle_governance(args: argparse.Namespace) -> int:
    if args.governance_command == "rollout-control":
        return _handle_rollout_control(args)
    argv = [args.governance_command, "--state-dir", str(args.state_dir.expanduser())]
    if args.governance_command == "pull":
        argv.extend(
            [
                "--source",
                args.source,
                "--assignment-decision",
                str(args.assignment_decision.expanduser()),
                "--auth-ref",
                args.auth_ref,
            ]
        )
        if args.auth_present:
            argv.append("--auth-present")
    if args.governance_command == "apply":
        argv.extend(
            [
                "--pull-record",
                str(args.pull_record.expanduser()),
                "--approval",
                str(args.approval.expanduser()),
            ]
        )
    return governance_pull_main(argv)


def _handle_rollout_control(args: argparse.Namespace) -> int:
    argv = [
        "--simulation",
        str(args.simulation.expanduser()),
        "--state-dir",
        str(args.state_dir.expanduser()),
        "--operator",
        args.operator,
        "--bundle-id",
        args.bundle_id,
        "--bundle-version",
        args.bundle_version,
        "--bundle-channel",
        args.bundle_channel,
        "--bundle-digest",
        args.bundle_digest,
    ]
    if args.created_at:
        argv.extend(["--created-at", args.created_at])
    return rollout_controller_main(argv)


def _add_pull_parser(
    governance_subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    pull_parser = governance_subparsers.add_parser(
        "pull",
        help="Fetch an assigned governance bundle into cache",
    )
    pull_parser.add_argument("--source", required=True)
    pull_parser.add_argument("--assignment-decision", required=True, type=Path)
    pull_parser.add_argument("--state-dir", required=True, type=Path)
    pull_parser.add_argument("--auth-ref", required=True)
    pull_parser.add_argument("--auth-present", action="store_true")
    pull_parser.set_defaults(handler=handle_governance)


def _add_apply_parser(
    governance_subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    apply_parser = governance_subparsers.add_parser(
        "apply",
        help="Apply a cached governance bundle after approval",
    )
    apply_parser.add_argument("--pull-record", required=True, type=Path)
    apply_parser.add_argument("--state-dir", required=True, type=Path)
    apply_parser.add_argument("--approval", required=True, type=Path)
    apply_parser.set_defaults(handler=handle_governance)


def _add_rollback_parser(
    governance_subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    rollback_parser = governance_subparsers.add_parser(
        "rollback",
        help="Roll back the active governance deployment",
    )
    rollback_parser.add_argument("--state-dir", required=True, type=Path)
    rollback_parser.set_defaults(handler=handle_governance)


def _add_rollout_control_parser(
    governance_subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    rollout_parser = governance_subparsers.add_parser(
        "rollout-control",
        help="Record local rollout-control audit actions from a simulation result",
    )
    rollout_parser.add_argument("--simulation", required=True, type=Path)
    rollout_parser.add_argument("--state-dir", required=True, type=Path)
    rollout_parser.add_argument("--operator", required=True)
    rollout_parser.add_argument("--bundle-id", required=True)
    rollout_parser.add_argument("--bundle-version", required=True)
    rollout_parser.add_argument("--bundle-channel", required=True)
    rollout_parser.add_argument("--bundle-digest", required=True)
    rollout_parser.add_argument("--created-at")
    rollout_parser.set_defaults(handler=handle_governance)
