# Runtime Layout

Runtime root:

```text
~/mcp/mcp-broker/
  config/
  logs/
  run/
    sockets/
    upstreams/
  secrets/
  sockets/
  state/
    broker-status.json
    deployments/
      active.json
      previous.json
      rollback-journal.jsonl
      records/
    runtime-install/
      active-runtime.json
      previous-runtime.json
      versions/
        <version>/
          <runtime_id>/
            runtime-manifest.json
    upstreams/
      example-store/
      example-http/
      example-mutating/
```

The repo owns source and tests. The runtime root owns machine-specific state.

The sole live inventory is `config/broker.private.yaml` under the runtime root.
For an existing checkout-local inventory, inspect the migration before changing
anything, then opt in only when it reports `ready`:

```bash
make migrate-runtime-config
make migrate-runtime-config RUNTIME_CONFIG_MIGRATION_APPLY=1
```

The migration refuses a divergent destination. `make runtime-sync-check` then
verifies the LaunchAgent points at the canonical config and public runtime tree.

`make doctor` creates the base runtime directories, runs the broker-owned
runtime reaper, verifies that the central config file exists, and fails when an
enabled stdio upstream command is missing or not executable. Disabled upstreams
and non-stdio upstreams are skipped. It does not start the broker.

Runtime ownership metadata is file-backed under `run/upstreams/*.json` and
`run/sockets/*.json`. `make broker-reap` removes stale broker-owned pidfiles,
removes stale broker-owned sockets, and kills orphaned broker-owned process
groups whose recorded broker PID no longer exists. If an upstream parent PID has
already exited but the recorded process group still has child members, the reaper
kills that process group before removing the pidfile.

Broker daemon structured logs are newline-delimited JSON at
`logs/broker.jsonl`. Each record includes `ts`, `level`, `event`, and `pid`.
Daemon events cover lifecycle start/stop, handled socket requests, and upstream
events: `upstream.start`, `upstream.ready`, `upstream.call`,
`upstream.timeout`, `upstream.stop`, `upstream.kill`, `upstream.restart`,
`upstream.backoff`, and `upstream.disabled`. Upstream events include the
`upstream` name and event-specific fields such as `method`, `tool_name`,
`timeout_seconds`, `state`, `signal`, and `restart_count`.

Log values are redacted before write for env maps, tokens, credentials, access
IDs, URLs, and filesystem paths.

Broker daemon metrics are file-backed at `state/broker-status.json`. The daemon
writes the snapshot at start, after each handled socket request, and during
shutdown. The snapshot includes daemon status, PID, socket path, start and update
timestamps, broker identity, configured profile names, request and request-error
counters, last request method and status, and the same per-upstream health map
returned by `broker/health`, including auth-repair counters when a configured
repair path has run. The snapshot also includes `break_glass`; when an active
unexpired break-glass record exists, daemon status is written as `degraded` and
the break-glass block includes the active audit record.

Break-glass audit records are file-backed under `state/break-glass/`. The active
pointer lives at `active.json`, records live under `records/`, and audit events
append to `audit.jsonl`. Each record includes reason, operator, expiration,
bypassed policy paths, creation time, and audit path.

Desired-state deployment records are file-backed under `state/deployments/`.
`deployment-stage` validates `BUNDLE` and records an active deployment when
`DEPLOYMENT_DRY_RUN=0`; the default is dry-run. Active and previous deployment
pointers are written as separate JSON files, records live under
`state/deployments/records/`, and rollback/recovery actions append to
`rollback-journal.jsonl`.

Deployment state commands:

```bash
make deployment-stage BUNDLE=path/to/bundle.json
make deployment-stage BUNDLE=path/to/bundle.json DEPLOYMENT_DRY_RUN=0
make deployment-rollback
make deployment-recover
```

These commands do not edit client config files. Client rendering and rollback
remain under `config-render` and `config-rollback`.

