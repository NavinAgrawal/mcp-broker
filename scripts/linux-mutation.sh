#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${MCP_BROKER_MUTATION_IMAGE:-python:3.11-bookworm}"
DEFAULT_MAX_CHILDREN=4
if [[ "$(uname -s)" == "Darwin" ]]; then
  DEFAULT_MAX_CHILDREN=1
fi
MAX_CHILDREN="${MCP_BROKER_MUTATION_MAX_CHILDREN:-$DEFAULT_MAX_CHILDREN}"
MUTATION_ARGS_VALUE="${MCP_BROKER_MUTATION_ARGS:-}"
MUTATION_DEBUG="${MCP_BROKER_MUTATION_DEBUG:?MCP_BROKER_MUTATION_DEBUG is required}"
MUTATION_PATHS_TO_MUTATE="${MCP_BROKER_MUTATION_PATHS_TO_MUTATE:-}"
MUTATION_TESTS_TO_RUN="${MCP_BROKER_MUTATION_TESTS_TO_RUN:-}"
WORK_DIR="${MCP_BROKER_MUTATION_WORK_DIR:-}"
LOG_PATH="${MCP_BROKER_MUTATION_LOG:-$ROOT/var/quality/mutation-linux.log}"
MUTANTS_EXPORT_DIR="${MCP_BROKER_MUTATION_MUTANTS_DIR:-$ROOT/var/quality/mutants-linux}"
WORK_DIR_CREATED=0

usage() {
  cat <<'USAGE'
usage: linux-mutation.sh [--help]

Runs the mutation gate inside a Linux container from a public-safe source
archive. The host receives only var/quality/mutation_stats.json.

Environment:
  MCP_BROKER_MUTATION_IMAGE         Container image, default: python:3.11-bookworm
  MCP_BROKER_MUTATION_MAX_CHILDREN  mutmut worker count, default: 1 on macOS, 4 elsewhere
  MCP_BROKER_MUTATION_ARGS          Optional mutant selector
  MCP_BROKER_MUTATION_DEBUG         Mutmut debug flag, true or false
  MCP_BROKER_MUTATION_PATHS_TO_MUTATE Optional whitespace-separated source paths
  MCP_BROKER_MUTATION_TESTS_TO_RUN  Optional whitespace-separated affected test paths
  MCP_BROKER_MUTATION_WORK_DIR      Optional existing work directory
  MCP_BROKER_MUTATION_LOG           Host log path, default: var/quality/mutation-linux.log
  MCP_BROKER_MUTATION_MUTANTS_DIR   Host mutants export dir, default: var/quality/mutants-linux
USAGE
}

for arg in "$@"; do
  case "$arg" in
    --help|-h)
      usage
      exit 0
      ;;
    *)
      printf "unknown argument: %s\n" "$arg" >&2
      usage >&2
      exit 2
      ;;
  esac
done

command -v docker >/dev/null 2>&1 || {
  printf "docker is required for linux-mutation\n" >&2
  exit 2
}

QOS_PREFIX=()
if [[ "$(uname -s)" == "Darwin" ]] && command -v taskpolicy >/dev/null 2>&1; then
  QOS_PREFIX=(taskpolicy -b)
fi

if [[ -z "$WORK_DIR" ]]; then
  WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/mcp-broker-linux-mutation.XXXXXX")"
  WORK_DIR_CREATED=1
fi

cleanup() {
  if [[ "$WORK_DIR_CREATED" == "1" ]]; then
    rm -rf "$WORK_DIR"
  fi
}
trap cleanup EXIT

tar_option_supported() {
  local option="$1"
  local probe_dir
  probe_dir="$(mktemp -d "${TMPDIR:-/tmp}/mcp-broker-tar-probe.XXXXXX")"
  if tar "$option" -cf /dev/null -C "$probe_dir" . >/dev/null 2>&1; then
    rm -rf "$probe_dir"
    return 0
  fi
  rm -rf "$probe_dir"
  return 1
}

ARCHIVE_PATH="$WORK_DIR/source.tar"
TAR_CREATE_OPTIONS=()
if tar_option_supported "--no-xattrs"; then
  TAR_CREATE_OPTIONS+=(--no-xattrs)
fi
if tar_option_supported "--no-mac-metadata"; then
  TAR_CREATE_OPTIONS+=(--no-mac-metadata)
fi

COPYFILE_DISABLE=1 tar \
  "${TAR_CREATE_OPTIONS[@]}" \
  --exclude=".git" \
  --exclude="venv-mcp-broker" \
  --exclude="config/broker.private.yaml" \
  --exclude="var/coverage/*" \
  --exclude="var/quality/*" \
  --exclude="var/test-logs/*" \
  --exclude=".mutmut-cache" \
  --exclude="mutants" \
  --exclude="dist" \
  --exclude="build" \
  -C "$ROOT" \
  -cf "$ARCHIVE_PATH" \
  .

