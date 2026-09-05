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

def test_publish_everywhere_orchestration_is_sequenced_and_parallel() -> None:
    makefile = read_combined_makefiles(ROOT)
    make_vars = read_make_variable_defaults(ROOT)

    release_section = makefile.split("_release-impl:", maxsplit=1)[1].split(
        "publish-version-check:",
        maxsplit=1,
    )[0]
    release_check_section = makefile.split("release-check:", maxsplit=1)[1].split(
        "publish-everywhere-check:",
        maxsplit=1,
    )[0]
    check_section = makefile.split("publish-everywhere-check:", maxsplit=1)[1].split(
        "publish-everywhere:",
        maxsplit=1,
    )[0]
    publish_section = makefile.split("publish-everywhere:", maxsplit=1)[1].split(
        "_publish-everywhere-pypi:",
        maxsplit=1,
    )[0]
    pypi_index = publish_section.index("_publish-everywhere-pypi")
    fanout_index = publish_section.index("_publish-everywhere-registry-fanout")
    registry_verify_index = publish_section.index("_publish-everywhere-live-verify-registries")
    github_release_index = publish_section.index("_publish-everywhere-github-release")
    github_verify_index = publish_section.index("_publish-everywhere-live-verify-github-release")

    assert "PUBLISH_CHECK_JOBS ?= 2" in makefile
    assert "PUBLISH_EVERYWHERE_JOBS ?= 4" in makefile
    assert "RELEASE_APPLY ?= 0" in makefile
    assert "RELEASE_VERSION ?=" in makefile
    for variable in [
        "PYPI_PROJECT_NAME",
        "PYPI_VERSION_URL",
        "PYPI_SIMPLE_CHECK_URL",
        "GITHUB_REPO",
        "GITHUB_RELEASE_TAG",
        "GITHUB_RELEASE_URL",
        "GITHUB_TAG_SOURCE_TARBALL_URL",
        "DOCKER_HUB_REPOSITORY_URL",
        "DOCKER_HUB_RELEASE_TAG_URL",
        "DOCKER_HUB_MINOR_TAG_URL",
        "DOCKER_HUB_API_REPOSITORY_BASE_URL",
        "DOCKER_HUB_API_NAMESPACE_BASE_URL",
        "DOCKER_HUB_LEGACY_REPOSITORY_BASE_URL",
        "DOCKER_HUB_LOGIN_URL",
        "DOCKER_REGISTRY_HOST",
        "DOCKER_REGISTRY_SERVICE",
        "DOCKER_REGISTRY_AUTH_URL",
        "DOCKER_REGISTRY_MANIFEST_BASE_URL",
        "GHCR_REGISTRY_HOST",
        "GHCR_REGISTRY_SERVICE",
        "GHCR_REGISTRY_AUTH_URL",
        "GHCR_REGISTRY_MANIFEST_BASE_URL",
        "MCP_REGISTRY_NAME",
        "MCP_REGISTRY_API_BASE_URL",
        "MCP_REGISTRY_SEARCH_URL",
        "MCP_PUBLISHER_RELEASE_DOWNLOAD_BASE_URL",
        "NPM_REGISTRY_URL",
        "HOMEBREW_TAP_CLONE_URL",
        "SMITHERY_API_BASE_URL",
    ]:
        assert variable in make_vars
    assert "$(PYPI_PROJECT_NAME)" in make_vars["PYPI_VERSION_URL"]
    assert "$(PACKAGE_VERSION)" in make_vars["PYPI_VERSION_URL"]
    assert "$(PACKAGE_VERSION)" in make_vars["GITHUB_RELEASE_TAG"]
    assert "$(GITHUB_REPO)" in make_vars["GITHUB_RELEASE_URL"]
    assert "$(GITHUB_RELEASE_TAG)" in make_vars["GITHUB_RELEASE_URL"]
    assert "$(DOCKER_NAMESPACE)" in make_vars["DOCKER_HUB_REPOSITORY_URL"]
    assert "$(DOCKER_IMAGE_NAME)" in make_vars["DOCKER_HUB_REPOSITORY_URL"]
    assert "$(DOCKER_RELEASE_TAG)" in make_vars["DOCKER_HUB_RELEASE_TAG_URL"]
    assert "$(PACKAGE_MINOR_VERSION)" in make_vars["DOCKER_HUB_MINOR_TAG_URL"]
    assert "$(MCP_REGISTRY_NAME)" in make_vars["MCP_REGISTRY_SEARCH_URL"]
    assert "$(GITHUB_REPOSITORY_URL)" in make_vars["GITHUB_TAG_SOURCE_TARBALL_URL"]
    assert "$(PYPI_PROJECT_NAME)" in make_vars["PYPI_SIMPLE_CHECK_URL"]
    assert "$(HOMEBREW_TAP_URL)" in make_vars["HOMEBREW_TAP_CLONE_URL"]
    assert "release-version-check" in release_check_section
    assert "publish-everywhere-check" in release_check_section
    assert "directory-submission-check mcpb-smoke smithery-payload-check" in release_check_section
    assert '$(call timed_make,"release-check: publish preflight",publish-everywhere-check)' in release_check_section
    assert '$(call timed_make,"release-check: directory and bundle metadata",$(call parallel_make_args,$(PUBLISH_CHECK_JOBS)) directory-submission-check mcpb-smoke smithery-payload-check)' in release_check_section
    assert '$(call timed_make,"release: preflight",release-check)' in release_section
    assert '$(call timed_make,"release: publish",PUBLISH_EVERYWHERE_APPLY=1 PUBLISH_EVERYWHERE_SKIP_CHECKS=1 publish-everywhere)' in release_section
    assert "publish-version-check" in check_section
    release_gate_index = check_section.index("release-gate")
    publish_check_fanout_index = check_section.index("npm-package-check npm-smoke _publish-check-docker-smoke _publish-check-docker-buildx")
    assert "_publish-check-docker-smoke:" in makefile
    assert "_publish-check-docker-buildx:" in makefile
    assert "docker-hub-public-ensure:" in makefile
    assert "_publish-everywhere-required-env-check:" in makefile
    assert "_publish-everywhere-docker-hub-public:" in makefile
    assert "_publish-everywhere-live-verify-registries:" in makefile
    assert "_publish-everywhere-github-release:" in makefile
    assert "_publish-everywhere-live-verify-github-release:" in makefile
    assert '$(call timed_make,"publish-everywhere-check: release gate",PYTEST_MARKER_EXPRESSION="$(RELEASE_GATE_PYTEST_MARKER_EXPRESSION)" release-gate)' in check_section
    assert '$(call timed_make,"publish-everywhere-check: package smoke children",$(call parallel_make_args,$(PUBLISH_CHECK_JOBS)) npm-package-check npm-smoke _publish-check-docker-smoke _publish-check-docker-buildx)' in check_section
    assert release_gate_index < publish_check_fanout_index
    assert 'docker-smoke DOCKER_IMAGE="mcp-broker:publish-check"' in makefile
    assert 'docker-buildx DOCKER_IMAGE="mcp-broker:buildx-check" DOCKER_PLATFORMS="$(DOCKER_LOCAL_PLATFORM)"' in makefile
    assert '$(call timed_make,"publish-everywhere: required env",_publish-everywhere-required-env-check)' in publish_section
    assert '$(call timed_make,"publish-everywhere: docker hub public repository",_publish-everywhere-docker-hub-public)' in publish_section
    assert publish_section.index("_publish-everywhere-required-env-check") < pypi_index
    assert publish_section.index("_publish-everywhere-docker-hub-public") < pypi_index
    assert 'HOMEBREW_EFFECTIVE_TOKEN="$${HOMEBREW_TAP_TOKEN:-}"' in makefile
    assert 'HOMEBREW_EFFECTIVE_TOKEN="$$(gh auth token)"' in makefile
    assert "DOCKERHUB_USERNAME is required before publish-everywhere starts" in makefile
    assert "DOCKERHUB_TOKEN is required before publish-everywhere starts" in makefile
    assert '$(call timed_make,"publish-everywhere: pypi",_publish-everywhere-pypi)' in publish_section
    assert '$(call timed_make,"publish-everywhere: registry fanout",_publish-everywhere-registry-fanout)' in publish_section
    assert '$(call timed_make,"publish-everywhere: live registry verification",_publish-everywhere-live-verify-registries)' in publish_section
    assert '$(call timed_make,"publish-everywhere: github release",_publish-everywhere-github-release)' in publish_section
    assert '$(call timed_make,"publish-everywhere: github release verification",_publish-everywhere-live-verify-github-release)' in publish_section
    assert "PUBLISH_EVERYWHERE_SKIP_CHECKS ?= 0" in makefile
    assert "publish-everywhere: current local preflight evidence reused" in makefile
    assert pypi_index < fanout_index
    assert fanout_index < registry_verify_index < github_release_index < github_verify_index
    assert 'docker buildx build \\' in makefile
    assert '$(call timed_make,"publish child: docker-publish-check",docker-publish-check)' in makefile
    assert "\n\t+@label=\"$(call strip_quotes,$(1))\"" in makefile
    assert "\n\t@label=\"$(call strip_quotes,$(1))\"" not in makefile

