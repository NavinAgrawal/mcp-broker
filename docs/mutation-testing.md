# Mutation Testing

Mutation testing is a release gate, not a quick local loop.

Run the public release gate before publishing:

```bash
make release-gate
```

`release-gate` refreshes dependencies once, then runs repo-local quality checks,
package checks, release smoke, and mutation testing. Linux/CI can run those
children in parallel. macOS runs them sequentially by default so mutation cannot
fight pytest and Docker for the same cores.

Local parallelism is capped by default:

```bash
LOCAL_CPU_BUDGET=2  # macOS
LOCAL_CPU_BUDGET=4  # Linux and CI
```

That budget feeds pytest workers, release-gate fanout workers, and mutmut
children. On macOS, mutmut and the Linux-container mutation path default to one
child and run through `taskpolicy -b` so the desktop stays responsive. During a
Linux/CI `release-gate`, mutation receives a release-scoped child count divided
by `RELEASE_GATE_JOBS` so it does not consume the full runner budget while the
other release children run. Raise the budget in CI or on a spare machine, for
example `make release-gate LOCAL_CPU_BUDGET=8`; lower it when you need the
workstation to stay responsive.

The private source repo also has `make maintainer-release-gate`, which runs the
public export dry run alongside the public release checks.

For mutation only:

```bash
make mutation
```

On macOS release runs use the Linux container mutation target:

```bash
make mutation-linux
```

`make release-gate` selects `mutation-linux` on Darwin and `mutation` on Linux.
It resolves affected Python source from the release diff and subtracts signed
whole-file entries from `docs/mutation-carveouts.md`. This avoids macOS
fork-related mutmut failures without mutating unrelated source.

By default, `make mutation-linux` and the release-gate mutation child resolve
the changed Python source files under the configured mutmut roots by diffing
against `MUTATION_DIFF_BASE`:

```bash
MUTATION_DIFF_BASE=origin/main
```

That keeps interactive macOS runs scoped to the files touched by the current
release train. To override the selector, pass an explicit list:

```bash
make mutation-linux MUTATION_PATHS_TO_MUTATE="src/mcp_broker/fleet_collection.py"
```

The Linux mutation wrapper rewrites `paths_to_mutate` only inside the disposable
container workspace. The committed `setup.cfg` remains the full public mutation
universe. The wrapper also copies `src` into the mutmut workspace so a narrowed
mutation target can still import the complete package during stats collection
and mutant execution.

For a full repo burn, pass the full source root deliberately:

```bash
make mutation-linux MUTATION_PATHS_TO_MUTATE="src/mcp_broker"
```

Direct script runs follow the same macOS guard:

```bash
scripts/linux-mutation.sh
```

When `MCP_BROKER_MUTATION_MAX_CHILDREN` is unset, the script uses one child on
macOS and four elsewhere. The host-side Docker run uses background QoS on
macOS and still goes through the global command governor; if load is over the
interactive mutation threshold, wait instead of bypassing the guard.

The target runs mutmut against the selected mutation paths using public unit and
journey tests, then reads the generated `mutants/**/*.meta` files with
`scripts/check_mutation_stats.py`.
The mutmut config copies the public docs, config, registry metadata, packaging
assets, workflow files, scripts, `Makefile`, and root public docs into the
mutated workspace because several journey tests enforce those public contracts.

The stats checker writes:

```text
var/quality/mutation_stats.json
```

The JSON report includes top-level counts, the score, and a ranked
`blocked_by_file` list with capped examples for `survived`, `no_tests`, and
other failing statuses. Use that list to fix the largest behavioral gaps
first.

The gate fails when any mutant is:

- `survived`
- `no_tests`
- `skipped`
- `suspicious`
- `timeout`
- `check_was_interrupted_by_user`
- `segfault`
- `not_checked`

The default score threshold is 100. Type-check catches count as passing
mutants. Generated mutation work directories stay out of git:

```text
mutants/
.mutmut-cache/
var/quality/mutation_stats.json
```

## Public Repo Policy

The public repo should include:

- `setup.cfg` mutation configuration
- `make mutation`
- `make mutation-linux`
- `make release-gate`
- `scripts/check_mutation_stats.py`
- `scripts/linux-mutation.sh`
- public-safe tests under `tests/`

The public repo should not include generated mutation output. `mutants/` and
`.mutmut-cache/` are local artifacts and must stay ignored.

## Interpreting Results

Line and branch coverage tell us tests executed code. Mutation testing checks
whether tests reject changed behavior.

Do not treat mutmut's process exit code alone as proof. The release gate is the
JSON stats check. A run with `0 survived` but thousands of `segfault` or
`not_checked` mutants fails because it did not prove the suite killed those
mutants.

## Scope

The default mutation selector uses public unit and journey tests. Local e2e
tests stay in `make quality-gate`, not mutmut, because they spawn console
entrypoints in subprocesses. Mutmut's instrumentation requires an in-process
runner; subprocess imports of instrumented modules crash during stats
collection before mutant execution. Private contract tests are excluded with
the `private_contract` marker because they validate the maintainer export
pipeline and private runbooks, not public broker behavior.

Public CI can run `make quality-gate` on every PR and reserve `make
release-gate` for tagged releases, protected release branches, or manual
maintainer runs.
