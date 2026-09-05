from __future__ import annotations

import json
from pathlib import Path
import re

import pytest
import yaml

from tests.support.makefiles import (
    expand_make_value,
    read_combined_makefiles,
    read_make_variable_defaults,
)


pytestmark = pytest.mark.journey

ROOT = Path(__file__).resolve().parents[2]


def test_public_landing_surface_exists_and_is_generic() -> None:
    required_paths = [
        "README.md",
        "LICENSE",
        "SECURITY.md",
        "CONTRIBUTING.md",
        "CHANGELOG.md",
        "ROADMAP.md",
        "glama.json",
        ".github/ISSUE_TEMPLATE/bug_report.md",
        ".github/ISSUE_TEMPLATE/config_help.md",
        ".github/ISSUE_TEMPLATE/upstream_compatibility.md",
        ".github/pull_request_template.md",
        "docs/context-reduction-measurement.md",
        "docs/branding.md",
        "docs/add-profile.md",
        "docs/comparison.md",
        "docs/adoption-guide.md",
        "docs/safety.md",
        "docs/distribution.md",
        "docs/directory-submission-packet.md",
        "docs/github-publication.md",
        "docs/launch.md",
        "docs/community-launch.md",
        "docs/public-readiness.md",
        ".well-known/mcp/server-card.json",
        "registry/server.json",
        "registry/server.template.json",
        "Dockerfile",
        ".dockerignore",
        "docker/docker-entrypoint.sh",
        "mcpb/manifest.json",
    ]
    missing = [path for path in required_paths if not (ROOT / path).is_file()]

    assert missing == []


def test_mcpb_manifest_tool_names_are_client_safe() -> None:
    manifest = json.loads((ROOT / "mcpb" / "manifest.json").read_text(encoding="utf-8"))
    tool_names = [tool["name"] for tool in manifest["tools"]]
    client_safe_pattern = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

    assert tool_names == [
        "broker_search_tools",
        "broker_describe_tool",
        "broker_call_tool",
        "broker_status",
        "broker_close_session",
    ]
    assert [name for name in tool_names if not client_safe_pattern.fullmatch(name)] == []


def test_glama_claim_metadata_is_public_safe() -> None:
    make_vars = read_make_variable_defaults(ROOT)
    claim_path = ROOT / "glama.json"
    allowlist_path = ROOT / "public-export" / "allowlist.txt"

    claim = json.loads(claim_path.read_text(encoding="utf-8"))
    serialized = json.dumps(claim, sort_keys=True)

    assert claim == {
        "$schema": expand_make_value(make_vars, make_vars["GLAMA_SCHEMA_URL"]),
        "maintainers": [expand_make_value(make_vars, make_vars["GLAMA_MAINTAINER"])],
    }
    if allowlist_path.exists():
        assert "glama.json" in allowlist_path.read_text(encoding="utf-8")
    assert "/Users/" not in serialized
    assert "CloudStorage" not in serialized


def test_packaged_chat_profiles_use_client_safe_broker_tool_names() -> None:
    config = yaml.safe_load((ROOT / "config" / "broker.example.yaml").read_text(encoding="utf-8"))

    assert {
        profile_name: config["profiles"][profile_name]["broker_tool_name_style"]
        for profile_name in ("codex", "claude", "agy", "docker")
    } == {
        "codex": "snake",
        "claude": "snake",
        "agy": "snake",
        "docker": "snake",
    }


def test_readme_public_first_screen_has_adoption_content() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    required_terms = [
        "brand/assets/readme-header.svg",
        "one local MCP entry",
        "609",
        "43",
        "92.94%",
        "276,989",
        "45,281",
        "83.65%",
        "## Quickstart",
        "## Architecture",
        "## Comparison",
        "## Screenshots Or GIF",
        "docs/assets/quickstart-terminal.svg",
        "SECURITY.md",
        "CONTRIBUTING.md",
    ]
    forbidden_terms = [
        "Claude config rendering exists, but Claude should not be wired",
        "/Users/",
        "$HOME/Projects",
        "CloudStorage",
    ]

    assert [term for term in required_terms if term not in readme] == []
    assert [term for term in forbidden_terms if term in readme] == []