def test_public_release_live_verification_proves_registry_truth_before_github_latest() -> None:
    make_vars = read_make_variable_defaults(ROOT)
    makefile = read_combined_makefiles(ROOT)
    verifier = (ROOT / "scripts" / "verify_public_release.py").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "publish-everywhere.yml").read_text(
        encoding="utf-8"
    )
    distribution = (ROOT / "docs" / "distribution.md").read_text(encoding="utf-8")
    normalized_distribution = " ".join(distribution.split())

    registries_section = makefile.split(
        "_publish-everywhere-live-verify-registries:",
        maxsplit=1,
    )[1].split("_publish-everywhere-github-release:", maxsplit=1)[0]
    github_release_section = makefile.split(
        "_publish-everywhere-github-release:",
        maxsplit=1,
    )[1].split("_publish-everywhere-live-verify-github-release:", maxsplit=1)[0]
    github_verify_section = makefile.split(
        "_publish-everywhere-live-verify-github-release:",
        maxsplit=1,
    )[1].split("_publish-everywhere-pypi:", maxsplit=1)[0]

    assert "public-release-live-verify:" in makefile
    assert "--checks pypi,npm,docker-hub,docker-registry,ghcr,mcp-registry,homebrew" in registries_section
    assert "--checks github-release" in github_verify_section
    assert "gh release view \"$(GITHUB_RELEASE_TAG)\"" in github_release_section
    assert "gh release create \"$(GITHUB_RELEASE_TAG)\"" in github_release_section
    assert "gh release edit \"$(GITHUB_RELEASE_TAG)\"" in github_release_section
    assert "Docker Hub repository is not public" in verifier
    assert "Docker Hub tag is not public" in verifier
    for variable in [
        "DOCKER_HUB_API_REPOSITORY_BASE_URL",
        "DOCKER_REGISTRY_AUTH_URL",
        "GHCR_REGISTRY_AUTH_URL",
        "DOCKER_REGISTRY_HOST",
        "GHCR_REGISTRY_HOST",
        "MCP_REGISTRY_API_BASE_URL",
        "NPM_REGISTRY_URL",
        "PYPI_VERSION_URL",
        "GITHUB_RELEASE_URL",
    ]:
        assert expand_make_value(make_vars, make_vars[variable]) not in verifier
    assert "github_repo:" not in verifier
    assert "pypi_project:" not in verifier
    assert "npm_package: str =" not in verifier
    assert "docker_namespace: str =" not in verifier
    assert "docker_image_name: str =" not in verifier
    assert "mcp_registry_name: str =" not in verifier
    assert "homebrew_formula_url: str =" not in verifier
    assert "--github-repo" not in makefile
    assert "--pypi-project" not in makefile
    assert "--npm-package \"$(NPM_PACKAGE_NAME)\"" in makefile
    assert "--docker-namespace \"$(DOCKER_NAMESPACE)\"" in makefile
    assert "--docker-image-name \"$(DOCKER_IMAGE_NAME)\"" in makefile
    assert "--mcp-registry-name \"$(MCP_REGISTRY_NAME)\"" in makefile
    assert "--github-release-url \"$(GITHUB_RELEASE_URL)\"" in makefile
    assert "--pypi-version-url \"$(PYPI_VERSION_URL)\"" in makefile
    assert "--npm-registry-url \"$(NPM_REGISTRY_URL)\"" in makefile
    assert "--docker-hub-api-repository-base-url \"$(DOCKER_HUB_API_REPOSITORY_BASE_URL)\"" in makefile
    assert "--docker-registry-service \"$(DOCKER_REGISTRY_SERVICE)\"" in makefile
    assert "--docker-registry-host \"$(DOCKER_REGISTRY_HOST)\"" in makefile
    assert "--docker-registry-auth-url \"$(DOCKER_REGISTRY_AUTH_URL)\"" in makefile
    assert "--docker-registry-manifest-base-url \"$(DOCKER_REGISTRY_MANIFEST_BASE_URL)\"" in makefile
    assert "--ghcr-registry-service \"$(GHCR_REGISTRY_SERVICE)\"" in makefile
    assert "--ghcr-registry-host \"$(GHCR_REGISTRY_HOST)\"" in makefile
    assert "--ghcr-registry-auth-url \"$(GHCR_REGISTRY_AUTH_URL)\"" in makefile
    assert "--ghcr-registry-manifest-base-url \"$(GHCR_REGISTRY_MANIFEST_BASE_URL)\"" in makefile
    assert "--mcp-registry-search-url \"$(MCP_REGISTRY_SEARCH_URL)\"" in makefile
    assert "--homebrew-formula-url \"$(HOMEBREW_FORMULA_RAW_URL)\"" in makefile
    assert "--base-url \"$(SMITHERY_API_BASE_URL)\"" in makefile
    assert (
        "MCP_PUBLISHER_RELEASE_DOWNLOAD_BASE_URL=$(make --no-print-directory print-var "
        "VAR=MCP_PUBLISHER_RELEASE_DOWNLOAD_BASE_URL)"
    ) in workflow
    assert "${MCP_PUBLISHER_RELEASE_DOWNLOAD_BASE_URL}/mcp-publisher_" in workflow
    assert "DOCKERHUB_USERNAME: ${{ secrets.DOCKERHUB_USERNAME }}" in workflow
    assert "DOCKERHUB_TOKEN: ${{ secrets.DOCKERHUB_TOKEN }}" in workflow
    assert "public live verification" in distribution
    assert "GitHub Release is created only after registry verification passes" in normalized_distribution

