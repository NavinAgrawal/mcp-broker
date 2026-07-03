import re
import subprocess
from pathlib import Path

import pytest

from tests.support.makefiles import read_combined_makefiles
from tests.support.bundles import write_signed_bundle
from tests.support.repo_paths import make_command

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]


def test_make_help_exposes_broker_entrypoints() -> None:
    result = subprocess.run(
        ["make", "help"],
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    for target in [
        "setup",
        "test",
        "test-unit",
        "test-journey",
        "test-live",
        "test-e2e",
        "broker-start",
        "broker-stop",
        "broker-status",
        "broker-wait",
        "broker-reap",
        "broker-smoke",
        "tools-count",
        "facade-smoke",
        "codex-facade-smoke",
        "claude-facade-smoke",
        "agy-facade-smoke",
        "discovery-parity",
        "codex-claude-discovery-parity",
        "launchagent-install",
        "service-plan",
        "launchagent-load",
        "launchagent-uninstall",
        "launchagent-unload",
        "systemd-install",
        "systemd-load",
        "systemd-uninstall",
        "systemd-unload",
        "windows-install",
        "windows-load",
        "windows-uninstall",
        "windows-unload",
        "linux-container-smoke",
        "linux-release-gate",
        "windows-powershell-smoke",
        "release-smoke",
        "package-install-smoke",
        "docker-build",
        "docker-smoke",
        "docker-buildx",
        "docker-mcp-catalog-smoke",
        "directory-submission-check",
        "mcpb-pack",
        "mcpb-stdio-smoke",
        "mcpb-smoke",
        "mcpb-validate",
        "smithery-payload-check",
        "smithery-publish",
        "config-init",
        "bundle-validate",
        "deployment-stage",
        "deployment-rollback",
        "deployment-recover",
        "governance-pull",
        "governance-apply",
        "governance-rollback",
        "fleet-status-collect",
        "plugin-install",
        "plugin-status",
        "plugin-render",
        "plugin-apply",
        "plugin-rollback",
        "plugin-bootstrap-preflight",
        "plugin-bootstrap-plan",
        "plugin-bootstrap-apply",
        "plugin-bootstrap-status",
        "plugin-bootstrap-rollback",
        "plugin-bootstrap-uninstall",
        "config-backup",
        "codex-app-policy",
        "config-render",
        "config-rollback",
        "profile-snippet",
        "mutation",
        "mutation-linux",
        "precommit",
        "quality-gate",
        "release-check",
        "release-gate",
        "release",
    ]:
        assert target in result.stdout

    for maintainer_only_target in [
        "codex-deferred-acceptance",
        "public-export",
        "public-export-check",
        "public-release-dry-run",
        "violations",
        "grade-quality",
        "maintainer-violations",
        "maintainer-grade-quality",
        "maintainer-precommit",
        "maintainer-quality-gate",
        "maintainer-release-gate",
    ]:
        assert maintainer_only_target not in result.stdout


def test_makefile_is_split_by_command_family() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    expected_includes = [
        "include $(ROOT)/mk/config.mk",
        "include $(ROOT)/mk/logging.mk",
        "include $(ROOT)/mk/bootstrap.mk",
        "include $(ROOT)/mk/tests.mk",
        "include $(ROOT)/mk/runtime.mk",
        "include $(ROOT)/mk/plugin.mk",
        "include $(ROOT)/mk/distribution.mk",
        "include $(ROOT)/mk/maintainer.mk",
        "include $(ROOT)/mk/release.mk",
    ]

    for include in expected_includes:
        assert include in makefile

    include_positions = [makefile.index(include) for include in expected_includes]
    assert include_positions == sorted(include_positions)
    assert "-include $(ROOT)/local.mk" in makefile
    assert makefile.index("include $(ROOT)/mk/release.mk") < makefile.index("-include $(ROOT)/local.mk")
    assert len(makefile.splitlines()) < 80

    for path in [
        ROOT / "mk" / "config.mk",
        ROOT / "mk" / "logging.mk",
        ROOT / "mk" / "bootstrap.mk",
        ROOT / "mk" / "tests.mk",
        ROOT / "mk" / "runtime.mk",
        ROOT / "mk" / "plugin.mk",
        ROOT / "mk" / "distribution.mk",
        ROOT / "mk" / "maintainer.mk",
        ROOT / "mk" / "release.mk",
    ]:
        assert path.exists()


def test_make_profile_snippet_keeps_home_placeholder_public_safe() -> None:
    result = subprocess.run(
        make_command(
            "profile-snippet",
            "NEW_PROFILE=sample-client",
            "NEW_CLIENT_FORMAT=mcp-settings-json",
        ),
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert "config_path: $HOME/.sample-client/settings.json" in result.stdout
    assert "mcp_allowed_servers:" in result.stdout
    assert "      - mcp-broker" in result.stdout
    assert "/Users/" not in result.stdout
    assert "make config-render CLIENT=sample-client CONFIG_RENDER_APPLY=0" in result.stdout


def test_make_bundle_validate_validates_local_bundle_without_runtime_writes(tmp_path: Path) -> None:
    bundle_path = tmp_path / "bundle.json"
    write_signed_bundle(bundle_path)

    result = subprocess.run(
        make_command("bundle-validate", f"BUNDLE={bundle_path}", f"RUNTIME_ROOT={tmp_path / 'runtime'}"),
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert "bundle validated:" in result.stdout
    assert str(bundle_path) in result.stdout
    assert not (tmp_path / "runtime").exists()


def test_mutation_target_uses_venv_console_script() -> None:
    makefile = read_combined_makefiles(ROOT)
    setup_cfg = (ROOT / "setup.cfg").read_text(encoding="utf-8")

    mutation_section = makefile.split("mutation: $(VENV_DIR)/.deps.stamp", maxsplit=1)[1].split(
        "release-gate:",
        maxsplit=1,
    )[0]
    assert "$(MUTMUT) run" in mutation_section
    assert "python -m mutmut" not in mutation_section
    assert 'MCP_BROKER_REPO_ROOT="$(ROOT)"' not in mutation_section
    assert 'PYTHONPATH="$(PYTHONPATH)"' not in mutation_section
    assert "scripts/check_mutation_stats.py" in mutation_section
    assert '$(if $(MUTATION_ARGS),--include-mutants $(MUTATION_ARGS),)' in mutation_section
    assert "CPU_COUNT ?=" in makefile
    assert "LOCAL_CPU_BUDGET ?= $(if $(filter Darwin,$(UNAME_S)),2,4)" in makefile
    assert "MUTATION_QOS_PREFIX ?= $(if $(filter Darwin,$(UNAME_S)),taskpolicy -b,)" in makefile
    assert "@$(MUTATION_QOS_PREFIX) $(MUTMUT) run" in mutation_section
    mutation_children_default = re.search(r"^MUTATION_MAX_CHILDREN \?= (.+)$", makefile, re.M)
    assert mutation_children_default is not None
    assert mutation_children_default.group(1) == "$(if $(filter Darwin,$(UNAME_S)),1,$(LOCAL_CPU_BUDGET))"
    release_mutation_children_default = re.search(
        r"^MUTATION_RELEASE_CHILDREN \?= (.+)$", makefile, re.M
    )
    assert release_mutation_children_default is not None
    assert "$(if $(filter Darwin,$(UNAME_S)),1," in release_mutation_children_default.group(1)
    assert 'int("$(LOCAL_CPU_BUDGET)") // int("$(RELEASE_GATE_JOBS)")' in (
        release_mutation_children_default.group(1)
    )
    assert 'MUTATION_MAX_CHILDREN="$(MUTATION_RELEASE_CHILDREN)" $(RELEASE_MUTATION_TARGET)' in (
        makefile
    )
    assert "mutation-linux:" in makefile
    assert "scripts/linux-mutation.sh" in makefile
    assert "MUTATION_LOG ?= $(QUALITY_DIR)/mutation-linux.log" in makefile
    assert "MUTATION_MUTANTS_DIR ?= $(QUALITY_DIR)/mutants-linux" in makefile
    assert 'MCP_BROKER_MUTATION_LOG="$(MUTATION_LOG)"' in makefile
    assert 'MCP_BROKER_MUTATION_MUTANTS_DIR="$(MUTATION_MUTANTS_DIR)"' in makefile
    assert "RELEASE_MUTATION_TARGET ?= $(if $(filter Darwin,$(UNAME_S)),mutation-linux,mutation)" in makefile
    assert 'MUTATION_STATS_JSON ?= $(QUALITY_DIR)/mutation_stats.json' in makefile
    assert (
        "MUTATION_FAIL_STATUSES ?= survived no_tests skipped suspicious timeout "
        "check_was_interrupted_by_user segfault not_checked"
    ) in makefile
    assert "RELEASE_GATE_JOBS ?= $(if $(filter Darwin,$(UNAME_S)),1,2)" in makefile
    assert "RELEASE_GATE_PARALLEL ?= $(if $(filter Darwin,$(UNAME_S)),0,1)" in makefile
    assert "release-gate: ## Run release gates with resource-bounded mutation" in makefile
    assert '$(call timed_make,"release-gate: deps",deps)' in makefile
    assert "doctor: deps runtime-layout broker-reap ## Verify runtime directories and report broker-owned leftovers" in makefile
    assert "broker-reap: deps runtime-layout ## Reap stale broker-owned pidfiles, sockets, and orphaned process groups" in makefile
    assert '$(call timed_make,"release-gate: parallel children",-j $(RELEASE_GATE_JOBS) _release-gate-quality _release-gate-package _release-gate-smoke _release-gate-mutation)' in makefile
    assert '$(call timed_make,"release-gate: sequential quality-gate",_release-gate-quality)' in makefile
    assert '$(call timed_make,"release-gate: sequential package-check",_release-gate-package)' in makefile
    assert '$(call timed_make,"release-gate: sequential release-smoke",_release-gate-smoke)' in makefile
    assert '$(call timed_make,"release-gate: sequential mutation",_release-gate-mutation)' in makefile
    assert "RELEASE_GATE_LOG_DIR ?= $(QUALITY_DIR)/release-gate" in makefile
    assert '"$(RELEASE_GATE_LOG_DIR)/quality-gate.log"' in makefile
    assert '"$(RELEASE_GATE_LOG_DIR)/package-check.log"' in makefile
    assert '"$(RELEASE_GATE_LOG_DIR)/release-smoke.log"' in makefile
    assert '"$(RELEASE_GATE_LOG_DIR)/$(RELEASE_MUTATION_TARGET).log"' in makefile
    assert 'tail -n 80 "$$log" >&2' in makefile
    assert "paths_to_mutate=src/mcp_broker" in setup_cfg
    also_copy = setup_cfg.split("also_copy=\n", maxsplit=1)[1].split("tests_dir=", maxsplit=1)[0]
    copied_paths = {line.strip() for line in also_copy.splitlines() if line.strip()}
    assert {
        "brand",
        "config",
        "docs",
        "docker",
        "mk",
        "mcpb",
        "npm",
        "public-export",
        "registry",
        "scripts",
        ".github",
        ".well-known",
        ".dockerignore",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "glama.json",
        "LICENSE",
        "ROADMAP.md",
        "SECURITY.md",
        "Dockerfile",
        "Makefile",
        "pyproject.toml",
        "pytest.ini",
        "README.md",
        "requirements.txt",
    } <= copied_paths
    assert "    AGENTS.md" not in setup_cfg
    assert "    TODO.md" not in setup_cfg
    assert "tests_dir=\n    tests/unit\n    tests/journey" in setup_cfg
    assert "pytest_add_cli_args=\n    --timeout=30\n    -m\n    not private_contract" in setup_cfg
    assert "mutate_only_covered_lines=true" in setup_cfg

def test_make_test_gates_use_parallel_workers_and_fanout() -> None:
    makefile = read_combined_makefiles(ROOT)

    assert "CPU_COUNT ?=" in makefile
    assert "LOCAL_CPU_BUDGET ?= $(if $(filter Darwin,$(UNAME_S)),2,4)" in makefile
    assert "PYTEST_WORKERS ?= $(LOCAL_CPU_BUDGET)" in makefile
    assert "PYTEST_TARGETED_WORKERS ?= 0" in makefile
    assert "PYTEST_FANOUT_WORKERS ?=" in makefile
    assert "PYTEST_PRECOMMIT_WORKERS ?=" in makefile
    assert "PYTEST_RELEASE_WORKERS ?=" in makefile
    assert "PUBLIC_RELEASE_PYTEST_MARKER_EXPRESSION ?= not private_contract" in makefile
    assert (
        "PYTEST_MARKER_EXPRESSION ?= "
        "$(if $(wildcard $(ROOT)/local.mk),,$(PUBLIC_RELEASE_PYTEST_MARKER_EXPRESSION))"
    ) in makefile
    assert "QUALITY_GATE_PYTEST_MARKER_EXPRESSION ?= $(RELEASE_GATE_PYTEST_MARKER_EXPRESSION)" in makefile
    assert 'PYTEST_MARKER_ARGS ?= $(if $(strip $(PYTEST_MARKER_EXPRESSION)),-m "$(PYTEST_MARKER_EXPRESSION)",)' in makefile
    assert 'PYTEST_COV_MARKER_ARGS ?= $(if $(strip $(QUALITY_GATE_PYTEST_MARKER_EXPRESSION)),-m "$(QUALITY_GATE_PYTEST_MARKER_EXPRESSION)",)' in makefile
    assert "$(PYTEST_MARKER_ARGS) $(PYTEST_XDIST_ARGS)" in makefile
    assert "$(PYTEST_MARKER_ARGS) $(PYTEST_TARGETED_XDIST_ARGS)" in makefile
    assert "$(PYTEST_COV_MARKER_ARGS) $(PYTEST_XDIST_ARGS)" in makefile
    assert 'int("$(LOCAL_CPU_BUDGET)") // int("$(TEST_JOBS)")' in makefile
    assert 'int("$(LOCAL_CPU_BUDGET)") // int("$(PRECOMMIT_JOBS)")' in makefile
    assert 'int("$(LOCAL_CPU_BUDGET)") // int("$(RELEASE_GATE_JOBS)")' in makefile
    assert "PYTEST_XDIST_DIST ?= loadfile" in makefile
    assert (
        "PYTEST_XDIST_ARGS ?= $(if $(filter 0,$(PYTEST_WORKERS)),"
        ",-n $(PYTEST_WORKERS) --dist $(PYTEST_XDIST_DIST))"
    ) in makefile
    assert (
        "PYTEST_TARGETED_XDIST_ARGS ?= $(if $(filter 0,$(PYTEST_TARGETED_WORKERS)),"
        ",-n $(PYTEST_TARGETED_WORKERS) --dist $(PYTEST_XDIST_DIST))"
    ) in makefile
    assert "$(PYTEST_XDIST_ARGS)" in makefile
    assert "$(PYTEST_TARGETED_XDIST_ARGS)" in makefile
    assert "PYTEST_TARGETED_COMMON ?=" in makefile
    assert "TEST_JOBS ?= 4" in makefile
    assert "PRECOMMIT_JOBS ?= 2" in makefile
    assert "RELEASE_GATE_JOBS ?= $(if $(filter Darwin,$(UNAME_S)),1,2)" in makefile
    assert "RELEASE_GATE_PARALLEL ?= $(if $(filter Darwin,$(UNAME_S)),0,1)" in makefile
    mutation_children_default = re.search(r"^MUTATION_MAX_CHILDREN \?= (.+)$", makefile, re.M)
    assert mutation_children_default is not None
    assert mutation_children_default.group(1) == "$(if $(filter Darwin,$(UNAME_S)),1,$(LOCAL_CPU_BUDGET))"
    release_mutation_children_default = re.search(
        r"^MUTATION_RELEASE_CHILDREN \?= (.+)$", makefile, re.M
    )
    assert release_mutation_children_default is not None
    assert "$(if $(filter Darwin,$(UNAME_S)),1," in release_mutation_children_default.group(1)
    assert 'int("$(LOCAL_CPU_BUDGET)") // int("$(RELEASE_GATE_JOBS)")' in (
        release_mutation_children_default.group(1)
    )
    assert 'MUTATION_MAX_CHILDREN="$(MUTATION_RELEASE_CHILDREN)" $(RELEASE_MUTATION_TARGET)' in (
        makefile
    )
    assert "XDIST_BENCHMARK_TARGETS ?= $(PY_UNIT_DIR) $(PY_JOURNEY_DIR)" in makefile
    assert "xdist-benchmark:" in makefile
    assert "PYTEST_XDIST_DIST=load" in makefile
    assert "PYTEST_XDIST_DIST=loadfile" in makefile
    assert "TEST_RUNTIME_ROOT ?= $(ROOT)/var/test-runtime" in makefile

    test_section = makefile.split("test: ## Run all test tiers in parallel", maxsplit=1)[1].split(
        "_test-targeted:",
        maxsplit=1,
    )[0]
    precommit_section = makefile.split("precommit: ## Public commit gate", maxsplit=1)[1].split(
        "quality-gate:",
        maxsplit=1,
    )[0]

    assert 'ifneq ($(strip $(PYTEST_ARGS)),)' in test_section
    assert '$(call timed_make,"test: targeted tests",_test-targeted)' in test_section
    assert '$(call timed_make,"test: all tiers",-j $(TEST_JOBS) _test-unit-fanout _test-journey-fanout _test-live-fanout _test-e2e-fanout)' in test_section
    assert 'RUNTIME_ROOT="$(TEST_RUNTIME_ROOT)/unit"' in makefile
    assert 'RUNTIME_ROOT="$(TEST_RUNTIME_ROOT)/journey"' in makefile
    assert 'RUNTIME_ROOT="$(TEST_RUNTIME_ROOT)/live"' in makefile
    assert 'RUNTIME_ROOT="$(TEST_RUNTIME_ROOT)/e2e"' in makefile
    assert '$(call timed_make,"precommit: unit and journey",-j $(PRECOMMIT_JOBS) _precommit-unit-fanout _precommit-journey-fanout)' in precommit_section
    assert '$(call timed_make,"precommit: targeted tests",_test-targeted)' in precommit_section
    assert 'PYTEST_WORKERS="$(PYTEST_PRECOMMIT_WORKERS)"' in precommit_section


def test_make_parallel_gates_report_child_and_total_elapsed_time() -> None:
    makefile = read_combined_makefiles(ROOT)

    assert "define timed_make" in makefile
    assert "date +\"%Y-%m-%d %H:%M:%S %Z\"" in makefile
    assert "[TIME]" in makefile
    assert "start %s at %s" in makefile
    assert "elapsed_human=$$(printf" in makefile
    assert "end %s at %s elapsed=%s elapsed_seconds=%s status=%s" in makefile

    for label in [
        "test: total",
        "test: targeted tests",
        "test: all tiers",
        "test child: unit",
        "test child: journey",
        "test child: live",
        "test child: e2e",
        "precommit: total",
        "precommit: targeted tests",
        "precommit: unit and journey",
        "precommit child: unit",
        "precommit child: journey",
        "mutation: total",
        "mutation-linux: total",
        "release-check: total",
        "release-check: version",
        "release-check: publish preflight",
        "release-check: directory and bundle metadata",
        "release: total",
        "release: preflight",
        "release: publish",
        "release-gate: total",
        "release-gate: deps",
        "release-gate: parallel children",
        "release-gate: sequential quality-gate",
        "release-gate: sequential package-check",
        "release-gate: sequential release-smoke",
        "release-gate: sequential mutation",
        "publish-everywhere-check: total",
        "publish-everywhere-check: version check",
        "publish-everywhere-check: release gate",
        "publish-everywhere-check: package smoke children",
        "publish-everywhere: total",
        "publish-everywhere: preflight checks",
        "publish-everywhere: auth preflight",
        "publish-everywhere: pypi",
        "publish-everywhere: registry fanout",
        "publish-everywhere: live registry verification",
        "publish-everywhere: github release",
        "publish-everywhere: github release verification",
    ]:
        assert f'$(call timed_make,"{label}",' in makefile

    assert '$(call timed_make,"release-gate child: quality-gate",' in makefile
    assert '$(call timed_make,"release-gate child: package-check",' in makefile
    assert '$(call timed_make,"release-gate child: release-smoke",' in makefile
    assert '$(call timed_make,"release-gate child: $(RELEASE_MUTATION_TARGET)",' in makefile
    assert '$(call timed_make,"publish check child: docker-smoke",' in makefile
    assert '$(call timed_make,"publish check child: docker-buildx",' in makefile
    assert '$(call timed_make,"publish child: docker-publish-check",' in makefile


def test_release_smoke_copies_nonignored_git_files_not_live_cache_tree() -> None:
    script = (ROOT / "scripts" / "release-smoke.sh").read_text(encoding="utf-8")

    assert "git ls-files -co --exclude-standard -z" in script
    assert '--null \\' in script
    assert '-T "$SOURCE_LIST_PATH" \\' in script
    assert '-C "$ROOT" -cf - .' not in script


def test_hidden_maintainer_violations_target_is_public_safe() -> None:
    makefile = read_combined_makefiles(ROOT)

    assert "maintainer-violations:" in makefile
    assert "maintainer-grade-quality:" in makefile
    assert "require-violations-tool:" in makefile
    assert "require-grade-quality-tool:" in makefile
    assert "CHECK_VIOLATIONS ?= $(SHARED_SCRIPTS_DIR)/check-violations.sh" in makefile
    assert "GRADE_QUALITY    ?= $(SHARED_SCRIPTS_DIR)/grade_quality.sh" in makefile
    assert "GRADE_REPORT_JSON ?= $(QUALITY_DIR)/grade_quality_report.json" in makefile
    assert "VIOLATIONS_JSON   ?= $(QUALITY_DIR)/violations.json" in makefile
    assert "VIOLATIONS_LOG    ?= $(QUALITY_DIR)/violations.log" in makefile
    assert "VIOLATIONS_FLAGS  ?= --no-cache" in makefile
    assert "$(VIOLATIONS_FLAGS) \\" in makefile
    assert '--log-file "$(VIOLATIONS_LOG)"' in makefile
    assert '--json-file "$(VIOLATIONS_JSON)"' in makefile
    assert '--violations-json "$(VIOLATIONS_JSON)"' in makefile
    assert '--output-json "$(GRADE_REPORT_JSON)"' in makefile
    assert "~/.llm-shared" not in makefile


@pytest.mark.private_contract
def test_public_export_verify_targets_fail_fast() -> None:
    local_makefile = ROOT / "local.mk"
    if not local_makefile.exists():
        pytest.skip("local.mk is private maintainer wiring")
    makefile = local_makefile.read_text(encoding="utf-8")

    assert "PUBLIC_EXPORT_PYTEST_MARKER_EXPRESSION ?= not private_contract" in makefile

    public_export_section = makefile.split(
        "public-export-check: public-export",
        maxsplit=1,
    )[1].split("public-export-full-check:", maxsplit=1)[0]

    assert (
        '$(MAKE) --no-print-directory -C "$(PUBLIC_REPO)" "$$target" '
        'PYTEST_MARKER_EXPRESSION="$(PUBLIC_EXPORT_PYTEST_MARKER_EXPRESSION)" '
        "|| exit $$?"
    ) in public_export_section


def test_live_tests_use_timeout_budget_that_can_cover_configured_upstreams() -> None:
    makefile = read_combined_makefiles(ROOT)
    live_section = makefile.split("test-live:", maxsplit=1)[1].split(
        "test-e2e:",
        maxsplit=1,
    )[0]
    coverage_section = makefile.split("test-cov:", maxsplit=1)[1].split(
        "runtime-layout:",
        maxsplit=1,
    )[0]
    live_timeout = re.search(r"^PYTEST_LIVE_TIMEOUT \?= (?P<seconds>\d+)$", makefile, re.MULTILINE)
    coverage_timeout = re.search(
        r"^PYTEST_COV_TIMEOUT \?= \$\(PYTEST_LIVE_TIMEOUT\)$",
        makefile,
        re.MULTILINE,
    )

    assert live_timeout is not None
    assert int(live_timeout.group("seconds")) >= 300
    assert coverage_timeout is not None
    assert int(live_timeout.group("seconds")) > 60
    assert "PYTEST_LIVE_COMMON" in makefile
    assert "PYTEST_COV_COMMON" in makefile
    assert "$(PYTEST_LIVE_COMMON) $(PYTEST_LIVE_TARGETS)" in live_section
    assert "$(PYTEST_COMMON) $(PYTEST_LIVE_TARGETS)" not in live_section
    assert "$(PYTEST_COV_COMMON)" in coverage_section
    assert "$(PYTEST_COMMON)" not in coverage_section
    assert "$(PYTEST_MARKER_ARGS)" not in coverage_section
    assert "$(PYTEST_COV_MARKER_ARGS)" in makefile


def test_deps_installs_public_example_for_editable_cli() -> None:
    makefile = read_combined_makefiles(ROOT)

    deps_section = makefile.split("$(VENV_DIR)/.deps.stamp: $(VENV_DIR)/bin/python $(REQUIREMENTS) pyproject.toml", maxsplit=1)[1].split(
        "config-init:",
        maxsplit=1,
    )[0]
    assert "deps: $(VENV_DIR)/.deps.stamp" in makefile
    assert "package-build: $(VENV_DIR)/.deps.stamp" in makefile
    assert "mutation: $(VENV_DIR)/.deps.stamp" in makefile
    assert "$(VENV_DIR)/share/mcp-broker/config" in deps_section
    assert 'cp "$(CONFIG_TEMPLATE_PATH)" "$(VENV_DIR)/share/mcp-broker/config/broker.example.yaml"' in deps_section
