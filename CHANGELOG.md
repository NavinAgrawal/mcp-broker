# Changelog

All notable public changes will be recorded here.

## 2.1.0 - 2026-09-05

- Add caller-bound broker session closure so clients can release idle upstream
  processes without affecting another session.
- Read newline-framed broker requests across socket chunks so large Outlook
  attachment payloads reach the upstream intact.
- Keep the apps checkout as the source repository and export runtime files to
  the public checkout named by the active environment.
- Default registry publication to local credentials while repository GitHub
  Actions remain disabled.
- Synchronize release metadata through the Makefile release path.
- Harden plugin runtime bootstrap transactions with fail-closed metadata,
  runtime-path, entrypoint, archive, smoke, rollback, and status validation.
- Harden fleet collection envelopes so local status upload preparation rejects
  unsafe URLs, auth references, retention values, timestamps, collector IDs,
  non-object payload sections, local paths, account identifiers, and secret-like
  values before any collection artifact is produced.
- Add governed changed-file mutation selection for interactive macOS release
  gates, while preserving the full public mutation universe in `setup.cfg`.

## 2.0.0 - 2026-06-27

- BREAKING: the project moved from the personal `NavinAgrawal`/`navinagrawal`
  identity to the Doric Stack LLC org `doricstack`. Install coordinates change:
  npm `@navinagrawal/mcp-broker` becomes `@doricstack/mcp-broker`, the container
  image namespace becomes `doricstack`/`ghcr.io/doricstack`, the Homebrew tap
  becomes `doricstack/tap` (`brew install doricstack/tap/mcp-broker`), and the MCP
  Registry name becomes `io.github.doricstack/mcp-broker`. The PyPI project name
  `mcp-broker` is unchanged. The old `@navinagrawal` package is deprecated and
  points here. The GitHub repo redirects from the old path automatically.
- BREAKING: `broker_search_tools` no longer returns each tool's `inputSchema` in
  search results.
  Search exists to pick a tool, which only needs the name, description, upstream,
  purpose, tags, and mutating flag; the exact schema is the single heaviest field
  and is still returned on demand by `broker_describe_tool` right before a call.
  In a local measurement a 50-result search dropped from roughly 68,000 to 2,500
  bytes (about 96 percent smaller) with no loss of discovery signal, since
  relevance scoring already ran on the full entry before the result was built.
- `broker_call_tool` accepts an optional `projection` argument
  (`{"paths": ["data.items.id"], "max_array_items": 5}`) that trims the upstream
  response server-side before it reaches the client. Dotted paths keep only the
  fields you name (a path that reaches a list is applied to every element) and
  `max_array_items` caps long arrays. Omitting `projection` returns the full,
  unmodified response, so existing callers are unaffected. A verbose 50-item
  response dropped from roughly 19,800 to 200 bytes (about 99 percent smaller) in
  local measurement. Both changes reduce context tokens and end-to-end latency.

## 1.4.1 - 2026-06-10

- Rank `broker_search_tools` results by relevance instead of requiring every
  query token to match. Natural-language queries such as
  `fly app status logs ssh postgres deploy` now return the best-matching
  upstreams ordered by score, weighting name and tag matches over purpose and
  description matches, rather than returning an empty list when no single tool
  contains all tokens.

## 1.4.0 - 2026-06-10

- Add an idle-upstream janitor that periodically reaps stdio upstreams left idle
  past their configured timeout, guarded by a registry lock and an identity
  check so an upstream reused between sweeps is not evicted mid-use.
- Harden open-file-descriptor limits across the macOS LaunchAgent, the Linux
  systemd user service, and the Docker entrypoint, driven by
  `BROKER_MAX_OPEN_FILES` (default 8192), to prevent file-descriptor exhaustion
  under many concurrent client sessions.
- Add structural upstream-client Protocols so the daemon's stdio and HTTP
  upstream registries are statically typed.
- Record source provenance (git SHA and source path) in the `daemon.started`
  structured log event.

## 1.3.1 - 2026-06-07

- Synchronize release metadata through the Makefile release path.

## 1.3.0 - 2026-06-06

- Add the production `mcp-broker` brand asset kit, README header, and branding
  rules contract for public repo surfaces.
- Add client cwd based profile routing so a broker request can route to the
  profile that owns the active project root.
- Add runtime secrets sync before broker startup so LaunchAgent-owned daemons
  can read configured secret files without inheriting shell environment.
- Filter selected broker catalog listing by upstream metadata and tool prefix
  so targeted broker searches avoid listing irrelevant slow upstreams.

## 1.2.0 - 2026-05-29

- Add per-tool upstream call timeouts so long-running brokered tools can get
  more time without loosening the default timeout for every tool on an upstream.

## 1.1.3 - 2026-05-28

- Synchronize release metadata through the Makefile release path.

## 1.1.2 - 2026-05-28

- Add source-backed broker facade tool descriptions and schemas for Glama,
  Smithery, Claude Desktop MCPB, and other directory scanners.
- Add `make release` as the CI release transaction so release publication runs
  one version-aware target before publishing all registry surfaces.
- Add `make release-check RELEASE_VERSION=<semver>` as the local pre-push
  release gate for version alignment, package checks, directory metadata, MCPB,
  and Smithery payload validation.

## 1.1.1 - 2026-05-27

- Fix MCPB and Claude Desktop stdio startup when the broker daemon is already
  running.
- Add a Smithery MCPB release adapter that preserves the valid Claude Desktop
  manifest while publishing Smithery server-card tool schemas.
- Add a configurable MCPB `UVX command path` for GUI environments that cannot
  find `uvx` from shell PATH.

## 1.1.0 - 2026-05-26

- Add the one-shot `make publish-everywhere` CI release path for PyPI, NPM,
  Docker Hub, GHCR, and MCP Registry publication.
- Add the scoped NPM bridge package at `${NPM_PACKAGE_NAME}`.
- Add Docker Hub primary and GHCR mirror publication contracts for the Docker
  image release path.

## 1.0.0 - 2026-05-26

- Promote the public release line from pre-1.0 packages to the first stable
  release.
- Align package, registry, MCPB, server-card, Homebrew, and public release
  documentation on `1.0.0`.
- Keep `0.1.x` entries as pre-1.0 public history instead of deleting published
  artifacts.

## 0.1.2 - 2026-05-26

- Harden mutation-gate coverage for configuration, profile validation, project
  MCP import, daemon helpers, upstream lifecycle, and runtime cleanup paths.
- Remove mutation skip comments and require zero survived, untested, skipped,
  suspicious, timeout, interrupted, segfault, or unchecked mutants for release.
- Keep public export and maintainer release gates green with 100% coverage and
  100% mutation score.

## 0.1.1 - 2026-05-25

- Correct MCP Registry namespace casing in package README, registry metadata,
  and server card so GitHub OIDC ownership validation matches PyPI metadata.

## 0.1.0 - 2026-05-25

- Local MCP broker daemon with a compact MCP facade.
- Stdio client shim for Codex and Claude Code.
- Strict YAML config contract with JSON Schema validation.
- Profile-scoped upstream exposure and mutating-tool allowlists.
- Shared and per-session upstream process management.
- Runtime state under the user's broker runtime root, outside the repo.
- macOS LaunchAgent, Linux systemd user service, and Windows Scheduled Task
  install flows with dry-run defaults.
- Public smoke, quality, release, and export gates.
- Context reduction evidence in `docs/context-reduction-measurement.md`.