def test_publish_everywhere_records_registry_failures_then_runs_live_verification() -> None:
    makefile = read_combined_makefiles(ROOT)
    make_vars = read_make_variable_defaults(ROOT)

    publish_section = makefile.split("_publish-everywhere-impl:", maxsplit=1)[1].split(
        "_publish-everywhere-preflight:",
        maxsplit=1,
    )[0]
    fanout_section = makefile.split(
        "_publish-everywhere-registry-fanout:",
        maxsplit=1,
    )[1].split("_publish-everywhere-preflight:", maxsplit=1)[0]

    assert "RELEASE_TRANSACTION_LEDGER" in make_vars
    assert "scripts/release_fanout.py" in makefile
    assert "_publish-everywhere-registry-fanout" in publish_section
    assert "-j $(PUBLISH_EVERYWHERE_JOBS) _publish-everywhere-npm" not in publish_section
    assert "release_fanout.py" in fanout_section
    assert "--ledger \"$(RELEASE_TRANSACTION_LEDGER)\"" in fanout_section
    assert '--step "npm::$(MAKE) --no-print-directory _publish-everywhere-npm"' in fanout_section
    assert '--step "docker::$(MAKE) --no-print-directory _publish-everywhere-docker"' in fanout_section
    assert (
        '--step "mcp-registry::$(MAKE) --no-print-directory _publish-everywhere-mcp-registry"'
        in fanout_section
    )
    assert (
        '--step "homebrew::$(MAKE) --no-print-directory _publish-everywhere-homebrew"'
        in fanout_section
    )
    assert publish_section.index("_publish-everywhere-registry-fanout") < publish_section.index(
        "_publish-everywhere-live-verify-registries"
    )
    assert publish_section.index("_publish-everywhere-live-verify-registries") < publish_section.index(
        "_publish-everywhere-github-release"
    )

