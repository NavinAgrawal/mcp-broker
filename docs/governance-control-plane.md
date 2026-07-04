# Governance Control Plane

`mcp-broker` does not need a hosted service to support enterprise governance.
Phase 2 defines publishable documents and local simulation rules first. The
local broker still owns execution, upstream startup, profile gates, client
rendering, status, and rollback on the engineer's machine.

## Governance Bundle Documents

Desired state is split into five publishable documents. They can be reviewed,
versioned, signed, mirrored through an internal artifact store, and validated by
the broker without running upstream tools centrally.

### Profile Bundle

The profile bundle defines client-visible behavior:

- profile names
- compact broker facade settings
- maximum exposed tools
- upstream exposure lists
- mutating-upstream allowlists
- broker tool name style

It must not contain local client config paths, account names, OAuth state,
runtime sockets, or private upstream inventory.

### Upstream Catalog Bundle

The upstream catalog bundle defines approved upstream templates:

- upstream identifiers and display names
- transport class
- command and argument templates
- purpose, tags, mutating flags, and lifecycle mode
- safe smoke-probe shape
- required environment variable names

It may name environment variables. It must not contain secret values. A catalog
entry is data only; the local broker decides whether a local private config maps
that entry to a real upstream.

### Policy Bundle

The policy bundle defines guardrails:

- `approval_required`
- `allow_remote_code_execution`
- `mutating_upstreams_require_allowlist`
- profile exposure rules
- redaction requirements
- rollback and break-glass requirements

The safe default is `approval_required: true`,
`allow_remote_code_execution: false`, and
`mutating_upstreams_require_allowlist: true`.

### Rollout Bundle

The rollout bundle defines how a version moves through a fleet:

- channels
- canary cohorts
- staged rollout rings
- required health gates
- rollback triggers
- operator approval points

The rollout document does not push config by itself. It gives the local broker
and offline simulator a plan to evaluate.

### Compatibility Bundle

The compatibility bundle defines version constraints:

- broker version range
- config schema version range
- required features
- deprecated feature flags
- compatibility rejection reasons

Compatibility rejection must happen before apply. A broker that cannot prove
compatibility must refuse the bundle.

## Local Execution Boundary

Execution stays local. The control-plane contract:

- does not run upstream tools centrally
- does not accept inbound remote tool calls
- does not start a remote listener
- does not move OAuth, browser, filesystem, or database state off the machine
- does not write client config without local approval

The local broker validates governance documents, stages desired state, applies
approved changes, reports status, and performs rollback through local runtime
state.

## Offline Control-Plane Simulation

Phase 2 uses local simulation only. The simulator should load the five
publishable documents, then evaluate:

- canary selection
- staged rollout ordering
- approval decisions
- compatibility rejection
- rollback decisions
- health gate outcomes

The simulator is not a hosted service. It is a deterministic local proof path
for enterprise governance behavior before any shared infrastructure is designed.

Run it against a local bundle and one or more redacted fleet-status payloads:

```bash
mcp-broker rollout simulate --bundle path/to/bundle.json --fleet-status path/to/fleet-status.json
mcp-broker rollout simulate --bundle path/to/bundle.json --fleet-status path/to/fleet-status.json --approved
```

The result is a decision payload with `mode: local_simulation_only`. It can
return `approval_required`, `compatibility_rejection`, `rollback`, or `ready`.
Ready simulations list broker-level decisions for canary, staged rollout, and
broad rollout stages. Rollback simulations list the broker and stage that
triggered rollback. The command reads local files and writes JSON to stdout; it
does not fetch bundles, upload status, contact a control plane, or mutate
runtime state.

## Signed Bundle Publishing

Signed bundle publishing creates a local publish manifest for a validated
governance bundle. The publish manifest records source provenance, bundle
version, channel, digest, compatibility range, signature reference, promotion state, and revocation state.

Publishing is still local file preparation. It must reject unsigned candidates,
reject unchecksummed bundles, and report `changed_runtime_state: false`. A
publish manifest is not an assignment, not a rollout action, and not a remote
tool execution request.

## Assignment Source Contract

Assignment source documents map broker identifiers and match users, teams,
channels, and rings to published governance bundle versions. They are data-only routing
documents. The local broker evaluates them against its own broker identity and
caller context, then selects one target bundle digest from already published
manifests.

Assignment sources must not include local filesystem paths, account names,
secret values, runtime sockets, OAuth state, or private upstream inventory.
The evaluator rejects local paths, account names, secret-looking values,
unpublished bundle target references, invalid match fields, and ambiguous
assignment matches with the same priority.
Error messages include `local paths are not allowed` and
`unpublished bundle target` so operators can fix the assignment source without
guessing.
The exact match dimensions are users, teams, channels, and rings. Same-priority
double matches fail as ambiguous assignment matches.

Assignment evaluation must report `changed_runtime_state: false`. It does not
fetch bundles, apply bundles, update client config, upload status, or call
upstream tools.

## Broker Pull/Apply Protocol