mkdir -p "$ROOT/var/quality"
mkdir -p "$(dirname "$LOG_PATH")"
rm -f "$LOG_PATH"
rm -rf "$MUTANTS_EXPORT_DIR"
mkdir -p "$MUTANTS_EXPORT_DIR"

set +e
"${QOS_PREFIX[@]}" docker run --rm \
  -e MCP_BROKER_MUTATION_MAX_CHILDREN="$MAX_CHILDREN" \
  -e MCP_BROKER_MUTATION_ARGS="$MUTATION_ARGS_VALUE" \
  -e MCP_BROKER_MUTATION_DEBUG="$MUTATION_DEBUG" \
  -e MCP_BROKER_MUTATION_PATHS_TO_MUTATE="$MUTATION_PATHS_TO_MUTATE" \
  -e MCP_BROKER_MUTATION_TESTS_TO_RUN="$MUTATION_TESTS_TO_RUN" \
  -v "$ARCHIVE_PATH:/tmp/source.tar:ro" \
  -v "$ROOT/var/quality:/output" \
  -v "$MUTANTS_EXPORT_DIR:/mutants-output" \
  "$IMAGE" \
  bash -lc '
    set -euo pipefail
    copy_mutants() {
      if [[ -d /workspace/mutants ]]; then
        mkdir -p /mutants-output
        rm -rf /mutants-output/*
        cp -a /workspace/mutants/. /mutants-output/
      fi
    }
    rewrite_mutation_scope() {
      python3 - <<'"'"'PY'"'"'
import configparser
import os

paths = os.environ.get("MCP_BROKER_MUTATION_PATHS_TO_MUTATE", "").split()
tests = os.environ.get("MCP_BROKER_MUTATION_TESTS_TO_RUN", "").split()
debug = os.environ["MCP_BROKER_MUTATION_DEBUG"]
if not paths:
    raise SystemExit(0)

parser = configparser.ConfigParser()
read_files = parser.read("setup.cfg")
if not read_files or not parser.has_section("mutmut"):
    raise SystemExit("setup.cfg has no mutmut section")

value = "\n" + "\n".join(f"    {path}" for path in paths)
parser.set("mutmut", "paths_to_mutate", value)
if tests:
    tests_value = "\n" + "\n".join(f"    {path}" for path in tests)
    parser.set("mutmut", "tests_dir", tests_value)
    parser.set("mutmut", "debug", debug)
also_copy = []
if parser.has_option("mutmut", "also_copy"):
    also_copy = [
        line.strip()
        for line in parser.get("mutmut", "also_copy").splitlines()
        if line.strip()
    ]
if "src" not in also_copy:
    also_copy.append("src")
also_copy_value = "\n" + "\n".join(f"    {path}" for path in also_copy)
parser.set("mutmut", "also_copy", also_copy_value)
with open("setup.cfg", "w", encoding="utf-8") as handle:
    parser.write(handle)
PY
    }
    trap copy_mutants EXIT
    apt-get update
    apt-get install -y --no-install-recommends make
    mkdir -p /workspace /tmp/home /tmp/runtime
    tar -xf /tmp/source.tar -C /workspace
    cd /workspace
    rewrite_mutation_scope
    HOME=/tmp/home make mutation \
      RUNTIME_ROOT=/tmp/runtime \
      PYTHON_BIN=python3 \
      VENV_DIR=/tmp/venv-mcp-broker \
      PYTHON=/tmp/venv-mcp-broker/bin/python \
      PIP=/tmp/venv-mcp-broker/bin/pip \
      MUTMUT=/tmp/venv-mcp-broker/bin/mutmut \
      MUTATION_MAX_CHILDREN="$MCP_BROKER_MUTATION_MAX_CHILDREN" \
      MUTATION_ARGS="$MCP_BROKER_MUTATION_ARGS" \
      MUTATION_STATS_JSON=/output/mutation_stats.json
  ' 2>&1 | if [[ -n "$MUTATION_ARGS_VALUE" ]]; then
    tee "$LOG_PATH" >/dev/null
  else
    tee "$LOG_PATH"
  fi
pipeline_status=("${PIPESTATUS[@]}")
container_status=${pipeline_status[0]}
log_status=${pipeline_status[1]}
set -e
if [[ "$container_status" -ne 0 || "$log_status" -ne 0 ]]; then
  if [[ -n "$MUTATION_ARGS_VALUE" ]]; then
    tail -n 80 "$LOG_PATH" >&2
  fi
  if [[ "$container_status" -ne 0 ]]; then
    exit "$container_status"
  fi
  exit "$log_status"
fi

printf "linux_mutation=true image=%s stats=%s log=%s\n" "$IMAGE" "$ROOT/var/quality/mutation_stats.json" "$LOG_PATH"