def test_publish_everywhere_auth_preflight_runs_before_first_registry_write() -> None:
    makefile = read_combined_makefiles(ROOT)
    publish_section = makefile.split("_publish-everywhere-impl:", maxsplit=1)[1].split(
        "_publish-everywhere-preflight:",
        maxsplit=1,
    )[0]
    auth_section = makefile.split(
        "_publish-everywhere-auth-preflight:",
        maxsplit=1,
    )[1].split("_publish-everywhere-docker-hub-public:", maxsplit=1)[0]

    assert "_publish-everywhere-auth-preflight" in publish_section
    assert publish_section.index("_publish-everywhere-auth-preflight") < publish_section.index(
        "_publish-everywhere-pypi"
    )
    assert '$(NPM) whoami --registry "$(NPM_REGISTRY_URL)"' in auth_section
    assert "gh auth status --hostname github.com" in auth_section
    assert 'gh api "orgs/$(PUBLIC_NAMESPACE)/packages/container/$(DOCKER_IMAGE_NAME)"' in auth_section
    assert "GHCR package is not public" in auth_section
    assert "GIT_ASKPASS=\"$$tmpdir/git-askpass.sh\"" in auth_section
    assert "git ls-remote \"$(HOMEBREW_TAP_CLONE_URL)\" HEAD" in auth_section
    assert '"$(MCP_REGISTRY_LOGIN_METHOD)"' in auth_section


