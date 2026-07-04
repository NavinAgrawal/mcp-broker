#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Callable, Sequence
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import urlopen


class ReleaseIdempotencyError(RuntimeError):
    """Raised when a release surface cannot be treated as idempotent."""


@dataclass(frozen=True)
class ReleaseAction:
    surface: str
    version: str
    status: str
    verified: dict[str, str]


JsonFetcher = Callable[[str], object]
NpmPacker = Callable[[Path], tuple[str, str]]

LOGGER = logging.getLogger("release_idempotency")


def decide_release_action(
    *,
    surface: str,
    version: str,
    expected: dict[str, str],
    remote: dict[str, str],
) -> ReleaseAction:
    if not expected:
        raise ReleaseIdempotencyError(f"{surface} {version} has no expected artifacts to verify")
    if not remote:
        return ReleaseAction(surface=surface, version=version, status="publish", verified={})

    verified: dict[str, str] = {}
    for name, expected_digest in expected.items():
        remote_digest = remote.get(name)
        if remote_digest is None:
            return ReleaseAction(surface=surface, version=version, status="publish", verified=verified)
        if remote_digest != expected_digest:
            raise ReleaseIdempotencyError(
                f"{surface} {version} digest mismatch for {name}: "
                f"expected {expected_digest}, remote {remote_digest}"
            )
        verified[name] = expected_digest
    return ReleaseAction(surface=surface, version=version, status="skip", verified=verified)


def inspect_pypi_release(
    *,
    version: str,
    dist_dir: Path,
    fetch_json: JsonFetcher,
    pypi_version_url: str,
) -> ReleaseAction:
    expected = {
        artifact.name: _sha256(artifact)
        for artifact in sorted(dist_dir.iterdir())
        if artifact.is_file()
    }
    payload = fetch_json(pypi_version_url)
    if payload is None:
        return decide_release_action(
            surface="pypi",
            version=version,
            expected=expected,
            remote={},
        )
    if not isinstance(payload, dict):
        raise ReleaseIdempotencyError("PyPI release payload must be a JSON object")
    info = payload.get("info")
    if isinstance(info, dict) and info.get("version") not in {None, version}:
        raise ReleaseIdempotencyError(
            f"PyPI version metadata mismatch: expected {version}, remote {info.get('version')}"
        )

    remote: dict[str, str] = {}
    urls = payload.get("urls")
    if not isinstance(urls, list):
        raise ReleaseIdempotencyError("PyPI release payload urls must be a list")
    for release in urls:
        if not isinstance(release, dict):
            raise ReleaseIdempotencyError("PyPI release entry must be an object")
        filename = release.get("filename")
        digests = release.get("digests")
        if not isinstance(filename, str) or not isinstance(digests, dict):
            raise ReleaseIdempotencyError("PyPI release entry is missing filename/digests")
        sha256 = digests.get("sha256")
        if not isinstance(sha256, str):
            raise ReleaseIdempotencyError(f"PyPI release entry is missing sha256 for {filename}")
        remote[filename] = sha256
    return decide_release_action(
        surface="pypi",
        version=version,
        expected=expected,
        remote=remote,
    )


def inspect_npm_release(
    *,
    version: str,
    package_dir: Path,
    package_name: str,
    npm_registry_url: str,
    npm_pack: NpmPacker,
    fetch_json: JsonFetcher,
) -> ReleaseAction:
    filename, integrity = npm_pack(package_dir)
    if not filename or not integrity:
        raise ReleaseIdempotencyError("npm pack did not report filename and integrity")
    payload = fetch_json(_npm_metadata_url(npm_registry_url, package_name))
    if payload is None:
        return decide_release_action(
            surface="npm",
            version=version,
            expected={filename: integrity},
            remote={},
        )
    if not isinstance(payload, dict):
        raise ReleaseIdempotencyError("NPM metadata payload must be a JSON object")
    versions = payload.get("versions")
    if not isinstance(versions, dict):
        raise ReleaseIdempotencyError("NPM metadata versions must be an object")
    release = versions.get(version)
    if release is None:
        return decide_release_action(
            surface="npm",
            version=version,
            expected={filename: integrity},
            remote={},
        )
    if not isinstance(release, dict):
        raise ReleaseIdempotencyError("NPM release metadata must be an object")
    dist = release.get("dist")
    if not isinstance(dist, dict):
        raise ReleaseIdempotencyError("NPM release metadata is missing dist")
    remote_integrity = dist.get("integrity") or dist.get("shasum")
    if not isinstance(remote_integrity, str):
        raise ReleaseIdempotencyError("NPM release metadata is missing integrity")
    return decide_release_action(
        surface="npm",
        version=version,
        expected={filename: integrity},
        remote={filename: remote_integrity},
    )