def test_branding_rules_document_locked_assets_and_enforcement() -> None:
    branding = (ROOT / "docs" / "branding.md").read_text(encoding="utf-8")
    brand_readme = (ROOT / "brand" / "README.md").read_text(encoding="utf-8")
    asset_usage = (ROOT / "brand" / "assets" / "USAGE.md").read_text(encoding="utf-8")
    token_css = (ROOT / "brand" / "assets" / "tokens.css").read_text(encoding="utf-8")
    token_json = json.loads((ROOT / "brand" / "assets" / "tokens.json").read_text(encoding="utf-8"))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    allowlist_path = ROOT / "public-export" / "allowlist.txt"
    allowlist = allowlist_path.read_text(encoding="utf-8") if allowlist_path.exists() else ""

    required_assets = [
        "brand/assets/readme-header.svg",
        "brand/assets/github-social-preview.svg",
        "brand/assets/app-icon-1024.png",
        "brand/assets/favicon-32.png",
        "brand/assets/mark.svg",
        "brand/assets/mark-favicon.svg",
        "brand/assets/horizontal.svg",
        "brand/assets/tokens.css",
        "brand/assets/tokens.json",
    ]
    required_rule_ids = [
        "BRAND-1",
        "BRAND-2",
        "BRAND-3",
        "BRAND-4",
        "BRAND-5",
        "BRAND-6",
        "BRAND-7",
    ]
    required_enforcement_terms = [
        "A brand rule with no named gate is a comment, not a standard.",
        "make test-journey",
        "make public-export-check",
        "make release-check",
        "tests/journey/test_public_adoption_contract.py",
        "public-export/allowlist.txt",
        "no alternate concept folders",
        "no generated-set folder names",
        "no \"manual review\" enforcement",
        "single source of truth",
    ]
    required_tokens = {
        "ink": "#0D1117",
        "midnight": "#111827",
        "slate": "#334155",
        "steel": "#64748B",
        "fog": "#F6F7F9",
        "primary": "#0EA5A0",
        "secondary": "#2563EB",
        "accent": "#F59E0B",
    }
    forbidden_brand_path_terms = [
        "set-",
        "production-vector",
        "generated",
        "concept",
        "downloads",
    ]

    assert [asset for asset in required_assets if asset not in branding] == []
    assert [asset for asset in required_assets if not (ROOT / asset).is_file()] == []
    assert [rule for rule in required_rule_ids if rule not in branding] == []
    assert [term for term in required_enforcement_terms if term not in branding] == []
    assert [
        f"brand/{path.name}"
        for path in (ROOT / "brand").iterdir()
        if path.is_dir() and path.name != "assets"
    ] == []
    assert [
        str(path.relative_to(ROOT))
        for path in (ROOT / "brand").rglob("*")
        if any(term in str(path.relative_to(ROOT)).lower() for term in forbidden_brand_path_terms)
    ] == []
    assert token_json["colors"] == required_tokens
    assert [
        token
        for token, hex_value in required_tokens.items()
        if f"--mcp-{token}: {hex_value};" not in token_css or hex_value not in branding
    ] == []
    assert "routing backbone" in branding
    assert "one ingress. many servers. safe by profile." in branding
    assert "/Users/" not in branding
    assert "CloudStorage" not in branding
    assert "brand/assets/readme-header.svg" in readme
    if allowlist_path.exists():
        assert "docs/branding.md" in allowlist
        assert "brand/**" in allowlist
    else:
        assert (ROOT / "brand").is_dir()
    assert "Do not reintroduce alternate concept folders into the public repo." in brand_readme
    assert "Routing backbone - MCP Broker brand direction" in asset_usage