Installed runtime manifests are file-backed under `state/runtime-install/`.
The active runtime pointer lives at `active-runtime.json`, and the previous
runtime pointer lives at `previous-runtime.json`. Runtime manifests live under
`state/runtime-install/versions/<version>/<runtime_id>/runtime-manifest.json`.

The installed-runtime manifest records the runtime version, runtime identifier,
installed runtime path, package entrypoint, artifact digest, and install status.
This establishes the stable broker-owned manifest layout that plugin setup and
launcher wiring will consume in later Phase 1 tasks.

Installed runtime manifests do not activate artifacts by themselves. The
launcher can resolve the active manifest into an argv plan, but artifact
integrity, bootstrap apply, rollback, and uninstall are separate Phase 1
contracts.

The active runtime launcher resolves the active pointer and manifest without
executing the installed runtime:

```bash
mcp-broker runtime launch-plan --state-dir ~/mcp/mcp-broker/state -- status
```

The command prints the active installed runtime argv as JSON. It fails closed
when the active pointer, manifest, runtime identifier, runtime path, or
entrypoint is missing, malformed, outside `runtime-install/versions/`, or a
symlink escape from the installed runtime path. It also rejects a non-executable
entrypoint. Runtime artifact digest verification and activation remain separate
Phase 1 contracts.

Runtime artifact integrity is verified before activation:

```bash
mcp-broker runtime artifact-verify --artifact path/to/runtime.zip --digest sha256:<digest>
mcp-broker runtime artifact-verify --metadata path/to/runtime-metadata.json
```

The verifier supports `.zip` and tar-compatible archives, validates the
`sha256:` digest, checks every archive member for absolute paths, `..` path
traversal, Windows drive or backslash traversal, archive links, and tar special
files. Empty archives are rejected.

Metadata sidecar verification is the activation-readiness check. Sidecar
`artifact_path` values must be relative to the metadata file directory and must
not escape that directory. Sidecar `entrypoint` values must be safe archive
member paths, must exist in the archive, and must identify an executable regular
file. `safe_to_activate` is reported only after the digest, archive safety, and
metadata entrypoint checks pass.

Artifact verification does not write active runtime pointers.

Bootstrap transactions use the verified metadata sidecar and the referenced
artifact archive to plan and apply activation:

```bash
mcp-broker runtime bootstrap preflight --metadata path/to/runtime-metadata.json --state-dir ~/mcp/mcp-broker/state
mcp-broker runtime bootstrap plan --metadata path/to/runtime-metadata.json --state-dir ~/mcp/mcp-broker/state
mcp-broker runtime bootstrap apply --metadata path/to/runtime-metadata.json --state-dir ~/mcp/mcp-broker/state --approved
mcp-broker runtime bootstrap status --state-dir ~/mcp/mcp-broker/state
mcp-broker runtime bootstrap rollback --state-dir ~/mcp/mcp-broker/state --approved
mcp-broker runtime bootstrap uninstall --state-dir ~/mcp/mcp-broker/state --approved
```

`preflight` verifies the artifact metadata and runtime entrypoint without
writing bootstrap state. `apply` extracts the verified archive into
`runtime-install/extracted-runtimes/`, runs a bounded entrypoint smoke check,
and moves the active runtime pointer only after that extracted runtime passes.
`rollback` validates the previous manifest and entrypoint before swapping
pointers. `apply`, `rollback`, and `uninstall` require explicit approval flags.
Failed apply preserves the previous active runtime pointer.
# Canonical local configuration

The machine-local broker inventory has one authority:

```text
$HOME/mcp/mcp-broker/config/broker.private.yaml
```

The private repository owns source and public-safe templates. The public repository is the release and runtime checkout. Neither checkout owns a separate live inventory file.

Use `make runtime-sync-check` from the public runtime checkout after any LaunchAgent change. It verifies the LaunchAgent runtime root, config path, and working directory without reading config values.

Use `make public-export-verify PUBLIC_REPO=<public-checkout>` from the private source checkout before a public commit. It compares selected public files without copying or modifying either checkout.