def test_publish_everywhere_defaults_to_local_auth_without_github_actions() -> None:
    makefile = read_combined_makefiles(ROOT)
    make_vars = read_make_variable_defaults(ROOT)

    assert make_vars["PUBLISH_EXECUTION_MODE"] == "local"
    assert make_vars["PYPI_TRUSTED_PUBLISHING"] == "never"
    assert make_vars["NPM_PUBLISH_PROVENANCE_ARGS"] == ""
    assert make_vars["MCP_REGISTRY_LOGIN_METHOD"] == "github"


def test_docker_hub_token_stays_out_of_process_arguments() -> None:
    makefile = read_combined_makefiles(ROOT)
    script = (ROOT / "scripts" / "ensure_docker_hub_public.py").read_text(encoding="utf-8")
    target = makefile.split("docker-hub-public-ensure:", maxsplit=1)[1].split(
        "docker-publish-check:", maxsplit=1
    )[0]

    assert '--token "$${DOCKERHUB_TOKEN}"' not in target
    assert 'parser.add_argument("--token"' not in script
    assert "dockerhub_token_from_env(os.environ)" in script
    assert "publish-everywhere must run in GitHub Actions" not in makefile
    assert "release must run in GitHub Actions" not in makefile
    assert 'case "$(PUBLISH_EXECUTION_MODE)" in' in makefile
    assert '"$(UV)" publish --trusted-publishing "$(PYPI_TRUSTED_PUBLISHING)"' in makefile
    assert "$(NPM) publish --access public $(NPM_PUBLISH_PROVENANCE_ARGS)" in makefile
    assert 'mcp-publisher login "$(MCP_REGISTRY_LOGIN_METHOD)"' in makefile
    assert '--token "$$(gh auth token)"' not in makefile
    assert "local publication requires PYPI_TRUSTED_PUBLISHING=never" in makefile
    assert "local publication forbids NPM provenance" in makefile
    assert "local publication requires MCP_REGISTRY_LOGIN_METHOD=github" in makefile
    assert "UV_PUBLISH_TOKEN is required for local PyPI publication" in makefile
    assert "NODE_AUTH_TOKEN is required for CI publication" in makefile
    assert "HOMEBREW_EFFECTIVE_TOKEN=\"$${HOMEBREW_TAP_TOKEN:-}\"" in makefile
    assert "HOMEBREW_EFFECTIVE_TOKEN=\"$$(gh auth token)\"" in makefile

    github_release_section = makefile.split(
        "_publish-everywhere-github-release:",
        maxsplit=1,
    )[1].split("_publish-everywhere-live-verify-github-release:", maxsplit=1)[0]
    assert "GH_TOKEN or GITHUB_TOKEN is required" not in github_release_section