The broker can pull an assigned governance bundle into local cache, validate it,
and apply it only after a local approval record exists.

Pull supports `file://` bundle sources and localhost HTTP(S) bundle sources.
It requires an auth reference such as `env:GOVERNANCE_FETCH_TOKEN` or
`keychain:GOVERNANCE_FETCH_TOKEN` and records only the reference, never the
secret value. Remote non-localhost URLs are rejected in this phase.

Pull writes a cache record under local runtime state:

```bash
mcp-broker governance pull \
  --source file:///path/to/bundle.json \
  --assignment-decision assignment-decision.json \
  --state-dir ~/mcp/mcp-broker/state \
  --auth-ref env:GOVERNANCE_FETCH_TOKEN \
  --auth-present
```

The pull step validates bundle schema, checksum, compatibility, and assigned
target digest before writing the cache record. It reports
`changed_runtime_state: false` and does not update deployment pointers.

Apply requires an explicit approval JSON whose assignment id and target match
the pull record:

```bash
mcp-broker governance apply \
  --pull-record ~/mcp/mcp-broker/state/governance-pull/cache/.../pull-record.json \
  --approval approval.json \
  --state-dir ~/mcp/mcp-broker/state
```

Apply delegates to the P1 transactional deployment state. Rollback uses the
same deployment rollback pointer swap:

```bash
mcp-broker governance rollback --state-dir ~/mcp/mcp-broker/state
```

The Make targets are `governance-pull`, `governance-apply`, and
`governance-rollback`. Hosted fetching and central approval workflow are still
future Phase 2 work; the current protocol proves the local broker behavior.

## Rollout Controller

The rollout controller turns `local_simulation_only` output into an
append-only action ledger. It records canary, staged, broad, rollback, and hold actions
under local runtime state while preserving `changed_runtime_state: false`.

```bash
mcp-broker governance rollout-control \
  --simulation rollout-simulation.json \
  --state-dir ~/mcp/mcp-broker/state \
  --operator release-operator \
  --bundle-id governance-bundle \
  --bundle-version ${PACKAGE_VERSION} \
  --bundle-channel stable \
  --bundle-digest sha256:abc123
```

The controller writes individual action records under
`governance-rollout/actions/` and appends every record to
`governance-rollout/action-log.jsonl`. Each record includes the operator,
bundle version and digest, source simulation state, broker id, stage, action,
approval requirement, reasons, and the original simulator decision.

`approval_required` and `compatibility_rejection` simulations become hold
records. `rollback` simulations become rollback records for the affected
brokers. `ready` simulations become canary, staged, or broad rollout records.
The command rejects non-local simulation modes, does not fetch bundles, does
not update deployment pointers, does not contact a hosted service, and does not
call upstream tools.

The Make target is `governance-rollout-control`.

## Fleet Status Export And Collection

Fleet status is a redacted export derived from the local
`state/broker-status.json` snapshot:

```bash
mcp-broker fleet-status export --status-file ~/mcp/mcp-broker/state/broker-status.json
```

The export includes:

- broker identity: `broker_id`, `environment`, `bundle_version`,
  `schema_version`, and active profile names
- health: daemon status, start time, update time, and last request status
- counters: total requests and request errors
- upstream states: enabled flag, lifecycle state, transport, mode, mutating
  flag, auth state, restart count, and redacted last error

The export must not include local filesystem paths, socket paths, process IDs,
environment maps, account names, URLs, token values, credential values, OAuth
state, or private upstream configuration.

Collection wraps that redacted status in a local-only envelope:

```bash
mcp-broker fleet-status collect \
  --status-file ~/mcp/mcp-broker/state/broker-status.json \
  --target-url https://collector.example.invalid/mcp-broker/fleet-status \
  --auth-ref env:MCP_BROKER_FLEET_COLLECTOR_TOKEN \
  --retention-days 30 \
  --collector-id local-fleet-collector
```

The Make target is `fleet-status-collect` and reads these variables:
`FLEET_STATUS_FILE`, `FLEET_STATUS_TARGET`, `FLEET_STATUS_AUTH_REF`,
`FLEET_STATUS_RETENTION_DAYS`, and `FLEET_STATUS_COLLECTOR_ID`.

Collection is still prepare-only. It validates the upload target, auth
reference, retention period, retry policy, failure handling, and redacted
payload, then prints JSON to stdout. It does not upload to a remote service.
Upstream names are anonymized as `upstream-001`, `upstream-002`, and so on
before collection so private MCP inventory does not leave the local broker.
Targets with embedded credentials or secret-bearing query parameters are
rejected. Raw status payloads with local paths, socket paths, process IDs,
account names, URLs, token values, or credential values are rejected.

The broker does not open an inbound status endpoint.

## Enterprise Adoption Path

An enterprise can publish the five documents from an internal artifact system,
then let each engineer clone the public repo, install the broker, and validate
the desired state locally. That keeps the repo useful for personal users while
giving teams a governance contract they can automate around.
