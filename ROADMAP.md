# Roadmap

This roadmap is product-facing. Private agent task history and local upstream inventory stay out of the public repo.

## 0.1.0

- Local broker daemon over a Unix socket.
- Stdio client shim for MCP clients.
- Compact broker facade with search, describe, call, and status tools.
- YAML config contract with JSON Schema validation.
- Profile gates for MCP client profiles, including Codex, Claude, AGY, and manual test flows.
- macOS LaunchAgent, Linux systemd user service, and Windows Scheduled Task install flows.
- CLI-first package commands: `mcp-broker init`, `mcp-broker start`, `mcp-broker status`, and `mcp-broker render`.
- Public quality gates, release smoke, and context-reduction measurement.
- Private-To-Public Export pipeline for creating a clean public checkout from the private working repo.
- Public distribution, GitHub publication, and community launch checklists.

## Next

- Execute the foundation roadmap in `docs/phase-foundation-roadmap.md`: Phase 1
  plugin/local deployment contracts, Phase 2 governance contracts, and Phase 3
  shared-runtime guardrails. Phase 3 guardrails are defined in
  `docs/shared-runtime-guardrails.md`, with P3.8 E2E proof covering tenant
  isolation, authz denial, quota denial, session affinity, audit, rollback,
  degraded mode, local-only routing, and shared-eligible routing while hosted
  execution remains unsupported.
- Keep package, Homebrew, MCP Registry, Docker, and MCPB metadata aligned for each release.
- Publish Docker images after namespace, tag, SBOM, provenance, and catalog-review choices are confirmed.
- Complete directory listings after account-level review: Docker MCP Catalog, Smithery, Glama, PulseMCP, and secondary MCP indexes.
- Expand provider-specific passive auth probes where upstream token formats are stable and public.
- Add more migration helpers for users importing existing MCP configs from other clients.
- Add an authenticated remote broker mode only after the local Unix-socket security contract stays the default and remote auth is enforced before request handling.
- Add optional cache policy for read-only tool results, with per-upstream TTLs and no caching for mutating upstreams.
- Add metrics export beyond `broker-status.json`, likely Prometheus or OpenTelemetry, without requiring those dependencies for local default use.
- Document the socket and JSON-RPC contract well enough for non-Python helper clients or SDKs.
- Track WebSocket only as a future custom transport extension, opened when a
  real MCP compatibility fixture and framing contract exist.
- Keep context-reduction measurements current as clients add or change connector surfaces.

## Public Export

The first public repo must come from the export pipeline, not by changing visibility on the private repo. The public checkout should contain only allowlisted source, tests, docs, config examples, and release files.

Before first public push:

- Run `make release-gate` from a clean checkout before publishing a release.
- Review root docs, issue templates, visual assets, and release notes.
- Confirm no private docs, private inventory, runtime state, generated reports, or private git history are present.
- Run the public test gate from the exported checkout.
