# MCP Config Migration

Use this guide to move an existing MCP client inventory into
`config/broker.private.yaml`. The import path is manual by design: the broker
does not read or copy private client files, user paths, OAuth state, or secret
values.

## Start

Create the private config and keep the original client file untouched:

```bash
make config-init
make config-validate
```

For each existing MCP server, copy only the generic server shape into
`upstreams.<name>`:

| Source field | Broker field |
| --- | --- |
| `command` | `upstreams.<name>.command` |
| `args` | `upstreams.<name>.args` |
| `env` variable names | `upstreams.<name>.env` |
| secret values | Runtime secret file under `$HOME/mcp/mcp-broker/secrets/` |
| HTTP or SSE endpoint URL | `upstreams.<name>.command` |
| stdio, HTTP, or SSE protocol | `upstreams.<name>.transport` |
| client access list | `upstreams.<name>.profiles` |

Keep secrets out of YAML. If the source config has an actual token value, move
it to the host environment or to an `env_files` entry.

## Codex

Codex uses TOML with MCP servers stored as `mcp_servers`.

```toml
[mcp_servers.example-store]
command = "npx"
args = ["-y", "@example/mcp-server"]

[mcp_servers.example-store.env]
EXAMPLE_TOKEN = "move-this-out-of-config"
```

Broker upstream:

```yaml
upstreams:
  example-store:
    enabled: true
    mode: shared
    transport: stdio
    purpose: Example imported stdio MCP.
    tags:
      - imported
      - stdio
    tool_prefix: example-store
    command: npx
    args:
      - -y
      - "@example/mcp-server"
    state_dir: upstreams/example-store
    env:
      EXAMPLE_TOKEN: EXAMPLE_TOKEN
    profiles:
      - codex
      - claude
      - agy
```

Then dry-run and apply the Codex client render:

```bash
make config-backup CLIENT=codex
make config-render CLIENT=codex CONFIG_RENDER_APPLY=0
make config-render CLIENT=codex CONFIG_RENDER_APPLY=1
```

Use `CLIENT=claude` or `CLIENT=agy` for those clients after the matching
profile validation passes. For another JSON settings client that stores MCP
servers under top-level `mcpServers`, start with:

```bash
make profile-snippet NEW_PROFILE=local-client NEW_CLIENT_FORMAT=mcp-settings-json
```

Keep `mcp_allowed_servers` in that generated block unless the target client
documents that all configured MCP servers are exposed without an allowlist.

Rollback uses the latest broker backup:

```bash
make config-rollback CLIENT=codex
```

## Claude Code

Claude Code configs expose MCP servers as `mcpServers` in JSON. Keep
non-MCP settings in the original file. Move each MCP entry into
`config/broker.private.yaml`, then render only the broker entry after the Claude
profile has passed validation and the user has approved Claude wiring.

Required checks before applying Claude wiring:

```bash
make claude-facade-smoke
make claude-profile-validation
make codex-claude-discovery-parity
```

Dry-run first:

```bash
make config-render CLIENT=claude CONFIG_RENDER_APPLY=0
```

Claude Code also loads `.mcp.json` files from the current project tree, a
home-level `.mcp.json`, and per-project MCP entries stored in the Claude JSON
config. After the broker owns those MCPs, audit the project files and Claude
project state so raw local MCP servers do not come back when Claude starts in a
different project:

```bash
make project-mcp-audit PROJECT_MCP_ROOTS="/path/to/projects"
```

To migrate covered project files, run with apply enabled. Missing entries stay
blocked unless they can be imported into `config/broker.private.yaml`:

```bash
make project-mcp-migrate PROJECT_MCP_ROOTS="/path/to/projects" PROJECT_MCP_APPLY=1
make project-mcp-migrate PROJECT_MCP_ROOTS="/path/to/projects" PROJECT_MCP_IMPORT_MISSING=1 PROJECT_MCP_APPLY=1
```

The migration command backs up changed `.mcp.json` files and the Claude JSON
config under the broker runtime backup directory before writing empty
`mcpServers` objects for covered entries. Set `PROJECT_MCP_CLAUDE_CONFIG=` to
disable Claude JSON scanning for a run.

## Claude Desktop

Claude Desktop also uses `mcpServers` JSON. Treat it as an inventory source:
copy `command`, `args`, and `env` names into broker upstream records, then keep
Claude Desktop pointed at its own config unless this repo adds an explicit
Claude Desktop renderer.

Browser auth and local state stay with the upstream MCP. Put shared auth state
under the broker runtime root when the upstream supports a configurable state
directory.

## Cursor

Cursor MCP entries use the same common fields: `mcpServers`, `command`, `args`,
and `env`. Copy each entry into an upstream record and set `profiles` based on
which clients should see the tools.

Use `mode: shared` for read-only or shared-auth upstreams. Use
`mode: per_session` when the upstream keeps unsafe process-local state.
Use `mode: per_call` when a stdio adapter must start fresh for one operation and
exit afterward. `per_call` cannot use HTTP or SSE.

## Windsurf

Windsurf MCP entries can be copied through the same JSON mapping. Keep its app
settings separate from broker config. Move only MCP server definitions into
`upstreams`.

For mutating tools, set:

```yaml
mutating: true
```

Then add that upstream to each intended profile's `allow_mutating_upstreams`
list.

## LM Studio

LM Studio MCP configuration also maps to `mcpServers` style JSON. Import stdio
servers by copying `command`, `args`, and `env` names. Import remote endpoints
with HTTP or SSE transport:

```yaml
upstreams:
  example-http:
    enabled: true
    mode: shared
    transport: http
    purpose: Example imported remote MCP endpoint.
    tags:
      - imported
      - http
    tool_prefix: example-http
    command: https://example.invalid/mcp
    state_dir: upstreams/example-http
    profiles:
      - manual-test
```

## Validate

After editing the private config:

```bash
make config-validate
make profile-validation PROFILE=codex
make tools-count PROFILE=codex
```

Run profile-specific validation for every profile you expose in the YAML. Do
not apply a client config until the broker can list and call the intended
upstreams through that profile.
