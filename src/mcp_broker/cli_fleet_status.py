"""Fleet-status CLI wiring for the top-level package command."""

from __future__ import annotations

import argparse
from pathlib import Path

from mcp_broker.fleet_status import main as fleet_status_main


def add_fleet_status_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    fleet_parser = subparsers.add_parser(
        "fleet-status",
        help="Export central-safe broker fleet status",
    )
    fleet_subparsers = fleet_parser.add_subparsers(
        dest="fleet_status_command",
        required=True,
    )
    _add_export_parser(fleet_subparsers)
    _add_collect_parser(fleet_subparsers)


def handle_fleet_status_export(args: argparse.Namespace) -> int:
    return fleet_status_main(["--status-file", str(args.status_file.expanduser())])


def handle_fleet_status_collect(args: argparse.Namespace) -> int:
    argv = [
        "--status-file",
        str(args.status_file.expanduser()),
        "--target-url",
        args.target_url,
        "--auth-ref",
        args.auth_ref,
        "--retention-days",
        str(args.retention_days),
        "--collector-id",
        args.collector_id,
    ]
    if args.generated_at:
        argv.extend(["--generated-at", args.generated_at])
    return fleet_status_main(argv)


def _add_export_parser(
    fleet_subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    export_parser = fleet_subparsers.add_parser(
        "export",
        help="Export a redacted fleet status payload from broker-status.json",
    )
    export_parser.add_argument("--status-file", required=True, type=Path)
    export_parser.set_defaults(handler=handle_fleet_status_export)


def _add_collect_parser(
    fleet_subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    collect_parser = fleet_subparsers.add_parser(
        "collect",
        help="Prepare a redacted fleet status collection envelope without uploading",
    )
    collect_parser.add_argument("--status-file", required=True, type=Path)
    collect_parser.add_argument("--target-url", required=True)
    collect_parser.add_argument("--auth-ref", required=True)
    collect_parser.add_argument("--retention-days", required=True, type=int)
    collect_parser.add_argument("--collector-id", required=True)
    collect_parser.add_argument("--generated-at")
    collect_parser.set_defaults(handler=handle_fleet_status_collect)
