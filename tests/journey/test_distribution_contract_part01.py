from __future__ import annotations
import json
from pathlib import Path
import re
import tomllib
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

def test_distribution_docs_and_package_metadata_are_public_ready() -> None:
    make_vars = read_make_variable_defaults(ROOT)
    repository_url = expand_make_value(make_vars, make_vars["GITHUB_REPOSITORY_URL"])
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    install_doc = (ROOT / "docs" / "install.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    required_pyproject_terms = [
        'name = "mcp-broker"',
        'requires-python = ">=3.10"',
        "Local MCP broker for sharing upstream MCP servers across MCP clients",
        "mcp-broker-client",
        "mcp-broker-daemon",
        "[project.urls]",
    ]
    required_install_terms = [
        "pipx install mcp-broker",
        "brew install",
        "make systemd-install",
        "make systemd-load",
        "make windows-install",
        "make windows-load",
        "PowerShell Scheduled Task",
    ]
    required_readme_terms = [
        "pipx install mcp-broker",
        "Homebrew",
        "systemd",
        "linux-container-smoke",
        "windows-powershell-smoke",
        "release-smoke",
    ]

    assert [term for term in required_pyproject_terms if term not in pyproject] == []
    assert [term for term in required_install_terms if term not in install_doc] == []
    assert [term for term in required_readme_terms if term not in readme] == []
    assert "/Users/" not in install_doc
    assert "$HOME/Projects" not in install_doc
    forbidden_minor_image = "python:" + ".".join(("3", "13"))
    assert forbidden_minor_image not in pyproject
    assert repository_url in pyproject
    assert "Codex and Claude sessions" not in pyproject
    assert "to Codex and Claude." not in readme
    assert "Renders Codex and Claude MCP config entries" not in readme

def test_release_version_is_single_sourced_and_public_metadata_matches() -> None:
    makefile = read_combined_makefiles(ROOT)
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_init = (ROOT / "src" / "mcp_broker" / "__init__.py").read_text(encoding="utf-8")
    daemon = (ROOT / "src" / "mcp_broker" / "daemon.py").read_text(encoding="utf-8")
    upstream_stdio = (ROOT / "src" / "mcp_broker" / "upstream_stdio.py").read_text(encoding="utf-8")
    upstream_http = (ROOT / "src" / "mcp_broker" / "upstream_http.py").read_text(encoding="utf-8")
    server = json.loads((ROOT / "registry" / "server.json").read_text(encoding="utf-8"))
    server_template = json.loads((ROOT / "registry" / "server.template.json").read_text(encoding="utf-8"))
    mcpb_manifest = json.loads((ROOT / "mcpb" / "manifest.json").read_text(encoding="utf-8"))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    package_version = _package_version()
    latest_changelog_match = re.search(r"^## ([0-9]+\.[0-9]+\.[0-9]+) - ", changelog, re.M)
    assert latest_changelog_match is not None

    assert pyproject["project"]["dynamic"] == ["version"]
    assert "version" not in pyproject["project"]
    assert pyproject["tool"]["setuptools"]["dynamic"]["version"]["attr"] == "mcp_broker.__version__"
    assert "__version__ = _resolve_version()" in package_init
    assert re.search(r'__version__\s*=\s*"[0-9]+\.[0-9]+\.[0-9]+"', package_init) is None
    assert "MCP_BROKER_VERSION" in package_init
    assert "PACKAGE_VERSION   ?= $(shell" in makefile
    assert "MCP_BROKER_VERSION ?= $(PACKAGE_VERSION)" in makefile
    assert "export MCP_BROKER_VERSION" in makefile
    assert "npm/package.json" in makefile
    assert package_version == latest_changelog_match.group(1)
    repository_match = re.fullmatch(
        r"https://github\.com/([^/]+)/([^/]+)", server["repository"]["url"]
    )
    assert repository_match is not None
    assert server["name"] == f"io.github.{repository_match.group(1)}/{repository_match.group(2)}"
    assert server["version"] == package_version
    assert server["packages"][0]["version"] == package_version
    assert server_template["version"] == package_version
    assert server_template["packages"][0]["version"] == package_version
    assert mcpb_manifest["version"] == package_version
    assert server["packages"][0]["identifier"] == pyproject["project"]["name"]
    assert pyproject["project"]["urls"]["Homepage"] == server["repository"]["url"]
    assert pyproject["project"]["urls"]["Documentation"] == f"{server['repository']['url']}#readme"
    assert pyproject["project"]["urls"]["Issues"] == f"{server['repository']['url']}/issues"
    assert f"mcp-name: {server['name']}" in readme
    assert 'server_version="0.0.1"' not in daemon
    assert '"version": "0.0.1"' not in upstream_stdio
    assert '"version": "0.0.1"' not in upstream_http

