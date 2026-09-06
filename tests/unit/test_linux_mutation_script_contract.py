from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.unit


def test_linux_mutation_script_streams_container_output_to_host_log() -> None:
    script = (ROOT / "scripts" / "linux-mutation.sh").read_text(encoding="utf-8")

    assert 'LOG_PATH="${MCP_BROKER_MUTATION_LOG:-$ROOT/var/quality/mutation-linux.log}"' in script
    assert 'mkdir -p "$(dirname "$LOG_PATH")"' in script
    assert 'rm -f "$LOG_PATH"' in script
    assert '2>&1 | if [[ -n "$MUTATION_ARGS_VALUE" ]]; then' in script
    assert 'else\n    tee "$LOG_PATH"' in script
    assert 'printf "linux_mutation=true image=%s stats=%s log=%s\\n"' in script


def test_linux_mutation_script_suppresses_exact_slice_chatter() -> None:
    script = (ROOT / "scripts" / "linux-mutation.sh").read_text(encoding="utf-8")

    assert 'if [[ -n "$MUTATION_ARGS_VALUE" ]]; then' in script
    assert 'tee "$LOG_PATH" >/dev/null' in script
    assert 'pipeline_status=("${PIPESTATUS[@]}")' in script
    assert 'container_status=${pipeline_status[0]}' in script
    assert 'tail -n 80 "$LOG_PATH" >&2' in script


def test_linux_mutation_script_uses_mac_safe_default_and_background_qos() -> None:
    script = (ROOT / "scripts" / "linux-mutation.sh").read_text(encoding="utf-8")

    assert "DEFAULT_MAX_CHILDREN=4" in script
    assert 'if [[ "$(uname -s)" == "Darwin" ]]; then' in script
    assert "  DEFAULT_MAX_CHILDREN=1" in script
    assert 'MAX_CHILDREN="${MCP_BROKER_MUTATION_MAX_CHILDREN:-$DEFAULT_MAX_CHILDREN}"' in script
    assert "QOS_PREFIX=()" in script
    assert "QOS_PREFIX=(taskpolicy -b)" in script
    assert '"${QOS_PREFIX[@]}" docker run --rm' in script


def test_linux_mutation_script_preserves_caller_supplied_work_dir() -> None:
    script = (ROOT / "scripts" / "linux-mutation.sh").read_text(encoding="utf-8")

    assert "WORK_DIR_CREATED=0" in script
    assert 'WORK_DIR_CREATED=1' in script
    assert 'if [[ "$WORK_DIR_CREATED" == "1" ]]; then' in script
    assert 'rm -rf "$WORK_DIR"' in script


def test_linux_mutation_script_exports_container_mutants_for_debugging() -> None:
    script = (ROOT / "scripts" / "linux-mutation.sh").read_text(encoding="utf-8")

    assert (
        'MUTANTS_EXPORT_DIR="${MCP_BROKER_MUTATION_MUTANTS_DIR:-$ROOT/var/quality/mutants-linux}"'
        in script
    )
    assert 'rm -rf "$MUTANTS_EXPORT_DIR"' in script
    assert 'mkdir -p "$MUTANTS_EXPORT_DIR"' in script
    assert '-v "$MUTANTS_EXPORT_DIR:/mutants-output"' in script
    assert 'copy_mutants()' in script
    assert 'trap copy_mutants EXIT' in script
    assert "mkdir -p /mutants-output" in script
    assert 'cp -a /workspace/mutants/. /mutants-output/' in script


def test_linux_mutation_script_can_restrict_paths_to_mutate_inside_container() -> None:
    script = (ROOT / "scripts" / "linux-mutation.sh").read_text(encoding="utf-8")

    assert 'MUTATION_PATHS_TO_MUTATE="${MCP_BROKER_MUTATION_PATHS_TO_MUTATE:-}"' in script
    assert '-e MCP_BROKER_MUTATION_PATHS_TO_MUTATE="$MUTATION_PATHS_TO_MUTATE"' in script
    assert 'rewrite_mutation_scope()' in script
    assert 'paths = os.environ.get("MCP_BROKER_MUTATION_PATHS_TO_MUTATE", "").split()' in script
    assert "parser.set(\"mutmut\", \"paths_to_mutate\", value)" in script
    assert "also_copy.append(\"src\")" in script
    assert "parser.set(\"mutmut\", \"also_copy\", also_copy_value)" in script
    assert "rewrite_mutation_scope" in script