def test_public_metadata_docs_are_ready_for_first_release() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    roadmap = (ROOT / "ROADMAP.md").read_text(encoding="utf-8")
    readiness = (ROOT / "docs" / "public-readiness.md").read_text(encoding="utf-8")

    release_headings = re.findall(
        r"^## (?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*) - ",
        changelog,
        re.M,
    )
    assert len(release_headings) >= 2
    assert "context-reduction-measurement.md" in changelog
    assert "Private-To-Public Export" in roadmap
    assert "GitHub topics" in readiness
    assert "mcp, model-context-protocol, codex, claude" in readiness
    assert "/Users/" not in changelog
    assert "/Users/" not in roadmap
    assert "/Users/" not in readiness


def test_public_safety_docs_cover_broker_mediated_risks() -> None:
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    required_security_terms = [
        "mutating tools",
        "OAuth state",
        "browser state",
        "filesystem roots",
        "database URLs",
        "per-profile allowlists",
        "config/broker.private.yaml",
    ]
    required_contributing_terms = [
        "Public repo first",
        "make quality-gate",
        "config/broker.example.yaml",
        "Do not commit local MCP inventory",
    ]

    assert [term for term in required_security_terms if term not in security] == []
    assert [term for term in required_contributing_terms if term not in contributing] == []


def test_public_adoption_guides_cover_comparison_adoption_and_safety() -> None:
    comparison = (ROOT / "docs" / "comparison.md").read_text(encoding="utf-8")
    adoption = (ROOT / "docs" / "adoption-guide.md").read_text(encoding="utf-8")
    add_profile = (ROOT / "docs" / "add-profile.md").read_text(encoding="utf-8")
    safety = (ROOT / "docs" / "safety.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    required_comparison_terms = [
        "Docker MCP Gateway",
        "IBM ContextForge",
        "Microsoft MCP Gateway",
        "Smithery",
        "Glama Gateway",
        "simple MCP proxies",
    ]
    required_adoption_terms = [
        "Codex",
        "Claude",
        "Cursor",
        "too many MCP tools loaded",
        "compact broker facade",
    ]
    required_add_profile_terms = [
        "make profile-snippet",
        "NEW_PROFILE",
        "NEW_CLIENT_FORMAT",
        "mcp-settings-json",
        "make profile-validation PROFILE=",
        "make facade-smoke PROFILE=",
    ]
    required_safety_terms = [
        "mutating tools",
        "OAuth state",
        "browser state",
        "filesystem roots",
        "database URLs",
        "per-profile allowlists",
    ]

    assert [term for term in required_comparison_terms if term not in comparison] == []
    assert [term for term in required_adoption_terms if term not in adoption] == []
    assert [term for term in required_add_profile_terms if term not in add_profile] == []
    assert [term for term in required_safety_terms if term not in safety] == []
    assert "docs/comparison.md" in readme
    assert "docs/add-profile.md" in readme
    assert "docs/adoption-guide.md" in readme
    assert "docs/safety.md" in readme


def test_clone_to_running_path_is_documented_as_one_generic_flow() -> None:
    make_vars = read_make_variable_defaults(ROOT)
    repository_url = expand_make_value(make_vars, make_vars["GITHUB_REPOSITORY_URL"])
    adoption = (ROOT / "docs" / "adoption-guide.md").read_text(encoding="utf-8")
    install = (ROOT / "docs" / "install.md").read_text(encoding="utf-8")
    troubleshooting = (ROOT / "docs" / "troubleshooting.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    ordered_commands = [
        'git clone "$GITHUB_REPOSITORY_URL" mcp-broker',
        "cd mcp-broker",
        "make setup",
        "make config-init",
        "make config-validate",
        "make broker-smoke",
        "make profile-validation PROFILE=codex",
        "make config-backup CLIENT=codex",
        "make config-render CLIENT=codex CONFIG_RENDER_APPLY=0",
        "make broker-status",
        "make config-render CLIENT=codex CONFIG_RENDER_APPLY=1",
        "make config-rollback CLIENT=codex",
    ]
    required_terms = [
        "## Clone-To-Running Path",
        "Add one upstream",
        "No client config is written before `CONFIG_RENDER_APPLY=1`",
        "Keep local paths, account names, and secrets out of git",
        *ordered_commands,
    ]
    forbidden_terms = [
        "/Users/",
        "CloudStorage",
        "broker.private.yaml.bak",
        "navin@",
        "ms365-",
        repository_url,
    ]

    assert [term for term in required_terms if term not in adoption] == []
    assert [term for term in forbidden_terms if term in adoption] == []
    positions = [adoption.index(command) for command in ordered_commands]
    assert positions == sorted(positions)
    assert "docs/adoption-guide.md#clone-to-running-path" in readme
    assert "Clone-To-Running Path" in install
    assert "Clone-To-Running Path" in troubleshooting