def test_mcpb_distribution_targets_package_and_smoke_bundle() -> None:
    makefile = read_combined_makefiles(ROOT)
    distribution = (ROOT / "docs" / "distribution.md").read_text(encoding="utf-8")

    assert re.search(
        r"^MCPB_OUTPUT\s+\?= \$\(PACKAGE_DIST_DIR\)/\$\(PACKAGE_SLUG\)-\$\(PACKAGE_VERSION\)\.mcpb$",
        makefile,
        re.M,
    )
    assert "mcpb-pack:" in makefile
    assert "mcpb-smoke:" in makefile
    assert "mcpb-stdio-smoke:" in makefile
    assert "smithery-payload-check:" in makefile
    assert "smithery-publish:" in makefile
    assert "scripts/smithery_release.py" in makefile
    assert 'PYTHONPATH="$(PYTHONPATH)" $(PYTHON) "$(ROOT)/scripts/smithery_release.py"' in makefile
    assert "SMITHERY_USER_AGENT ?=" in makefile
    assert 'SMITHERY_USER_AGENT="$(SMITHERY_USER_AGENT)"' in makefile
    assert "scripts/mcpb_stdio_smoke.py" in makefile
    assert '@$(NPX) -y @anthropic-ai/mcpb pack "$(ROOT)/mcpb" "$(MCPB_OUTPUT)"' in makefile
    assert '@$(NPX) -y @anthropic-ai/mcpb info "$(MCPB_SMOKE_OUTPUT)"' in makefile
    assert '@$(NPX) -y @anthropic-ai/mcpb unpack "$(MCPB_SMOKE_OUTPUT)" "$(MCPB_SMOKE_UNPACK_DIR)"' in makefile
    assert "make mcpb-pack" in distribution
    assert "make mcpb-smoke" in distribution
    assert "make mcpb-stdio-smoke" in distribution

def test_stable_release_public_status_is_aligned_to_source_release() -> None:
    make_vars = read_make_variable_defaults(ROOT)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    distribution = (ROOT / "docs" / "distribution.md").read_text(encoding="utf-8")
    github_publication = (ROOT / "docs" / "github-publication.md").read_text(encoding="utf-8")
    normalized_distribution = " ".join(distribution.split())

    assert "Stable release metadata is validated by `make release-version-check`" in readme
    assert "Package metadata is release-aligned by `make release-version-sync`." in distribution
    assert (
        "PyPI: `${PYPI_PROJECT_NAME} ${PACKAGE_VERSION}` is published by the release transaction."
        in distribution
    )
    assert (
        "MCP Registry: `${MCP_REGISTRY_NAME} ${PACKAGE_VERSION}` "
        "is published and marked latest by the release transaction."
    ) in normalized_distribution
    assert (
        "Homebrew: `${HOMEBREW_FORMULA_REF} ${PACKAGE_VERSION}` is published through the public tap."
        in distribution
    )
    assert "${PACKAGE_SLUG} ${PACKAGE_VERSION}" in github_publication

def test_current_release_versions_are_not_copied_across_docs_or_tests() -> None:
    current_version = _package_version()
    offenders: list[str] = []
    scanned_suffixes = {".md", ".py", ".sh", ".yml", ".yaml", ".json", ".toml", ".js"}
    scanned_roots = [
        ROOT / ".github",
        ROOT / ".well-known",
        ROOT / "docker",
        ROOT / "docs",
        ROOT / "mcpb",
        ROOT / "mk",
        ROOT / "npm",
        ROOT / "registry",
        ROOT / "scripts",
        ROOT / "src",
        ROOT / "tests",
    ]
    scanned_files = [
        ROOT / "Dockerfile",
        ROOT / "Makefile",
        ROOT / "README.md",
        ROOT / "TODO.md",
    ]

    for path in [
        *scanned_files,
        *(path for root in scanned_roots for path in sorted(root.rglob("*"))),
    ]:
        if not path.is_file() or path.suffix not in scanned_suffixes:
            continue
        relative = str(path.relative_to(ROOT))
        if (
            relative in HISTORICAL_RELEASE_FILES
            or relative in STATIC_RELEASE_METADATA_FILES
        ):
            continue
        text = path.read_text(encoding="utf-8")
        if current_version in text:
            offenders.append(relative)

    assert offenders == []