def inspect_mcp_registry_release(
    *,
    version: str,
    registry_name: str,
    fetch_json: JsonFetcher,
    mcp_registry_search_url: str,
) -> ReleaseAction:
    payload = fetch_json(mcp_registry_search_url)
    if payload is None:
        return ReleaseAction(
            surface="mcp-registry",
            version=version,
            status="publish",
            verified={},
        )
    if not isinstance(payload, dict):
        raise ReleaseIdempotencyError("MCP Registry payload must be a JSON object")
    servers = payload.get("servers")
    if not isinstance(servers, list):
        raise ReleaseIdempotencyError("MCP Registry payload servers must be a list")
    for item in servers:
        if not isinstance(item, dict):
            raise ReleaseIdempotencyError("MCP Registry server entry must be an object")
        server = item.get("server")
        if not isinstance(server, dict):
            raise ReleaseIdempotencyError("MCP Registry server entry is missing server metadata")
        if server.get("name") == registry_name and server.get("version") == version:
            return ReleaseAction(
                surface="mcp-registry",
                version=version,
                status="skip",
                verified={registry_name: version},
            )
    return ReleaseAction(
        surface="mcp-registry",
        version=version,
        status="publish",
        verified={},
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fetch_json(url: str) -> object:
    try:
        with urlopen(url, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def _npm_metadata_url(npm_registry_url: str, package_name: str) -> str:
    return f"{npm_registry_url.rstrip('/')}/{quote(package_name, safe='')}"


def _npm_pack_with_command(npm_command: str) -> NpmPacker:
    def pack(package_dir: Path) -> tuple[str, str]:
        result = subprocess.run(
            [*shlex.split(npm_command), "pack", "--dry-run", "--json"],
            cwd=package_dir,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ReleaseIdempotencyError(
                f"npm pack failed with exit code {result.returncode}: {result.stderr.strip()}"
            )
        payload = json.loads(result.stdout)
        if not isinstance(payload, list) or not payload:
            raise ReleaseIdempotencyError("npm pack payload must be a non-empty list")
        package = payload[0]
        if not isinstance(package, dict):
            raise ReleaseIdempotencyError("npm pack entry must be an object")
        filename = package.get("filename")
        integrity = package.get("integrity") or package.get("shasum")
        if not isinstance(filename, str) or not isinstance(integrity, str):
            raise ReleaseIdempotencyError("npm pack entry is missing filename/integrity")
        return filename, integrity

    return pack


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify release-surface idempotency before skipping a publish.")
    parser.add_argument("--surface", required=True, choices=["pypi", "npm", "mcp-registry"])
    parser.add_argument("--version", required=True)
    parser.add_argument("--dist-dir", type=Path)
    parser.add_argument("--pypi-version-url")
    parser.add_argument("--package-dir", type=Path)
    parser.add_argument("--package-name")
    parser.add_argument("--npm-registry-url")
    parser.add_argument("--npm-command", default="npm")
    parser.add_argument("--mcp-registry-name")
    parser.add_argument("--mcp-registry-search-url")
    return parser.parse_args(argv)


def _require(value: object, name: str) -> object:
    if value is None or value == "":
        raise ReleaseIdempotencyError(f"{name} is required")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args(argv)
    try:
        if args.surface == "pypi":
            action = inspect_pypi_release(
                version=args.version,
                dist_dir=Path(_require(args.dist_dir, "--dist-dir")),
                fetch_json=_fetch_json,
                pypi_version_url=str(_require(args.pypi_version_url, "--pypi-version-url")),
            )
        elif args.surface == "npm":
            action = inspect_npm_release(
                version=args.version,
                package_dir=Path(_require(args.package_dir, "--package-dir")),
                package_name=str(_require(args.package_name, "--package-name")),
                npm_registry_url=str(_require(args.npm_registry_url, "--npm-registry-url")),
                npm_pack=_npm_pack_with_command(args.npm_command),
                fetch_json=_fetch_json,
            )
        else:
            action = inspect_mcp_registry_release(
                version=args.version,
                registry_name=str(_require(args.mcp_registry_name, "--mcp-registry-name")),
                fetch_json=_fetch_json,
                mcp_registry_search_url=str(
                    _require(args.mcp_registry_search_url, "--mcp-registry-search-url")
                ),
            )
        sys.stdout.write(action.status + "\n")
        return 0
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, ReleaseIdempotencyError) as exc:
        LOGGER.error("release idempotency check failed: %s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
