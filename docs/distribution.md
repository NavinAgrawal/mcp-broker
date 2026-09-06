# Distribution

This page tracks public distribution paths for `mcp-broker`.

## Python Package

Package metadata is release-aligned by `make release-version-sync`. The release
version is supplied through `RELEASE_VERSION=<semver>` or
`RELEASE_BUMP=patch|minor|major`; package metadata, registry metadata, MCPB
metadata, and Docker catalog metadata are synchronized from that input.

Current public package status:

- PyPI: `${PYPI_PROJECT_NAME} ${PACKAGE_VERSION}` is published by the release transaction.
- MCP Registry: `${MCP_REGISTRY_NAME} ${PACKAGE_VERSION}` is published and marked latest by the release transaction.
- Homebrew: `${HOMEBREW_FORMULA_REF} ${PACKAGE_VERSION}` is published through the public tap.
- NPM: `${NPM_PACKAGE_NAME} ${PACKAGE_VERSION}` is published by the release transaction.
- Docker: `${DOCKER_REPOSITORY_IMAGE}:${PACKAGE_VERSION}` is published by the
  release transaction. `${GHCR_REPOSITORY_IMAGE}:${PACKAGE_VERSION}` is a
  claimed mirror surface, but the 2026-07-04 live audit still fails anonymous
  GHCR manifest verification.
- Current source release: GitHub Release `v${PACKAGE_VERSION}` was recovered on
  2026-07-04 and points at the public repository main commit that contains the
  release-idempotency hardening.

The package command surface is:

```bash
mcp-broker init
mcp-broker stdio
mcp-broker start
mcp-broker status
mcp-broker render codex --dry-run
```

The install command is:

```bash
pipx install mcp-broker
```

`pyproject.toml` exposes `mcp-broker`, `mcp-broker-client`, and
`mcp-broker-daemon`. It also packages `config/broker.example.yaml` as shared
package data so `mcp-broker init` can create a private config outside a source
checkout.

`uv` uses the same package:

```bash
uv tool install mcp-broker
uvx mcp-broker status
```

Repository-owned package checks:

```bash
make package-check
```

The public artifact gate downloads into a temporary directory and verifies the
same package surfaces a user receives:

```bash
make public-stable-surface-smoke
```

That stable gate verifies PyPI, `pipx`, `uvx`, GitHub release source archive,
Homebrew, and MCP Registry for the currently published stable version.

After each distribution release, the full release surface gate is:

```bash
make public-release-surface-smoke
```

That release gate adds NPM and Docker checks and must pass before any directory
submission claims those surfaces are live.

The local release transaction runs public live verification:

```bash
make public-release-live-verify
```

That verifier checks public registry APIs and anonymous image pull manifests for
the intended version. It fails when a registry accepts a publisher write but a
normal public user cannot see the version.

Local publication is the default. GitHub Actions stays disabled. Run the
one-shot transaction from a release worktree:

```bash
make release RELEASE_APPLY=1
```

`make release` runs `make release-check` once, then calls
`make publish-everywhere` with preflight reuse enabled. The lower-level target
remains available for retry recovery. The dormant workflow file is not a
release path while repository Actions are disabled.

`make release-check RELEASE_VERSION=<semver>` is the local pre-push contract.
It refuses to run without an explicit version unless GitHub Actions supplied a
`v<semver>` release ref. It verifies version alignment, runs the publish
preflight, and checks directory, MCPB, and Smithery metadata before a release
tag or GitHub release is created.

The release transaction validates local tokens, GHCR package visibility, and
Docker Hub repository visibility before the first registry write, publishes
PyPI, then fans out NPM, Docker Hub, GHCR, MCP Registry metadata, and the
Homebrew tap formula. The fan-out writes a JSON ledger to
`var/quality/release/publish-everywhere-ledger.json` with each registry child
command, exit code, and status. A registry child command failure is not treated
as the final release truth by itself, because some registries can accept a write
and still return a non-zero command status. Public live verification runs after
the fan-out and decides whether the release can continue.

The GitHub Release is created only after registry verification passes, then the
release object is verified through the GitHub API. If the fan-out partly
published a version and stopped before GitHub release creation, rerun the same
local `publish-everywhere` command for the same version. Already-published registry
surfaces are skipped only after `scripts/release_idempotency.py` verifies the
registry metadata for that surface. PyPI compares every local dist artifact
SHA-256 digest with the PyPI release JSON, NPM compares the local dry-run pack
integrity with registry metadata, and MCP Registry requires matching
name/version metadata before it skips. A digest mismatch or malformed registry
payload fails closed before the target reports OK. Public live verification then
proves the public state, and the GitHub release step recovers the missing
`v<version>` release when all registry surfaces agree. Tag pushes do not
publish. There are no per-registry publish workflows.