def test_public_release_coordinates_are_centralized_in_make_config() -> None:
    make_vars = read_make_variable_defaults(ROOT)
    assert _find_public_coordinate_offenders(make_vars) == []

def test_docker_mcp_catalog_smoke_uses_file_metadata_boundary() -> None:
    make_vars = read_make_variable_defaults(ROOT)
    makefile = read_combined_makefiles(ROOT)
    catalog_file = ROOT / "docker" / "mcp-catalog" / "mcp-broker.yaml"
    catalog_text = catalog_file.read_text(encoding="utf-8")
    distribution = (ROOT / "docs" / "distribution.md").read_text(encoding="utf-8")

    for term in [
        "name: mcp-broker",
        "title: mcp-broker",
        "type: server",
        (
            "image: "
            f"{expand_make_value(make_vars, make_vars['DOCKER_REGISTRY'])}/"
            f"{expand_make_value(make_vars, make_vars['DOCKER_NAMESPACE'])}/"
            f"{expand_make_value(make_vars, make_vars['DOCKER_IMAGE_NAME'])}:"
            f"{_package_version()}"
        ),
        "description: Local MCP broker",
    ]:
        assert term in catalog_text

    assert "docker-mcp-catalog-smoke:" in makefile
    assert "docker mcp catalog create" in makefile
    assert "--server \"file://$(DOCKER_MCP_CATALOG_FILE)\"" in makefile
    assert "docker mcp catalog server ls" in makefile
    assert "docker mcp catalog remove" in makefile
    assert "DOCKER_MCP_CATALOG_FILE ?= $(ROOT)/docker/mcp-catalog/$(PACKAGE_SLUG).yaml" in makefile
    assert "DOCKER_MCP_CATALOG_REF ?= $(PACKAGE_SLUG)-local-catalog:local" in makefile
    release_smoke = re.search(
        r"(?ms)^docker-release-smoke:.*?(?=^[A-Za-z0-9_.-]+:|\Z)",
        makefile,
    )
    assert release_smoke is not None
    release_smoke_body = release_smoke.group(0)
    assert '> "$(TEST_LOG_DIR)/docker-release-smoke.jsonl"' in release_smoke_body
    assert 'grep -q \'"tools"\' "$(TEST_LOG_DIR)/docker-release-smoke.jsonl"' in release_smoke_body
    assert '| grep -q \'"tools"\'' not in release_smoke_body
    assert "Docker MCP Toolkit custom catalog smoke uses file-based server metadata" in distribution
    assert "The Docker image itself is not treated as self-describing" in distribution

def test_docker_mcp_registry_submission_packet_is_staged() -> None:
    make_vars = read_make_variable_defaults(ROOT)
    submission = (ROOT / "docs" / "docker-mcp-registry-submission.md").read_text(
        encoding="utf-8"
    )
    catalog = ROOT / "docker" / "mcp-catalog" / "mcp-broker.yaml"

    for term in [
        "${DOCKER_REPOSITORY_IMAGE}:${PACKAGE_VERSION}",
        "${GHCR_REPOSITORY_IMAGE}:${PACKAGE_VERSION}",
        "make docker-smoke",
        "make docker-mcp-catalog-smoke",
        "No hidden host client config writes",
        "PR submitted and pending external Docker review",
        "${DOCKER_MCP_CATALOG_PR_URL}",
        "mergeStateStatus=BLOCKED",
        "REVIEW_REQUIRED",
    ]:
        assert term in submission

    assert catalog.is_file()

