#!/bin/bash

set -euo pipefail

tier=""
base=""
repo="."
dry_run=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --tier)
      tier="$2"
      shift 2
      ;;
    --base)
      base="$2"
      shift 2
      ;;
    --repo)
      repo="$2"
      shift 2
      ;;
    --dry-run)
      dry_run=1
      shift
      ;;
    -h|--help)
      printf 'Usage: test-impact.sh --tier commit|push|ci [--base REF] [--repo PATH] [--dry-run]\n'
      exit 0
      ;;
    *)
      printf 'test-impact: unknown arg: %s\n' "$1" >&2
      exit 2
      ;;
  esac
done

case "$tier" in
  ci)
    command=(make quality-gate)
    reason="release certification; Make-backed full suite with coverage"
    ;;
  commit|push)
    selection="$(
      python3 "$repo/scripts/select_affected_tests.py" \
        --root "$repo" \
        --tier "$tier" \
        --base "${base:-origin/main}"
    )"
    standard_tests=()
    live_tests=()
    selected_count=0
    standard_count=0
    live_count=0
    while IFS= read -r test_path; do
      if [ -n "$test_path" ]; then
        selected_count=$((selected_count + 1))
        case "$test_path" in
          tests/live/*)
            live_tests+=("$test_path")
            live_count=$((live_count + 1))
            ;;
          *)
            standard_tests+=("$test_path")
            standard_count=$((standard_count + 1))
            ;;
        esac
      fi
    done <<< "$selection"
    if [ "$selected_count" -eq 0 ]; then
      printf 'test-impact: selector returned zero tests\n' >&2
      exit 2
    fi
    standard_pytest_args=""
    live_pytest_args=""
    if [ "$standard_count" -gt 0 ]; then
      standard_pytest_args="${standard_tests[*]}"
    fi
    if [ "$live_count" -gt 0 ]; then
      live_pytest_args="${live_tests[*]}"
    fi
    reason="$selected_count affected pytest files selected ($standard_count standard, $live_count live)"
    ;;
  *)
    printf 'test-impact: --tier must be commit|push|ci (got %s)\n' "${tier:-}" >&2
    exit 2
    ;;
esac

cd "$repo"

printf '=== mcp-broker CITS override (tier=%s%s) ===\n' "$tier" "${base:+, base=$base}"
printf '  - %s\n' "$reason"

if [ "$tier" = "ci" ]; then
  printf '  $ make %s\n' "${command[*]:1}"
else
  if [ "$standard_count" -gt 0 ]; then
    printf '  $ make test PYTEST_ARGS=<%s selected files>\n' "$standard_count"
  fi
  if [ "$live_count" -gt 0 ]; then
    printf '  $ make test-live-targeted PYTEST_ARGS=<%s selected files>\n' "$live_count"
  fi
fi

if [ "$dry_run" -eq 1 ]; then
  exit 0
fi

# Selection above uses the commit's index; tests must not inherit its Git state.
git_local_names="$(git rev-parse --local-env-vars)"
while IFS= read -r git_local_name; do
  unset "$git_local_name"
done <<< "$git_local_names"

if [ "$tier" = "ci" ]; then
  exec "${command[@]}"
fi
if [ "$standard_count" -gt 0 ]; then
  make test "PYTEST_ARGS=$standard_pytest_args"
fi
if [ "$live_count" -gt 0 ]; then
  exec make test-live-targeted "PYTEST_ARGS=$live_pytest_args"
fi