def test_release_metadata_sync_target_is_the_only_release_bump_path() -> None:
    makefile = read_combined_makefiles(ROOT)
    distribution = (ROOT / "docs" / "distribution.md").read_text(encoding="utf-8")
    script = (ROOT / "scripts" / "sync_release_metadata.py").read_text(encoding="utf-8")

    assert "release-version-resolve:" in makefile
    assert "release-version-sync:" in makefile
    assert "RELEASE_BUMP ?=" in makefile
    assert "scripts/sync_release_metadata.py" in makefile
    assert "--bump \"$(RELEASE_BUMP)\"" in makefile
    assert "--emit-version" in makefile
    assert "release-version-resolve RELEASE_BUMP=patch" in distribution
    assert "import logging" in script
    assert "print(" not in script

def test_public_release_workflows_cover_ci_package_and_registry_publish() -> None:
    workflows = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    }

    assert set(workflows) == {"ci.yml", "publish-everywhere.yml"}
    assert "make precommit" in workflows["ci.yml"]
    assert "make release-smoke" in workflows["ci.yml"]
    assert "make release RELEASE_APPLY=1" in workflows["publish-everywhere.yml"]
    assert "release_version:" in workflows["publish-everywhere.yml"]
    assert "release_bump:" in workflows["publish-everywhere.yml"]
    assert "RELEASE_VERSION=$version" in workflows["publish-everywhere.yml"]
    assert "make --no-print-directory release-version-resolve" in workflows["publish-everywhere.yml"]
    assert 'RELEASE_VERSION="$RELEASE_VERSION"' in workflows["publish-everywhere.yml"]
    assert "make publish-version-check" in workflows["ci.yml"]
    assert "make npm-package-check" in workflows["ci.yml"]
    assert "make npm-smoke" in workflows["ci.yml"]
    assert "release:" in workflows["publish-everywhere.yml"]
    assert "published" in workflows["publish-everywhere.yml"]
    assert "contents: write" in workflows["publish-everywhere.yml"]
    assert "id-token: write" in workflows["publish-everywhere.yml"]
    assert "packages: write" in workflows["publish-everywhere.yml"]
    assert "PYTEST_MARKER_EXPRESSION:" not in workflows["publish-everywhere.yml"]
    assert "publish-pypi.yml" not in workflows
    assert "publish-python.yml" not in workflows
    assert "publish-mcp-registry.yml" not in workflows

def test_package_build_targets_are_available_through_make() -> None:
    make_vars = read_make_variable_defaults(ROOT)
    makefile = read_combined_makefiles(ROOT)
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    normalized_requirements = {
        line.strip()
        for line in requirements.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }

    assert "package-build:" in makefile
    assert "package-check:" in makefile
    assert 'MCP_BROKER_VERSION="$(PACKAGE_VERSION)" $(PYTHON) -m build' in makefile
    assert "$(PYTHON) -m build" in makefile
    assert "$(PYTHON) -m twine check" in makefile
    assert "build==" in requirements
    assert "twine==" in requirements
    assert "pytest==9.0.3" in requirements
    assert "pytest-xdist==3.8.0" in requirements
    assert pyproject["project"]["license"] == "MIT"
    assert pyproject["project"]["authors"] == [{"name": make_vars["PACKAGE_AUTHOR"]}]
    for dependency in pyproject["project"]["dependencies"]:
        package_name = re.split(r"[<>=~!]", dependency, maxsplit=1)[0]
        assert any(line.startswith(package_name + "==") for line in normalized_requirements)

def test_docker_distribution_has_oci_labels_and_multi_arch_release_target() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    makefile = read_combined_makefiles(ROOT)
    distribution = (ROOT / "docs" / "distribution.md").read_text(encoding="utf-8")

    for term in [
        "ARG VERSION=",
        "ARG VCS_REF=",
        "ARG SOURCE_URL=",
        "ARG AUTHORS=",
        "org.opencontainers.image.title",
        "org.opencontainers.image.version",
        "org.opencontainers.image.revision",
        "org.opencontainers.image.source",
        "org.opencontainers.image.licenses",
    ]:
        assert term in dockerfile
    assert 'org.opencontainers.image.licenses="MIT"' in dockerfile
    assert 'org.opencontainers.image.authors="${AUTHORS}"' in dockerfile
    assert '--build-arg AUTHORS="$(PACKAGE_AUTHOR)"' in makefile

    for term in [
        "docker-buildx:",
        "DOCKER_PLATFORMS",
        "--sbom=$(DOCKER_SBOM)",
        "--provenance=$(DOCKER_PROVENANCE)",
        "--platform \"$(DOCKER_PLATFORMS)\"",
    ]:
        assert term in makefile

    assert 'SBOM_ARG="false"' in makefile
    assert 'PROVENANCE_ARG="false"' in makefile

    assert "SBOM" in distribution
    assert "provenance" in distribution
    assert "linux/amd64,linux/arm64" in distribution