def test_public_distribution_docs_cover_package_registry_and_directory_paths() -> None:
    make_vars = read_make_variable_defaults(ROOT)
    repository_url = expand_make_value(make_vars, make_vars["GITHUB_REPOSITORY_URL"])
    install = (ROOT / "docs" / "install.md").read_text(encoding="utf-8")
    distribution = (ROOT / "docs" / "distribution.md").read_text(encoding="utf-8")
    github_publication = (ROOT / "docs" / "github-publication.md").read_text(encoding="utf-8")
    community_launch = (ROOT / "docs" / "community-launch.md").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    required_install_terms = [
        "pipx install mcp-broker",
        "uv tool install mcp-broker",
        "brew install mcp-broker",
        "docker run",
        "mcp-broker init",
        "mcp-broker start",
        "mcp-broker status",
        "mcp-broker render codex --dry-run",
    ]
    required_distribution_terms = [
        "MCP Registry",
        "mcp-publisher",
        "server.json",
        "Docker MCP Toolkit",
        "mcpb/manifest.json",
        "Smithery",
        "Glama",
        "${GLAMA_LISTING_URL}",
        "PulseMCP",
        "PulseMCP has also appeared from the registry/server.json surface",
        "${PULSEMCP_LISTING_URL}",
        "Homebrew",
        "make release-gate",
        "var/quality/mutation_stats.json",
    ]
    required_publication_terms = [
        "repository description",
        "model-context-protocol",
        "pinned demo issue",
        "release notes",
    ]
    required_community_terms = [
        "GitHub Discussions",
        "Hacker News",
        "r/mcp",
        "r/LocalLLaMA",
        "feedback labels",
    ]

    assert [term for term in required_install_terms if term not in install] == []
    assert [term for term in required_distribution_terms if term not in distribution] == []
    assert [term for term in required_publication_terms if term not in github_publication] == []
    assert [term for term in required_community_terms if term not in community_launch] == []
    assert 'mcp-broker = "mcp_broker.cli:main"' in pyproject
    assert '"share/mcp-broker/config" = ["config/broker.example.yaml"]' in pyproject


def test_docker_packaging_contract_is_public_safe() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    entrypoint = (ROOT / "docker" / "docker-entrypoint.sh").read_text(encoding="utf-8")
    public_config = yaml.safe_load((ROOT / "config" / "broker.example.yaml").read_text(encoding="utf-8"))
    makefile = read_combined_makefiles(ROOT)
    allowlist_path = ROOT / "public-export" / "allowlist.txt"
    allowlist = allowlist_path.read_text(encoding="utf-8") if allowlist_path.exists() else ""

    assert "ARG PYTHON_IMAGE=python:3-slim" in dockerfile
    assert "COPY src /app/src" in dockerfile
    assert "COPY npm/package.json /app/npm/package.json" in dockerfile
    assert "COPY config/broker.example.yaml /app/config/broker.example.yaml" in dockerfile
    assert "ENTRYPOINT [\"/usr/local/bin/mcp-broker-docker\"]" in dockerfile
    assert "MCP_BROKER_RUNTIME_ROOT=/var/lib/mcp-broker" in dockerfile
    assert "PIP_ROOT_USER_ACTION=ignore python -m pip install" in dockerfile
    assert "mcp-broker stdio" in entrypoint
    assert "--init-if-missing" in entrypoint
    assert "MCP_BROKER_CONFIG" in entrypoint
    assert "MCP_BROKER_SOCKET" in entrypoint
    assert "${MCP_BROKER_PROFILE:-docker}" in entrypoint
    assert "docker" in public_config["profiles"]
    assert public_config["profiles"]["docker"]["compact_tools_enabled"] is True
    assert "docker-build:" in makefile
    assert "docker-smoke:" in makefile
    if allowlist:
        assert "Dockerfile" in allowlist
        assert ".dockerignore" in allowlist
        assert "docker/docker-entrypoint.sh" in allowlist
        assert "scripts/mcpb_stdio_smoke.py" in allowlist
    assert "/Users/" not in dockerfile
    assert "/Users/" not in entrypoint


