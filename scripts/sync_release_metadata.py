from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import re
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
LOGGER = logging.getLogger(__name__)
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
DOCKER_IMAGE_RE = re.compile(r"(?m)^image:\s*(?P<image>\S+)\s*$")


def _read_current_version() -> str:
    return _validate_version(str(_load_json(ROOT / "npm" / "package.json")["version"]))


def _bump_version(version: str, bump: str) -> str:
    match = SEMVER_RE.fullmatch(version)
    if match is None:
        raise ValueError(f"invalid semantic version: {version}")
    major, minor, patch = (int(part) for part in match.groups())
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    if bump == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"invalid release bump: {bump}")


def _validate_version(version: str) -> str:
    if SEMVER_RE.fullmatch(version) is None:
        raise ValueError(f"invalid semantic version: {version}")
    return version


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def docker_catalog_version_from_text(text: str) -> str:
    match = DOCKER_IMAGE_RE.search(text)
    if match is None:
        raise RuntimeError("docker MCP catalog must define an image tag")
    image = match.group("image")
    if ":" not in image:
        raise RuntimeError("docker MCP catalog image must include a tag")
    return image.rsplit(":", maxsplit=1)[1]


def replace_docker_catalog_version(text: str, version: str) -> str:
    match = DOCKER_IMAGE_RE.search(text)
    if match is None:
        raise RuntimeError("docker MCP catalog must define an image tag")
    image = match.group("image")
    if ":" not in image:
        raise RuntimeError("docker MCP catalog image must include a tag")
    image_name = image.rsplit(":", maxsplit=1)[0]
    return text[: match.start("image")] + f"{image_name}:{version}" + text[match.end("image") :]


def _json_metadata_updates(version: str) -> dict[str, dict[str, Any]]:
    updates: dict[str, dict[str, Any]] = {}

    npm_package = _load_json(ROOT / "npm" / "package.json")
    npm_package["version"] = version
    updates["npm/package.json"] = npm_package

    plugin_manifest = _load_json(ROOT / ".codex-plugin" / "plugin.json")
    plugin_manifest["version"] = version
    updates[".codex-plugin/plugin.json"] = plugin_manifest

    for relative in ["registry/server.json", "registry/server.template.json"]:
        metadata = _load_json(ROOT / relative)
        metadata["version"] = version
        metadata["packages"][0]["version"] = version
        updates[relative] = metadata

    mcpb_manifest = _load_json(ROOT / "mcpb" / "manifest.json")
    mcpb_manifest["version"] = version
    updates["mcpb/manifest.json"] = mcpb_manifest

    server_card = _load_json(ROOT / ".well-known" / "mcp" / "server-card.json")
    server_card["packages"][0]["version"] = version
    updates[".well-known/mcp/server-card.json"] = server_card

    return updates


def _sync_json_metadata(version: str, write: bool) -> list[str]:
    changed: list[str] = []
    for relative, updated in _json_metadata_updates(version).items():
        path = ROOT / relative
        original = _load_json(path)
        if original != updated:
            changed.append(relative)
            if write:
                _write_json(path, updated)
    return changed


def _sync_docker_catalog(version: str, write: bool) -> bool:
    path = ROOT / "docker" / "mcp-catalog" / "mcp-broker.yaml"
    text = path.read_text(encoding="utf-8")
    updated = replace_docker_catalog_version(text, version)
    changed = updated != text
    if changed and write:
        path.write_text(updated, encoding="utf-8")
    return changed


def _sync_changelog(version: str, write: bool) -> bool:
    path = ROOT / "CHANGELOG.md"
    text = path.read_text(encoding="utf-8")
    first_heading = re.search(r"^## ([0-9]+\.[0-9]+\.[0-9]+) - ", text, re.M)
    if first_heading and first_heading.group(1) == version:
        return False
    if re.search(rf"^## {re.escape(version)} - ", text, re.M):
        return False
    from datetime import date

    heading = (
        f"## {version} - {date.today().isoformat()}\n\n"
        "- Synchronize release metadata through the Makefile release path.\n\n"
    )
    insert_at = first_heading.start() if first_heading else len(text)
    updated = text[:insert_at] + heading + text[insert_at:]
    if write:
        path.write_text(updated, encoding="utf-8")
    return True


def sync_release_metadata(version: str, *, write: bool) -> list[str]:
    changed: list[str] = []
    changed.extend(_sync_json_metadata(version, write))
    if _sync_docker_catalog(version, write):
        changed.append("docker/mcp-catalog/mcp-broker.yaml")
    if _sync_changelog(version, write):
        changed.append("CHANGELOG.md")
    return changed


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Synchronize mcp-broker release metadata")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--version", help="explicit semantic version to apply")
    source.add_argument(
        "--bump",
        choices=["major", "minor", "patch"],
        help="derive the next semantic version from src/mcp_broker/__init__.py",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="write synchronized metadata")
    mode.add_argument("--check", action="store_true", help="fail if metadata would change")
    parser.add_argument("--emit-version", action="store_true", help="write the target version to stdout")
    args = parser.parse_args()
    if not (args.write or args.check or args.emit_version):
        parser.error("set --write, --check, or --emit-version")
    return args


def main() -> int:
    args = _parse_args()
    current_version = _read_current_version()
    version = _validate_version(args.version) if args.version else current_version
    if args.bump:
        version = _bump_version(current_version, args.bump)

    if args.emit_version and not (args.write or args.check):
        sys.stdout.write(version)
        sys.stdout.write("\n")
        return 0

    changed = sync_release_metadata(version, write=args.write)
    if args.emit_version:
        sys.stdout.write(version)
        sys.stdout.write("\n")

    if changed and args.check:
        for relative in changed:
            LOGGER.error("release metadata out of sync: %s", relative)
        return 1
    if changed:
        LOGGER.info("synchronized release metadata for %s: %s", version, ", ".join(changed))
    else:
        LOGGER.info("release metadata already synchronized for %s", version)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(main())
