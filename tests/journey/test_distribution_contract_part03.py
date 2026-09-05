from __future__ import annotations
import json
from pathlib import Path
import re
import pytest
from tests.support.makefiles import (
    expand_make_value,
    read_combined_makefiles,
    read_make_variable_defaults,
)
pytestmark = pytest.mark.journey
ROOT = Path(__file__).resolve().parents[2]
SEMVER_PATTERN = re.compile(r"\b(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\b")
HISTORICAL_RELEASE_FILES = {
    "CHANGELOG.md",
    "docs/p16-maintainer-inputs.md",
}
STATIC_RELEASE_METADATA_FILES = {
    ".well-known/mcp/server-card.json",
    "docker/mcp-catalog/mcp-broker.yaml",
    "mcpb/manifest.json",
    "npm/package.json",
    "registry/server.json",
    "registry/server.template.json",
}
def _package_version() -> str:
    package = json.loads((ROOT / "npm" / "package.json").read_text(encoding="utf-8"))
    return str(package["version"])
def _public_coordinate_values(make_vars: dict[str, str]) -> set[str]:
    direct_variables = [
        "GITHUB_OWNER",
        "PUBLIC_NAMESPACE",
        "GITHUB_REPO",
        "PRIVATE_GITHUB_REPO",
        "NPM_PACKAGE_NAME",
        "MCP_REGISTRY_NAME",
        "HOMEBREW_TAP_REPO",
        "HOMEBREW_TAP_REF",
        "SMITHERY_QUALIFIED_NAME",
        "SMITHERY_NAMESPACE",
        "SMITHERY_RELEASE_ID",
        "GLAMA_MAINTAINER",
        "DOCKER_REGISTRY_HOST",
        "DOCKER_REGISTRY_SERVICE",
        "GHCR_REGISTRY_HOST",
        "GHCR_REGISTRY_SERVICE",
    ]
    expanded_variables = [
        "GITHUB_REPOSITORY_URL",
        "GITHUB_REPOSITORY_HOST_PATH",
        "PRIVATE_GITHUB_REPOSITORY_URL",
        "GITHUB_TAG_SOURCE_TARBALL_URL",
        "DOCKER_REPOSITORY_IMAGE",
        "GHCR_REPOSITORY_IMAGE",
        "DOCKER_HUB_API_REPOSITORY_BASE_URL",
        "DOCKER_REGISTRY_AUTH_URL",
        "DOCKER_REGISTRY_MANIFEST_BASE_URL",
        "GHCR_REGISTRY_AUTH_URL",
        "GHCR_REGISTRY_MANIFEST_BASE_URL",
        "PYPI_PROJECT_URL",
        "PYPI_SIMPLE_CHECK_URL",
        "NPM_REGISTRY_URL",
        "NPM_PACKAGE_URL",
        "DOCKER_HUB_TAGS_URL",
        "HOMEBREW_TAP_URL",
        "HOMEBREW_TAP_CLONE_URL",
        "SMITHERY_LISTING_URL",
        "SMITHERY_API_BASE_URL",
        "SMITHERY_MCP_URL",
        "GLAMA_LISTING_URL",
        "GLAMA_SCHEMA_URL",
        "PULSEMCP_LISTING_URL",
        "PULSEMCP_SUBMIT_URL",
        "MCPSERVERS_LISTING_URL",
        "MCPSERVERS_SUBMIT_URL",
        "MCP_SO_LISTING_URL",
        "MCPCENTRAL_REGISTRY_URL",
        "MCPCENTRAL_SUBMIT_URL",
        "MCP_PUBLISHER_RELEASE_DOWNLOAD_BASE_URL",
        "DOCKER_MCP_CATALOG_PR_URL",
        "PUNKPEYE_AWESOME_PR_URL",
        "APPCYPHER_AWESOME_FORK_BRANCH_URL",
        "APPCYPHER_AWESOME_COMPARE_URL",
    ]
    return {
        *[expand_make_value(make_vars, make_vars[variable]) for variable in direct_variables],
        (
            f"{expand_make_value(make_vars, make_vars['DOCKER_NAMESPACE'])}/"
            f"{expand_make_value(make_vars, make_vars['DOCKER_IMAGE_NAME'])}"
        ),
        *[expand_make_value(make_vars, make_vars[variable]) for variable in expanded_variables],
    }
