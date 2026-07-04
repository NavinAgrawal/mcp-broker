# Phase Foundation Roadmap

This roadmap describes the public product sequence for plugin setup, enterprise
governance, and shared-runtime buildout. Private local upstream inventory,
operator notes, and maintainer task history stay out of this document.

## Current Position

`mcp-broker` is a local edge broker. It runs on the engineer's machine, reads
local YAML config, exposes one compact MCP server to clients, and routes calls to
configured upstream MCP servers. That remains the default execution boundary.

The next work adds contracts around that local runtime so the same repo can serve
individual users, small teams, and larger enterprises. Shared runtime work is in
scope for Phase 3, but local execution remains the default until the isolation,
quota, audit, rollback, and E2E gates pass.

## Release Reliability Blocker

Before the next public release, harden the Publish Everywhere transaction.
Current state verified on 2026-07-04: GitHub Release `v${PACKAGE_VERSION}` has
been recovered, and PyPI, npm, Docker Hub, Docker Registry, MCP Registry,
Homebrew, and the GitHub Release verify live. GHCR remains blocked because
`${GHCR_REPOSITORY_IMAGE}:${PACKAGE_VERSION}` is not anonymously readable and
the current local GitHub token lacks package scopes. The required fix before
the next public release is a transaction-safe release path with per-registry
preflight, idempotent reruns, live verification, and GHCR visibility or scope
resolution. Idempotent reruns must skip a surface only after verifying registry
metadata: PyPI artifact digests, NPM package integrity, MCP Registry
name/version metadata, and Homebrew formula SHA.

## Phase 1: Plugin And Local Deployment Foundation

- [x] Add a config-backed broker identity/status contract with `broker_id`,
      `environment`, `bundle_version`, `schema_version`, active profile, and
      configured profile set.
- [x] Define a desired-state bundle schema for profiles, upstream catalog
      entries, client render targets, policy, compatibility, and provenance.
- [x] Add a local bundle validator and loader that rejects invalid schema,
      version, checksum, and compatibility input without changing runtime state.
- [x] Add transactional deployment state under the runtime state directory,
      including active and previous pointers, rollback journal, and recovery from
      partial writes.
- [x] Add a plugin package contract with repo-owned install, status, dry-run,
      apply, rollback, and smoke checks.
- [x] Make the public clone-to-running path work through `make setup`, config
      initialization, profile validation, smoke checks, client render, rollback,
      and troubleshooting.
- [x] Add installed runtime manifests with active and previous pointers so
      plugin setup does not depend on a developer checkout path.
- [x] Add a plugin-owned launcher that resolves the active installed runtime.
- [x] Add runtime artifact metadata, digest verification, archive safety checks,
      and fail-closed activation.
- [x] Add approval-gated bootstrap preflight, plan, apply, status, rollback, and
      uninstall transactions.
- [x] Add layered configuration composition with deterministic merge, digest,
      provenance, and secret-reference validation.
- [x] Add break-glass audit records and degraded status.
- [x] Add cross-platform bootstrap tests for macOS, Linux, and Windows service
      setup without mutating host state by default.

## Phase 2: Governance Control Plane

Completed baseline contracts prove the local-only governance shape. The current
implementation queue begins with signed publishing, then assignment, pull/apply,
status collection, rollout control, approvals, and a reference control plane.

- [x] Split desired state into publishable profile, upstream-catalog, policy, rollout, and compatibility
      bundle documents. Contract: `docs/governance-control-plane.md`.
- [x] Add a redacted fleet-status export that reports broker identity, bundle
      version, health, counters, and upstream state without local paths, account
      names, secret values, or private inventory.
- [x] Add an offline control-plane simulator for canary, staged rollout,
      rollback, compatibility rejection, and approval decisions.
- [x] Add signed bundle publishing contracts.
- [x] Add assignment-source contracts for brokers, users, teams, channels, and
      rollout rings.
- [x] Add broker pull/apply protocol for authenticated fetch, cache,
      compatibility check, local approval, apply, and rollback.
- [x] Add fleet-status collection with auth, retention, failure handling, and
      no-secret validation.
- [ ] Add a rollout controller that turns simulator decisions into auditable
      actions.
- [ ] Add operator approval workflow for mutating rollout, rollback, policy
      override, and break-glass.
- [ ] Add a minimal reference control plane that exercises governance without
      centralizing tool execution.

Phase 2 build order is publish, assign, pull/apply, collect, control rollout,
record approvals, then run the reference control plane. That order keeps
unsigned state and unapproved mutation out of the broker path.

## Phase 3: Shared Runtime Build Sequence

Completed baseline guardrails document the gates. The current implementation
queue begins with tenant policy and remote API contracts, then state placement,
quotas, worker isolation, distributed state, hybrid routing, and E2E proof.

- [x] Document that shared hosted execution is not implemented. Contract:
      `docs/shared-runtime-guardrails.md`.
- [x] Require Phase 1 value proof and Phase 2 governance proof before designing
      a shared execution runtime.
- [x] Define the decision gates for tenant isolation, authorization, quotas,
      session affinity, distributed state, cost controls, audit, and failure
      domains.
- [ ] Add shared-runtime threat model and tenant model.
- [ ] Add remote broker API contract for authenticated discovery, describe,
      call, status, cancellation, streaming, and audit.
- [ ] Add session affinity and state-placement rules.
- [ ] Add quota and cost-control engine.
- [ ] Add isolated shared worker runtime for allowlisted stateless upstreams.
- [ ] Add distributed deployment state with locking, conflict handling,
      rollback, recovery, and audit.
- [ ] Add hybrid routing between local edge tools and shared workers.
- [ ] Add shared-runtime E2E proof for tenant isolation, authz denial, quota
      denial, session affinity, audit, rollback, and degraded mode.

Phase 3 build order is threat model, remote API, state placement, quota control,
shared worker isolation, distributed state, hybrid routing, then E2E proof.
Hosted execution remains unsupported until that proof passes.

## Non-Goals

- Do not move default execution away from the local machine.
- Do not add a remote listener without enforcing `broker.remote_auth` before
  request handling.
- Do not publish local private upstream inventory, account names, filesystem
  paths, or runtime state.
- Do not route any tool to shared hosted execution until the Phase 3 isolation,
  authorization, quota, audit, and rollback gates are implemented and tested.