def test_mcpb_manifest_contract_is_public_safe() -> None:
    make_vars = read_make_variable_defaults(ROOT)
    manifest_path = ROOT / "mcpb" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["name"] == "mcp-broker"
    assert manifest["license"] == "MIT"
    assert manifest["author"]["name"] == make_vars["PACKAGE_AUTHOR"]
    assert manifest["server"]["type"] == "binary"
    assert manifest["server"]["mcp_config"]["command"] == "${user_config.uvx_path}"
    assert manifest["server"]["mcp_config"]["args"][:2] == ["mcp-broker", "stdio"]
    assert manifest["user_config"]["uvx_path"]["default"] == "uvx"
    assert manifest["user_config"]["uvx_path"]["required"] is True
    assert "broker_call_tool" in {tool["name"] for tool in manifest["tools"]}
    assert "broker_status" in {tool["name"] for tool in manifest["tools"]}

    serialized = json.dumps(manifest, sort_keys=True)
    assert "/Users/" not in serialized
    assert "config/broker.private.yaml" not in serialized


def test_mcpb_manifest_tools_match_rich_compact_broker_descriptions() -> None:
    from mcp_broker.tool_namespace import compact_broker_tool_definitions

    manifest = json.loads((ROOT / "mcpb" / "manifest.json").read_text(encoding="utf-8"))
    live_tools = {
        tool["name"]: tool["description"]
        for tool in compact_broker_tool_definitions(broker_tool_name_style="snake")
    }

    manifest_tools = {
        tool["name"]: tool["description"]
        for tool in manifest["tools"]
    }

    assert manifest_tools == live_tools
    assert all("inputSchema" not in tool for tool in manifest["tools"])


def test_registry_template_is_public_safe_and_points_to_pypi_package() -> None:
    template = json.loads((ROOT / "registry" / "server.template.json").read_text(encoding="utf-8"))
    raw = (ROOT / "registry" / "server.template.json").read_text(encoding="utf-8")

    assert template["$schema"] == "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"
    assert template["name"] == "io.github.example/mcp-broker"
    assert template["packages"][0]["registryType"] == "pypi"
    assert template["packages"][0]["identifier"] == "mcp-broker"
    assert template["packages"][0]["transport"]["type"] == "stdio"
    assert "/Users/" not in raw
    assert "CloudStorage" not in raw


def test_registry_metadata_and_server_card_are_public_ready() -> None:
    import json

    server = json.loads((ROOT / "registry" / "server.json").read_text(encoding="utf-8"))
    template = json.loads((ROOT / "registry" / "server.template.json").read_text(encoding="utf-8"))
    card = json.loads((ROOT / ".well-known" / "mcp" / "server-card.json").read_text(encoding="utf-8"))
    distribution = (ROOT / "docs" / "distribution.md").read_text(encoding="utf-8")
    packet = (ROOT / "docs" / "directory-submission-packet.md").read_text(encoding="utf-8")
    launch = (ROOT / "docs" / "launch.md").read_text(encoding="utf-8")

    assert server["$schema"] == "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"
    assert server["name"].startswith("io.github.")
    assert server["packages"][0]["registryType"] == "pypi"
    assert server["packages"][0]["identifier"] == "mcp-broker"
    assert server["packages"][0]["transport"]["type"] == "stdio"
    assert template["name"] == "io.github.example/mcp-broker"
    assert card["name"] == server["name"]
    assert card["packages"][0]["identifier"] == "mcp-broker"
    assert "GitHub OIDC" in distribution
    assert "PyPI package must exist first" in distribution
    assert "MCPSERVERS" in packet
    assert "MCP_SO" in packet
    assert "MCPCentral" in packet
    assert '"mcpServers": {' in packet
    assert '"mcp-broker": {' in packet
    assert '"command": "${user_config.uvx_path}"' in packet
    assert "UVX command path" in packet
    assert '"mcp-broker", "stdio", "--profile", "docker", "--init-if-missing"' in packet
    assert "609 to 43" in launch
    assert "276,989 to 45,281" in launch
    assert "/Users/" not in json.dumps(server)
    assert "/Users/" not in json.dumps(card)
    assert "CloudStorage" not in packet