def test_linux_mutation_script_can_restrict_affected_tests_inside_container() -> None:
    script = (ROOT / "scripts" / "linux-mutation.sh").read_text(encoding="utf-8")

    assert 'MUTATION_TESTS_TO_RUN="${MCP_BROKER_MUTATION_TESTS_TO_RUN:-}"' in script
    assert '-e MCP_BROKER_MUTATION_TESTS_TO_RUN="$MUTATION_TESTS_TO_RUN"' in script
    assert 'tests = os.environ.get("MCP_BROKER_MUTATION_TESTS_TO_RUN", "").split()' in script
    assert "parser.set(\"mutmut\", \"tests_dir\", tests_value)" in script


def test_linux_mutation_debug_output_is_opt_in() -> None:
    script = (ROOT / "scripts" / "linux-mutation.sh").read_text(encoding="utf-8")
    config = (ROOT / "mk" / "config.mk").read_text(encoding="utf-8")
    release = (ROOT / "mk" / "release.mk").read_text(encoding="utf-8")

    assert "MUTATION_DEBUG   ?= false" in config
    assert 'MCP_BROKER_MUTATION_DEBUG="$(MUTATION_DEBUG)"' in release
    assert 'MUTATION_DEBUG="${MCP_BROKER_MUTATION_DEBUG:?MCP_BROKER_MUTATION_DEBUG is required}"' in script
    assert '-e MCP_BROKER_MUTATION_DEBUG="$MUTATION_DEBUG"' in script
    assert 'debug = os.environ["MCP_BROKER_MUTATION_DEBUG"]' in script
    assert 'parser.set("mutmut", "debug", debug)' in script


def test_mutmut_copies_public_listing_metadata_into_mutant_workspaces() -> None:
    setup_cfg = (ROOT / "setup.cfg").read_text(encoding="utf-8")

    assert "    brand" in setup_cfg
    assert "glama.json" in setup_cfg


def test_mutation_carveout_registry_records_config_keys_tool_incompatibility() -> None:
    registry = (ROOT / "docs" / "mutation-carveouts.md").read_text(encoding="utf-8")

    assert "src/mcp_broker/config_keys.py" in registry
    assert "whole file" in registry
    assert "tool-incompatible" in registry
    assert "mutmut 3.5" in registry
    assert "a7c2123bb44ca89ff1ec84a75eabcb4fea05eab72f2c9816f1dc74197aedc218" in registry
    assert "Approved 2026-09-05" in registry


def test_mutation_carveout_registry_records_daemon_class_method_limit() -> None:
    registry = (ROOT / "docs" / "mutation-carveouts.md").read_text(encoding="utf-8")

    assert "`BrokerDaemon._handle_connection`" in registry
    assert "`BrokerDaemon._read_request`" in registry
    assert "`BrokerDaemon._send_response`" in registry
    assert "`BrokerDaemon._reap_idle_upstreams`" in registry
    assert "538f7b8d2e8cb81fcb88b1ce4843be1ba485498c9d5942dbf41d632e02a1e308" in registry
    assert "source or mutmut version drift invalidates this approval" in registry


def test_mutation_carveout_registry_records_typing_only_protocol_limit() -> None:
    registry = (ROOT / "docs" / "mutation-carveouts.md").read_text(encoding="utf-8")

    assert "src/mcp_broker/upstream_protocols.py" in registry
    assert "whole file, lines 1-48" in registry
    assert "typing-only Protocol declarations" in registry
    assert "64c4b31d980c40e7dde4603bdc4213851f32ef4e32706d2555e3919b5a9129ec" in registry


def test_incremental_and_release_mutation_use_affected_file_selectors() -> None:
    release = (ROOT / "mk" / "release.mk").read_text(encoding="utf-8")
    incremental_block = release.split("_mutation-linux-impl:", maxsplit=1)[1].split(
        "release-gate:", maxsplit=1
    )[0]
    release_block = release.split("_release-gate-mutation-run:", maxsplit=1)[1]

    assert "--diff-base \"$(MUTATION_DIFF_BASE)\" --format make" in incremental_block
    assert "--all" not in incremental_block
    assert "--all" not in release_block
    assert '--diff-base "$(MUTATION_DIFF_BASE)" --format make' in release_block