The Makefile validates required publication environment before the first
registry write. For local publication, `DOCKERHUB_USERNAME`,
`DOCKERHUB_TOKEN`, and `UV_PUBLISH_TOKEN` must exist before PyPI publication
starts. NPM uses the authenticated local npm configuration. Homebrew and MCP
Registry use the authenticated local `gh` session, which must also be able to
read GitHub Packages metadata so the command can fail closed if the GHCR package
is not public. Local MCP Registry publication passes the `gh` token through the
publisher environment, so it does not open an interactive device-login flow.
Before the multi-platform image build, local publication signs in to the
configured Docker Hub and GHCR hosts with `docker login --password-stdin`.
Docker Hub uses `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN`; GHCR uses the
authenticated local `gh` account and token. Neither token is placed in command
arguments.
`DOCKERHUB_USERNAME`
and `DOCKERHUB_TOKEN` are also used by `make docker-hub-public-ensure` so the
Docker Hub repository is public before the image push and before PyPI can be
written.

The orchestrator is retry-aware for partially completed releases. It checks the
PyPI artifact digests, NPM package integrity, MCP Registry name/version
metadata, and Homebrew formula state before submitting, so a rerun can recover
after one registry fails without treating already-published verified surfaces as
fatal.

Before tagging a release, synchronize the version and run the release check:

```bash
make release-version-resolve RELEASE_BUMP=patch
make release-version-sync RELEASE_BUMP=patch
make release-check RELEASE_VERSION=<semver>
```

That target includes `make release-gate`, so the dependency refresh, coverage,
package checks, release smoke, and mutation run in the release preflight.
Mutation evidence is written under `var/quality/mutation_stats.json`.

The Linux parity gate remains available for public portability checks:

```bash
make linux-release-gate
```

That target runs the release gate inside a Linux container with
`GITHUB_ACTIONS`, `RUNNER_TEMP`, `HOME`, and `XDG_CONFIG_HOME` set to runner-like
values.

## Homebrew

Homebrew is published through:

```bash
brew tap ${HOMEBREW_TAP_REF}
brew install mcp-broker
```

The formula installs the same console scripts, leaves user client configs
untouched during install, and preserves the runtime root contract:

```text
$HOME/mcp/mcp-broker/
```

The public tap points to the PyPI source artifact for `${PACKAGE_VERSION}`.
Future releases update the formula through `make publish-everywhere` with the
local `gh` token. `HOMEBREW_TAP_TOKEN` remains an explicit override.

## NPM

NPM is an optional bridge package. It is for users who expect an `npx` command,
but the Python package remains the runtime source of truth.

The NPM package name is:

```text
${NPM_PACKAGE_NAME}
```

Do not publish the unscoped `mcp-broker` package name on NPM. That name already
belongs to a different project.

Local NPM publication uses the authenticated npm configuration verified by
`npm whoami` and does not request Actions provenance.

Details live in `docs/npm-distribution.md`.

## MCP Registry

The official MCP Registry uses `server.json` metadata and `mcp-publisher`.
The registry is in preview, so validate against the current schema before
publishing. This repo has two metadata files:

```text
registry/server.json
registry/server.template.json
```

The official metadata points to the PyPI package path. The template stays
generic for downstream forks.

Before publishing locally:

- Publish the `mcp-broker` package to PyPI.
- Confirm the PyPI package README contains `mcp-name: ${MCP_REGISTRY_NAME}`.
- Confirm `registry/server.json` and the PyPI package version match.
- Confirm `gh auth status` passes for the public GitHub organization. The
  publisher authenticates through the interactive `mcp-publisher login github`
  flow. No GitHub token is placed in process arguments.
- Run the local one-shot release transaction after PyPI metadata is public.

CI mode can use GitHub OIDC when repository Actions are enabled:

```bash
mcp-publisher login github-oidc
cp registry/server.json server.json
mcp-publisher publish
```

PyPI package must exist first. The MCP Registry validates that the public
package matches the server metadata before accepting the entry.

The MCP Registry publication runs after PyPI publication and marks
`${PACKAGE_VERSION}` as the latest entry.

Reference docs:

- https://modelcontextprotocol.io/registry/about
- https://modelcontextprotocol.io/registry/authentication
- https://modelcontextprotocol.io/registry/package-types
- https://modelcontextprotocol.io/registry/quickstart
- https://modelcontextprotocol.io/registry/versioning

## Docker And OCI

Docker mode is not the default local desktop experience. It is useful for
container-friendly upstreams and remote transports. The Docker image does not
edit host client files by default.

Build and smoke locally:

```bash
make docker-smoke
```

Build a release image with OCI labels, SBOM, and provenance:

```bash
make docker-buildx \
  DOCKER_IMAGE=${DOCKER_REPOSITORY_IMAGE}:${PACKAGE_VERSION} \
  DOCKER_PLATFORMS=linux/amd64,linux/arm64 \
  DOCKER_PUSH=1
```

Local publication creates and selects the configured `docker-container`
release builder when it is absent. Docker's default driver with the classic
image store cannot emit the required attestations.

Docker Hub is the primary image for Docker MCP Catalog work:

```text
${DOCKER_REPOSITORY_IMAGE}
```

Before the publish fan-out, `make docker-hub-public-ensure` authenticates to
Docker Hub, creates the repository as public when it is missing, attempts to
flip an existing private repository to public through the Docker Hub API, and
then verifies anonymous repository visibility. If Docker Hub refuses the
visibility update, the release blocks before PyPI publication instead of
creating a partially public release.

GHCR is a mirror only when it is anonymously readable:

```text
${GHCR_REPOSITORY_IMAGE}
```

If organization policy keeps GHCR packages private, do not claim GHCR as a
public release surface. The publish workflow must either verify the package is
public before the first write or remove GHCR from the public surface contract in
the same change.

Recommended release tags for `${PACKAGE_VERSION}`:

```text
${PACKAGE_VERSION}
${PACKAGE_MINOR_VERSION}
```

Do not publish `latest` until the maintainer confirms the tag should track the
newest stable release.

For a local one-platform buildx check without pushing:

```bash
make docker-buildx DOCKER_PLATFORMS=linux/arm64
```

Docker MCP Toolkit custom catalog smoke uses file-based server metadata:

```bash
make docker-mcp-catalog-smoke
```

The metadata file lives at:

```text
docker/mcp-catalog/mcp-broker.yaml
```

The Docker image itself is not treated as self-describing for Docker MCP
Toolkit. Docker's file-based catalog metadata path is the local validation path
until the official Docker registry review decides whether Docker builds the
catalog image or accepts the self-provided Docker Hub image.

Run manually:

```bash
docker build -t mcp-broker:local .
docker run --rm -i mcp-broker:local
```

The image entrypoint calls the package-owned stdio lifecycle:

```bash
mcp-broker stdio --init-if-missing
```

Boundary:

- Supported: HTTP, streamable HTTP, SSE, and stdio upstreams that run inside
  the container.
- Supported: explicit mounts for runtime state, config, logs, and secrets.
- Unsupported by default: hidden edits to host `~/.codex`, `~/.claude.json`, or
  browser profiles.
- Completed before Docker MCP Catalog PR approval: public image publication and
  Docker-specific security review. Local Docker MCP Toolkit custom catalog smoke
  is covered by `make docker-mcp-catalog-smoke`.

Docker MCP Catalog submission uses the Docker registry PR flow after the
public repo contains the Dockerfile. The staged PR packet is
`docs/docker-mcp-registry-submission.md`.

## MCPB, Smithery, Glama, PulseMCP, And Directories

Use the clean public GitHub repo as the source for indexers. Submit after the
README, safety docs, package install path, and registry metadata are stable.

MCPB metadata lives in:

```text
mcpb/manifest.json
```

Validate it with:

```bash
make mcpb-validate
make mcpb-pack
make mcpb-smoke
make mcpb-stdio-smoke
make smithery-payload-check
make directory-submission-check
```

Smithery uses the local MCPB path for this release. The hosted or remote path
waits until `mcp-broker` has a real streamable HTTP broker mode. The MCPB
manifest stays valid for Claude Desktop with rich descriptions only; MCPB does
not allow tool `inputSchema` fields. `make smithery-publish` sends a
Smithery-specific server-card payload and injects the source-backed broker
facade schemas for `broker_search_tools`, `broker_describe_tool`,
`broker_call_tool`, and `broker_status`. The first accepted Smithery release
returned deployment `${SMITHERY_RELEASE_ID}` and MCP URL
`${SMITHERY_MCP_URL}`; public search indexing may lag.

Glama lists the public repo at
`${GLAMA_LISTING_URL}`. PulseMCP has also appeared from the registry/server.json surface at `${PULSEMCP_LISTING_URL}`. Check that tool names, schemas,
install instructions, safety notes, license, GitHub links, and score output
render correctly before adding secondary directories. The root `glama.json`
keeps Glama claim metadata public and points maintainer ownership to
`${GLAMA_MAINTAINER}`.

Directory copy lives in:

```text
docs/directory-submission-packet.md
```

Before any directory submission, run `make directory-submission-check`. It
validates the packet, `/.well-known/mcp/server-card.json`,
`registry/server.json`, `glama.json`, and the MCPB manifest together so
directory pages cannot drift from the package metadata.

The public launch page lives in:

```text
docs/launch.md
```