def test_npm_and_docker_distribution_decisions_are_recorded() -> None:
    make_vars = read_make_variable_defaults(ROOT)
    npm_doc = (ROOT / "docs" / "npm-distribution.md").read_text(encoding="utf-8")
    distribution = (ROOT / "docs" / "distribution.md").read_text(encoding="utf-8")
    maintainer_inputs_path = ROOT / "docs" / "p16-maintainer-inputs.md"
    allowlist_path = ROOT / "public-export" / "allowlist.txt"

    assert "`mcp-broker` on NPM is a different project" in npm_doc
    assert "`${NPM_PACKAGE_NAME}`" in npm_doc
    assert "does not reimplement the Python broker in Node" in npm_doc
    assert "authenticated local npm configuration" in npm_doc
    assert "NPM is an optional bridge package" in distribution
    assert "Current source release: GitHub Release" in distribution
    assert "was recovered on" in distribution
    assert "GHCR manifest verification" in distribution
    assert "points at the public repository main commit" in distribution
    assert "`v${PACKAGE_VERSION}`" in distribution
    assert "${DOCKER_REPOSITORY_IMAGE}" in distribution
    assert "${GHCR_REPOSITORY_IMAGE}" in distribution
    assert "Docker Hub is the primary image for Docker MCP Catalog work" in distribution
    if maintainer_inputs_path.exists():
        maintainer_inputs = maintainer_inputs_path.read_text(encoding="utf-8")
        assert "Do not publish unscoped `mcp-broker` to NPM" in maintainer_inputs
    if allowlist_path.exists():
        assert "docs/npm-distribution.md" in allowlist_path.read_text(encoding="utf-8")