def test_public_surface_smoke_downloads_real_public_artifacts() -> None:
    script = (ROOT / "scripts" / "public-surface-smoke.sh").read_text(encoding="utf-8")
    distribution = (ROOT / "docs" / "distribution.md").read_text(encoding="utf-8")

    assert "PYTHONPATH=\"\"" in script
    assert "DOCKER_OUTPUT=" in script
    assert "grep -q '\"tools\"' \"$DOCKER_OUTPUT\"" in script
    for term in [
        "mktemp -d",
        "require_env PYPI_PROJECT_NAME",
        "require_env PACKAGE_COMMAND_NAME",
        "require_env GITHUB_TAG_SOURCE_TARBALL_URL",
        "require_env MCP_REGISTRY_SEARCH_URL",
        "pip install \"$PYPI_PROJECT_NAME==$PUBLIC_SURFACE_VERSION\"",
        "pipx run --spec \"$PYPI_PROJECT_NAME==$PUBLIC_SURFACE_VERSION\"",
        "uvx --from \"$PYPI_PROJECT_NAME==$PUBLIC_SURFACE_VERSION\"",
        "\"$GITHUB_TAG_SOURCE_TARBALL_URL\"",
        "HOMEBREW_CACHE=\"$WORK_DIR/homebrew-cache\"",
        "brew update --force --quiet",
        "brew fetch --formula \"$HOMEBREW_FORMULA_REF\"",
        "brew upgrade \"$HOMEBREW_FORMULA_REF\"",
        "brew list --formula --versions \"$PYPI_PROJECT_NAME\"",
        "brew test \"$HOMEBREW_FORMULA_REF\"",
        "npm view \"$NPM_PACKAGE_NAME@$PUBLIC_SURFACE_VERSION\"",
        "docker buildx imagetools inspect \"$DOCKER_RELEASE_IMAGE\"",
    ]:
        assert term in script

    assert "public-stable-surface-smoke" in distribution
    assert "public-release-surface-smoke" in distribution
    assert "downloads into a temporary directory" in distribution

def test_p16_p18_tracking_has_no_stale_repo_owned_pending_rows() -> None:
    todo_path = ROOT / "TODO.md"
    todo = todo_path.read_text(encoding="utf-8") if todo_path.exists() else ""
    maintainer_inputs_path = ROOT / "docs" / "p16-maintainer-inputs.md"
    maintainer_inputs = (
        maintainer_inputs_path.read_text(encoding="utf-8")
        if maintainer_inputs_path.exists()
        else ""
    )
    plan_path = ROOT / "docs" / "plans" / "2026-05-26-npm-docker-distribution.md"
    plan = plan_path.read_text(encoding="utf-8") if plan_path.exists() else ""

    if todo:
        assert "- [x] Validate `pipx` and `uvx` against the published PyPI package." in todo
    if maintainer_inputs:
        assert "pipx validation date: 2026-05-27" in maintainer_inputs
        assert "uv validation date: 2026-05-27" in maintainer_inputs
        assert "Status: complete for `${PACKAGE_VERSION}`." in maintainer_inputs
        assert "publication pending" not in maintainer_inputs
        assert "NPM_TOKEN" in maintainer_inputs
        assert "NODE_AUTH_TOKEN" in maintainer_inputs
        assert "Status: pending for `1.0.0`." not in maintainer_inputs
        assert "Source changes pending" not in maintainer_inputs
        assert "8326 mutants" not in maintainer_inputs
        assert "`8332` mutants" in maintainer_inputs
    if plan:
        assert "## Progress" in plan
        assert "- [x] Task 4: NPM publication completed through package bootstrap and scoped token auth." in plan
        assert "- [x] Task 7: Docker images published to Docker Hub and GHCR." in plan
        assert "- [x] Task 8: Docker MCP Catalog custom catalog smoke." in plan
        assert "- [x] Task 9: Docker MCP Registry PR packet staged." in plan
        assert "NPM_TOKEN" in plan
        assert "publication remains external" not in plan

def test_release_version_checker_uses_logging_instead_of_print() -> None:
    script = (ROOT / "scripts" / "check_release_versions.py").read_text(encoding="utf-8")

    assert "import logging" in script
    assert "LOGGER = logging.getLogger" in script
    assert '"registry/server.template.json"' in script
    assert '"mcp_registry_template"' in script
    assert '"mcp_registry_template_package"' in script
    assert "print(" not in script

def test_public_runtime_and_release_docs_do_not_use_python_print() -> None:
    scanned_roots = [
        ROOT / "src",
        ROOT / "scripts",
        ROOT / "npm",
        ROOT / "docs",
        ROOT / ".github" / "workflows",
    ]
    scanned_suffixes = {".md", ".py", ".js", ".json", ".sh", ".toml", ".yml", ".yaml"}
    offenders: list[str] = []

    for root in scanned_roots:
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix in scanned_suffixes:
                if "print(" in path.read_text(encoding="utf-8"):
                    offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []

def test_release_smoke_script_uses_tracked_public_files_only() -> None:
    script = ROOT / "scripts" / "release-smoke.sh"
    linux_script = ROOT / "scripts" / "linux-container-smoke.sh"
    linux_release_gate_script = ROOT / "scripts" / "linux-release-gate.sh"
    makefile = read_combined_makefiles(ROOT)
    text = script.read_text(encoding="utf-8")
    linux_text = linux_script.read_text(encoding="utf-8")
    linux_release_gate_text = linux_release_gate_script.read_text(encoding="utf-8")

    assert script.is_file()
    assert "scripts/public-export.py" in text
    assert "--allowlist" in text
    assert "--denylist" in text
    assert "git ls-files -co --exclude-standard -z" in text
    assert '--null \\' in text
    assert '-T "$SOURCE_LIST_PATH" \\' in text
    assert '-C "$ROOT" -cf - .' not in text
    assert 'MAKE_BIN="${MAKE_BIN:-}"' in text
    assert 'if [[ -z "$MAKE_BIN" ]]; then' in text
    assert "for candidate in /opt/homebrew/bin/gmake /usr/local/bin/gmake /usr/bin/make; do" in text
    assert 'MAKE_BIN="$candidate"' in text
    assert 'if [[ -z "$MAKE_BIN" || ! -x "$MAKE_BIN" ]]; then' in text
    assert "make config-init" in text
    assert "make config-validate" in text
    assert "make broker-smoke" in text
    assert '"$MAKE_BIN" config-init' in text
    assert '"$MAKE_BIN" setup' in text
    assert '"$MAKE_BIN" config-validate' in text
    assert '"$MAKE_BIN" broker-smoke' in text
    assert 'XDG_CONFIG_HOME="$XDG_CONFIG_HOME_DIR"' in text
    assert "/Users/" not in text
    export_helper = ROOT / "scripts" / "public-export.py"
    if export_helper.exists():
        assert "/Users/" not in export_helper.read_text(encoding="utf-8")
    assert "config/broker.private.yaml" not in text
    assert "PIP_UPGRADE       ?= 0" in makefile
    assert "tar_option_supported" in linux_text
    assert "TAR_CREATE_OPTIONS" in linux_text
    assert "linux-release-gate" in makefile
    assert "make release-gate" in linux_release_gate_text
    assert "GITHUB_ACTIONS=true" in linux_release_gate_text
    assert "XDG_CONFIG_HOME=/tmp/home/.config" in linux_release_gate_text
    assert "git init -q" in linux_release_gate_text
    assert "git ls-files -co --exclude-standard -z" in linux_release_gate_text
    assert "git config --global --add safe.directory /workspace" in linux_release_gate_text
    assert "git add ." in linux_release_gate_text
    assert '--exclude="var/coverage/*"' not in linux_release_gate_text
    assert "/Users/" not in linux_release_gate_text


def test_release_checklist_uses_local_publication_and_batch_mutation_evidence() -> None:
    checklist = (ROOT / "docs" / "release-checklist.md").read_text(encoding="utf-8")

    assert "GitHub Actions stays disabled" in checklist
    assert "make release RELEASE_APPLY=1" in checklist
    assert "current source hashes" in checklist
    assert "Do not rerun the full mutation inventory" in checklist

def test_systemd_service_contract_uses_runtime_root_and_config_path() -> None:
    script = ROOT / "scripts" / "install-systemd-user.sh"
    uninstall_script = ROOT / "scripts" / "uninstall-systemd-user.sh"
    text = script.read_text(encoding="utf-8")

    assert script.is_file()
    assert uninstall_script.is_file()
    assert "MCP_BROKER_RUNTIME_ROOT" in text
    assert "MCP_BROKER_SOCKET" in text
    assert "MCP_BROKER_CONFIG" in text
    assert "mcp_broker.daemon" in text
    assert "broker-smoke" in text
    assert "/Users/" not in text
    assert "navin" not in text.lower()
