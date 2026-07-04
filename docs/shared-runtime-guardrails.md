# Shared Runtime Guardrails

`mcp-broker` is a local edge broker first. The shared hosted execution is not
implemented, and it must not be treated as an implied roadmap commitment until
the local product proves value and the governance contracts prove they can
control change safely.

Contract statement: shared hosted execution is not implemented.

## Current Boundary

The local edge broker remains the default execution boundary. Each engineer's
machine owns MCP client config, upstream startup, OAuth state, browser state,
runtime sockets, logs, deployment state, rollback, and profile validation.

There is no remote listener, no shared upstream execution, no hosted tool-call
endpoint, and no central process that runs an engineer's upstream MCP servers.
Phase 3 is a guardrail definition only.

## Preconditions

Shared hosted execution can be designed only after both proofs exist:

- Phase 1 value proof: plugin setup, clone-to-running setup, local deployment
  state, rollback, and profile validation work for real personal and team use.
- Phase 2 governance proof: signed desired-state bundles, redacted fleet
  status, local simulation, staged rollout, compatibility rejection, and
  approval decisions are proven without moving execution off the machine.

If either proof fails, the answer is to improve the local edge broker and its
governance contracts, not to add a shared runtime.

## Decision Gates

Before any shared runtime design starts, these decisions must have written
contracts and tests:

The required gates are tenant isolation, authorization, quotas, session
affinity, distributed state, cost controls, audit, and failure domains.
Contract statement: session affinity must be designed before shared runtime.

| Gate | Required decision |
|---|---|
| Tenant isolation | Define how workspaces, users, upstream state, tokens, logs, and runtime files are separated. |
| Authorization | Define who can publish bundles, approve rollout, call tools, view status, and perform rollback. |
| Quotas | Define per-user, per-team, per-upstream, and per-tool limits before any pooled runtime exists. |
| Session affinity | Define whether stateful upstreams stay bound to one session, one machine, one user, or one hosted worker. |
| Distributed state | Define storage, locking, rollback, recovery, and conflict behavior for deployment state outside one filesystem. |
| Cost controls | Define budget limits, rate limits, metering, owner attribution, and kill switches before shared execution. |
| Audit | Define immutable event records for config publication, approval, apply, rollback, tool calls, and policy denial. |
| Failure domains | Define blast radius, isolation boundaries, degraded mode, rollback mode, and break-glass behavior. |

No gate can be satisfied by prose alone. Each gate needs a testable contract,
a default-deny behavior, and a migration path from the local runtime.

## Threat Model

The P3 threat model treats tenant, workspace, user, upstream, token, log, runtime-state, and audit isolation as separate failure boundaries.
A shared runtime must prove each boundary before hosted execution can be
supported.

The current contract remains:

```yaml
hosted_execution_supported: false
default_execution_boundary: local_edge
```

Threats that must fail closed:

- cross-tenant tool discovery
- cross-workspace tool calls
- user impersonation
- upstream state reuse across tenants
- token reuse across tenants, workspaces, users, or upstreams
- log records that mix tenant or user scope
- runtime-state conflict without a lock and recovery journal
- audit events without tenant, workspace, user, action, result, and denial reason

The local edge broker remains the default.
Contract statement: unknown upstream classes default to local-only.
Browser, file-access, OAuth, local-secret, and stateful upstreams are local-only
until a later task proves a narrower safe class.

## Tenant Model

Every shared-runtime decision requires a tenant context with `tenant_id`,
`workspace_id`, and `user_id`. The IDs are routing and audit scopes, not local
paths, account names, email addresses, token values, or private inventory.

The placement rule is deliberately narrow: stateless allowlisted upstreams are shared-worker eligible only when they require no local state. Every other upstream remains local edge.

The code contract lives in `mcp_broker.shared_runtime_policy`. It defines the
required isolation domains, validates tenant context, and returns placement
decisions without starting hosted workers or changing runtime state.

## Remote API Contract

The P3 remote API contract defines authenticated tool discovery, describe, call, status, cancellation, streaming chunks, and audit events.
It is a schema and validation contract only. The broker still has no remote
listener, no shared upstream execution, and no hosted tool-call endpoint.

The current contract remains:

```yaml
network_listener_supported: false
```

Every remote request must carry `auth_context, tenant_context, and policy_decision`
before the request shape is accepted. Tool discovery, describe, call, status,
and cancellation are the only request operations in the current contract.
Streaming chunks and audit events are the only event types.

The code contract lives in `mcp_broker.remote_api_contract`. It validates the
remote request and event shapes without opening sockets, starting workers,
calling upstream tools, or changing runtime state.

## Session Affinity And State Placement

Stateful, OAuth, browser, file-access, local-secret, and unknown upstream
classes remain local edge. Contract statement: stateful, OAuth, browser, file-access, local-secret, and unknown upstream classes remain local edge.
They bind state to the local client session and do not become shared-worker
candidates.

Only stateless upstreams that are explicitly allowlisted and require no local
state can use shared-worker placement. For that class, shared-worker state binds to tenant, workspace, user, and upstream scope.

Private inventory class labels are forbidden. Contract statement: private inventory class labels are forbidden. The policy fails closed before
producing a placement decision for those labels.

The code contract lives in `mcp_broker.session_affinity`. It makes pure
classification and state-placement decisions without routing traffic, starting
workers, calling upstream tools, or changing runtime state.

## Quota And Cost Controls

The default quota decision is deny. Contract statement: external metering is not implemented.
External metering is not implemented, and
the current contract does not connect to billing, usage collection, or cost
allocation systems.

Every shared-runtime request must pass global, team, user, upstream, and tool
scopes before it can be accepted. Contract statement: global, team, user, upstream, and tool scopes.
Quota denial is fail-closed and audit-required. Contract statement: quota denial is fail-closed and audit-required.

Kill switches are evaluated before limit counters. Contract statement: kill switches are evaluated before limit counters.
The policy can deny by global kill switch, scoped kill switch, missing quota
scope, or exhausted quota scope.

The code contract lives in `mcp_broker.quota_policy`. It returns deterministic
quota decisions from a supplied snapshot without billing calls, external
metering calls, counter writes, routing traffic, starting workers, or changing
runtime state.

## Shared Worker Runtime

P3.5 adds an in-process fake worker only. It is a proof surface for shared
runtime policy, not a hosted execution service and not a route to real upstream
MCP servers. Real upstream routing is not implemented.

Only allowlisted stateless fake tools can run in this worker proof. Contract statement: network, file-access, secret, local-state, and inherited-environment access default to deny.
Contract statement: real upstream routing is not implemented.
Contract statement: unsupported shared-worker tools are denied with audit events.

The code contract lives in `mcp_broker.shared_worker`. It validates tenant
context, session-affinity placement, quota decisions, tool allowlisting, and
default-deny sandbox capabilities before returning a deterministic fake-tool
result.

## Mandatory Non-Goals

Phase 3 does not add hosted execution. It does not add remote tool calls, remote
upstream startup, central OAuth storage, central browser state, or shared
file-access state. It does not make a future cloud service part of the default
install path.

The public repo must keep working for a single user who clones it, creates a
private config, and runs the broker locally.