def test_publish_everywhere_is_single_release_orchestrator() -> None:
    makefile = read_combined_makefiles(ROOT)
    npm_doc = (ROOT / "docs" / "npm-distribution.md").read_text(encoding="utf-8")
    distribution = (ROOT / "docs" / "distribution.md").read_text(encoding="utf-8")
    distribution_plan_path = (
        ROOT / "docs" / "plans" / "2026-05-26-npm-docker-distribution.md"
    )
    workflow = (ROOT / ".github" / "workflows" / "publish-everywhere.yml").read_text(
        encoding="utf-8"
    )

    for target in [
        "release-version-check:",
        "release-check:",
        "release:",
        "package-install-smoke:",
        "public-stable-surface-smoke:",
        "public-release-surface-smoke:",
        "public-release-live-verify:",
        "publish-everywhere-check:",
        "publish-everywhere:",
        "_publish-everywhere-pypi:",
        "_publish-everywhere-npm:",
        "_publish-everywhere-docker:",
        "_publish-everywhere-mcp-registry:",
        "_publish-everywhere-homebrew:",
        "_publish-everywhere-live-verify-registries:",
        "_publish-everywhere-github-release:",
        "_publish-everywhere-live-verify-github-release:",
        "docker-mcp-catalog-smoke:",
        "docker-publish-check:",
        "docker-release-smoke:",
        "publish-version-check:",
    ]:
        assert target in makefile

    for target in [
        "publish-pypi:",
        "publish-npm:",
        "publish-docker-images:",
        "publish-mcp-registry:",
    ]:
        assert target not in makefile

    assert "scripts/check_release_versions.py" in makefile
    assert "PUBLIC_RELEASE_PYTEST_MARKER_EXPRESSION ?= not private_contract" in makefile
    assert "RELEASE_GATE_PYTEST_MARKER_EXPRESSION ?=" in makefile
    assert "scripts/update_homebrew_formula.py" in makefile
    assert "scripts/public-surface-smoke.sh" in makefile
    assert "scripts/verify_public_release.py" in makefile
    assert "pipx run --spec \"mcp-broker==$" in makefile
    assert '"$(UVX)" --from "mcp-broker==$' in makefile
    assert "PUBLIC_SURFACE_REQUIRE_NPM=1" in makefile
    assert "PUBLIC_SURFACE_REQUIRE_DOCKER=1" in makefile
    assert "PYPI_VERSION_URL" in makefile
    assert "MCP_REGISTRY_SEARCH_URL" in makefile
    assert "HOMEBREW_TAP_TOKEN" in makefile
    assert "Homebrew formula already current" in makefile
    assert "--pypi-attempts \"$(HOMEBREW_PYPI_ATTEMPTS)\"" in makefile
    assert "GIT_ASKPASS=\"$$tmpdir/git-askpass.sh\"" in makefile
    assert "extraheader=\"AUTHORIZATION: bearer $${HOMEBREW_TAP_TOKEN}\"" not in makefile
    assert "x-access-token:$${HOMEBREW_TAP_TOKEN}" not in makefile
    assert "publish-version-check" in makefile
    assert '"$(UV)" publish --trusted-publishing "$(PYPI_TRUSTED_PUBLISHING)"' in makefile
    assert "$(NPM) publish --access public $(NPM_PUBLISH_PROVENANCE_ARGS)" in makefile
    assert "--push" in makefile
    assert 'mcp-publisher login "$(MCP_REGISTRY_LOGIN_METHOD)"' in makefile
    assert "make release RELEASE_APPLY=1" in workflow
    assert "make publish-everywhere PUBLISH_EVERYWHERE_APPLY=1" not in workflow
    assert "release:" in workflow
    assert "published" in workflow
    assert "id-token: write" in workflow
    assert "packages: write" in workflow
    assert "contents: write" in workflow
    assert "DOCKERHUB_USERNAME" in workflow
    assert "DOCKERHUB_TOKEN" in workflow
    assert "NODE_AUTH_TOKEN: ${{ secrets.NPM_TOKEN }}" in workflow
    assert 'node-version: "24"' in workflow
    assert "astral-sh/setup-uv" in workflow
    assert "actions/setup-node" in workflow
    assert "docker/setup-buildx-action" in workflow
    assert "docker/setup-buildx-action@v4" in workflow
    assert "docker/login-action@v4" in workflow
    assert "docker/setup-buildx-action@v3" not in workflow
    assert "docker/login-action@v3" not in workflow
    assert "docker/login-action" in workflow
    assert "uv publish" not in workflow
    assert "npm publish" not in workflow
    assert "docker buildx build" not in workflow
    assert "mcp-publisher publish" not in workflow
    assert "workflow_run:" not in workflow
    assert "push:" not in workflow
    assert "make release RELEASE_APPLY=1" in npm_doc
    assert "Local publication is the default" in npm_doc
    assert "GitHub Actions stays disabled" in npm_doc
    assert ".github/workflows/publish-npm.yml" not in npm_doc
    assert "`npm whoami`" in npm_doc
    assert "first publish returned `E404`" not in npm_doc
    assert ".github/workflows/publish-pypi.yml" not in distribution
    assert ".github/workflows/publish-python.yml" not in distribution
    assert ".github/workflows/publish-mcp-registry.yml" not in distribution
    assert "Local publication is the default" in distribution
    assert "GitHub Actions stays disabled" in distribution
    assert "Manual per-registry workflows remain" not in distribution
    if distribution_plan_path.exists():
        distribution_plan = distribution_plan_path.read_text(encoding="utf-8")
        assert ".github/workflows/publish-docker.yml" not in distribution_plan
        assert 'assert "npm publish" in workflow' not in distribution_plan
        assert 'assert "make docker-publish-check" in workflow' not in distribution_plan
        assert "Manual per-registry workflows remain" not in distribution_plan
        assert "publish-pypi" not in distribution_plan
        assert "publish-npm" not in distribution_plan
        assert "publish-docker-images" not in distribution_plan
        assert "publish-mcp-registry" not in distribution_plan
        assert "fallbacks only" not in distribution_plan
        assert "Push `1.0.0` and semver aliases" not in distribution_plan

def test_publish_everywhere_skips_only_after_registry_metadata_verification() -> None:
    makefile = read_combined_makefiles(ROOT)
    distribution = (ROOT / "docs" / "distribution.md").read_text(encoding="utf-8")

    assert "scripts/release_idempotency.py" in makefile
    assert "--surface pypi" in makefile
    assert "--surface npm" in makefile
    assert "--surface mcp-registry" in makefile
    assert "digest mismatch" in makefile
    assert "PyPI package already verified" in makefile
    assert '$(NPM) view "$(NPM_PACKAGE_NAME)@$(PACKAGE_VERSION)" version' not in makefile
    assert "NPM package already verified" in makefile
    assert "MCP Registry metadata already verified" in makefile
    assert "PyPI artifact digests" in distribution
    assert "NPM package integrity" in distribution
    assert "MCP Registry name/version" in distribution
    assert "metadata" in distribution

def test_public_export_includes_public_release_verifier_when_export_rules_exist() -> None:
    allowlist_path = ROOT / "public-export" / "allowlist.txt"
    if not allowlist_path.exists():
        return

    allowlist = allowlist_path.read_text(encoding="utf-8")

    assert "scripts/release_idempotency.py" in allowlist
    assert "scripts/verify_public_release.py" in allowlist
