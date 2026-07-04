"""Governance CLI wiring for the top-level package command."""

from __future__ import annotations

import argparse
from pathlib import Path

from mcp_broker.governance_approval import main as governance_approval_main
from mcp_broker.governance_pull import main as governance_pull_main
from mcp_broker.governance_reference_control_plane import (
    main as reference_control_plane_main,
)
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
    _add_approve_parser(governance_subparsers)
    _add_reference_control_plane_parser(governance_subparsers)


def handle_governance(args: argparse.Namespace) -> int:
    if args.governance_command == "rollout-control":
        return _handle_rollout_control(args)
    if args.governance_command == "approve":
        return _handle_approve(args)
    if args.governance_command == "reference-control-plane":
        return _handle_reference_control_plane(args)
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


def _handle_approve(args: argparse.Namespace) -> int:
    argv = [
        "--state-dir",
        str(args.state_dir.expanduser()),
        "--request-type",
        args.request_type,
        "--operator",
        args.operator,
        "--reason",
        args.reason,
        "--expires-at",
        args.expires_at,
    ]
    for action_id in args.action_id:
        argv.extend(["--action-id", action_id])
    for policy_path in args.policy_path:
        argv.extend(["--policy-path", policy_path])
    if args.break_glass_record_id:
        argv.extend(["--break-glass-record-id", args.break_glass_record_id])
    if args.created_at:
        argv.extend(["--created-at", args.created_at])
    return governance_approval_main(argv)


def _handle_reference_control_plane(args: argparse.Namespace) -> int:
    argv = [
        "--mode",
        args.mode,
        "--state-dir",
        str(args.state_dir.expanduser()),
        "--bundle",
        str(args.bundle.expanduser()),
        "--assignment-source",
        str(args.assignment_source.expanduser()),
        "--broker-context",
        str(args.broker_context.expanduser()),
        "--fleet-status",
        str(args.fleet_status.expanduser()),
        "--target-url",
        args.target_url,
        "--auth-ref",
        args.auth_ref,
        "--operator",
        args.operator,
        "--signature-ref",
        args.signature_ref,
        "--provenance",
        str(args.provenance.expanduser()),
        "--approval-expires-at",
        args.approval_expires_at,
    ]
    if args.created_at:
        argv.extend(["--created-at", args.created_at])
    return reference_control_plane_main(argv)


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


def _add_approve_parser(
    governance_subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    approve_parser = governance_subparsers.add_parser(
        "approve",
        help="Record an expiring local approval for a governance mutation",
    )
    approve_parser.add_argument("--state-dir", required=True, type=Path)
    approve_parser.add_argument("--request-type", required=True)
    approve_parser.add_argument("--operator", required=True)
    approve_parser.add_argument("--reason", required=True)
    approve_parser.add_argument("--expires-at", required=True)
    approve_parser.add_argument("--action-id", action="append", default=[])
    approve_parser.add_argument("--policy-path", action="append", default=[])
    approve_parser.add_argument("--break-glass-record-id")
    approve_parser.add_argument("--created-at")
    approve_parser.set_defaults(handler=handle_governance)


def _add_reference_control_plane_parser(
    governance_subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    reference_parser = governance_subparsers.add_parser(
        "reference-control-plane",
        help="Run the local reference control-plane flow",
    )
    reference_parser.add_argument("--mode", default="local_reference_only")
    reference_parser.add_argument("--state-dir", required=True, type=Path)
    reference_parser.add_argument("--bundle", required=True, type=Path)
    reference_parser.add_argument("--assignment-source", required=True, type=Path)
    reference_parser.add_argument("--broker-context", required=True, type=Path)
    reference_parser.add_argument("--fleet-status", required=True, type=Path)
    reference_parser.add_argument("--target-url", required=True)
    reference_parser.add_argument("--auth-ref", required=True)
    reference_parser.add_argument("--operator", required=True)
    reference_parser.add_argument("--signature-ref", required=True)
    reference_parser.add_argument("--provenance", required=True, type=Path)
    reference_parser.add_argument("--approval-expires-at", required=True)
    reference_parser.add_argument("--created-at")
    reference_parser.set_defaults(handler=handle_governance)