def test_directory_submission_check_is_make_backed() -> None:
    make_vars = read_make_variable_defaults(ROOT)
    makefile = read_combined_makefiles(ROOT)
    distribution = (ROOT / "docs" / "distribution.md").read_text(encoding="utf-8")
    packet = (ROOT / "docs" / "directory-submission-packet.md").read_text(encoding="utf-8")
    script = ROOT / "scripts" / "check_directory_submission.py"
    script_text = script.read_text(encoding="utf-8") if script.exists() else ""

    assert script.is_file()
    assert "directory-submission-check:" in makefile
    assert "$(PYTHON_BIN) \"$(ROOT)/scripts/check_directory_submission.py\"" in makefile
    assert "make directory-submission-check" in distribution
    assert "/Users/" not in script_text
    for env_name in [
        "PACKAGE_SLUG",
        "PACKAGE_COMMAND_NAME",
        "PYPI_PROJECT_NAME",
        "GLAMA_SCHEMA_URL",
    ]:
        assert f'{env_name}="$({env_name})"' in makefile
        assert f'_required_env("{env_name}")' in script_text
    for env_name in [
        "GITHUB_REPOSITORY_URL",
        "GLAMA_LISTING_URL",
        "PULSEMCP_LISTING_URL",
        "PULSEMCP_SUBMIT_URL",
        "MCPSERVERS_LISTING_URL",
        "MCP_SO_LISTING_URL",
        "MCPCENTRAL_REGISTRY_URL",
    ]:
        assert f'{env_name}="$({env_name})"' in makefile
        assert f'_placeholder_env("{env_name}")' in script_text
    for copied_term in [
        '"mcp-broker"',
        '"pipx install mcp-broker"',
        '"mcp-broker init"',
        '"mcp-broker render codex --dry-run"',
        '"MCPSERVERS"',
        '"MCP_SO"',
    ]:
        assert copied_term not in script_text
    for term in [
        "broker_search_tools",
        "broker_describe_tool",
        "broker_call_tool",
        "broker_status",
        "${GITHUB_REPOSITORY_URL}",
        "docs/context-reduction-measurement.md",
        "${GLAMA_LISTING_URL}",
        "glama.json",
        "${PULSEMCP_SUBMIT_URL}",
        "${PULSEMCP_LISTING_URL}",
        "PulseMCP: listed at",
        "MCPSERVERS: approved",
        "${MCPSERVERS_LISTING_URL}",
        "MCP_SO: live",
        "${MCP_SO_LISTING_URL}",
        "`${MCPCENTRAL_REGISTRY_URL}` currently does not resolve",
        "blocks non-browser automation",
        "mcp-publisher login github --registry ${MCPCENTRAL_REGISTRY_URL}",
        "${PUNKPEYE_AWESOME_PR_URL}",
        "${APPCYPHER_AWESOME_COMPARE_URL}",
        "Server tab",
        "Connector tab",
        "Settings -> Extensions -> Advanced settings -> Extension Developer -> Install Extension",
        "make smithery-payload-check",
        "make smithery-publish",
        "make mcpb-stdio-smoke",
        "scripts/mcpb_stdio_smoke.py",
        "scripts/smithery_release.py",
        "server.mcpb",
    ]:
        assert term in packet