def _centralized_coordinate_scan_paths() -> list[Path]:
    scanned_suffixes = {".py", ".sh", ".js", ".yml", ".yaml", ".mk", ".toml", ".json", ".md"}
    scanned_roots = [
        ROOT / "src",
        ROOT / "scripts",
        ROOT / "tests",
        ROOT / ".github" / "workflows",
        ROOT / "mk",
        ROOT / "docs",
    ]
    scanned_files = [ROOT / "Dockerfile", ROOT / "README.md", ROOT / "CHANGELOG.md", ROOT / "TODO.md"]
    paths = list(scanned_files)
    for root in scanned_roots:
        if not root.exists():
            continue
        paths.extend(path for path in sorted(root.rglob("*")) if path.is_file() and path.suffix in scanned_suffixes)
    return paths
def _find_public_coordinate_offenders(make_vars: dict[str, str]) -> list[str]:
    allowed_paths = {
        "mk/config.mk",
        "pyproject.toml",
        "npm/package.json",
        "npm/README.md",
        "registry/server.json",
        "registry/server.template.json",
        "mcpb/manifest.json",
        ".well-known/mcp/server-card.json",
        "docker/mcp-catalog/mcp-broker.yaml",
        # The changelog is a historical record: a release that re-homes the
        # publishing identity documents the old and new coordinates by name so
        # users can find the new install path. It is not a coordinate source.
        "CHANGELOG.md",
    }
    readme_mcp_marker = f"<!-- mcp-name: {expand_make_value(make_vars, make_vars['MCP_REGISTRY_NAME'])} -->"
    offenders: list[str] = []
    for path in _centralized_coordinate_scan_paths():
        if not path.exists():
            continue
        relative = path.relative_to(ROOT).as_posix()
        if relative in allowed_paths:
            continue
        text = path.read_text(encoding="utf-8").replace(readme_mcp_marker, "")
        offenders.extend(
            f"{relative}: {value}"
            for value in _public_coordinate_values(make_vars)
            if value and value in text
        )
    return offenders

def test_windows_scheduled_task_contract_uses_runtime_root_and_config_path() -> None:
    script = ROOT / "scripts" / "install-windows-task.ps1"
    uninstall_script = ROOT / "scripts" / "uninstall-windows-task.ps1"
    smoke_script = ROOT / "scripts" / "windows-powershell-smoke.sh"
    text = script.read_text(encoding="utf-8")
    smoke_text = smoke_script.read_text(encoding="utf-8")

    assert script.is_file()
    assert uninstall_script.is_file()
    assert smoke_script.is_file()
    assert "MCP_BROKER_RUNTIME_ROOT" in text
    assert "MCP_BROKER_SOCKET" in text
    assert "MCP_BROKER_CONFIG" in text
    assert "MCP_BROKER_DAEMON_COMMAND" in text
    assert "mcp_broker.daemon" in text
    assert "Register-ScheduledTask" in text
    assert "PIP_DISABLE_PIP_VERSION_CHECK=1" in smoke_text
    assert "/Users/" not in text
    assert "navin" not in text.lower()

def test_config_schema_has_public_distribution_metadata() -> None:
    schema = json.loads((ROOT / "config" / "broker.schema.json").read_text(encoding="utf-8"))

    assert schema["title"] == "mcp-broker config"
    assert schema["type"] == "object"
    assert "runtime" in schema["properties"]

def test_install_manifests_raise_the_file_descriptor_limit() -> None:
    # The daemon multiplexes many upstream subprocess pipes across concurrent LLM
    # clients; every install surface must lift the platform default FD ceiling or
    # the broker hits "Too many open files" and drops client transports.
    launchagent = (ROOT / "scripts" / "install-launchagent.sh").read_text(encoding="utf-8")
    assert "BROKER_MAX_OPEN_FILES" in launchagent
    assert "<key>SoftResourceLimits</key>" in launchagent
    assert "<key>NumberOfFiles</key>" in launchagent

    systemd = (ROOT / "scripts" / "install-systemd-user.sh").read_text(encoding="utf-8")
    assert "LimitNOFILE=$BROKER_MAX_OPEN_FILES" in systemd

    entrypoint = (ROOT / "docker" / "docker-entrypoint.sh").read_text(encoding="utf-8")
    assert "ulimit -n" in entrypoint
