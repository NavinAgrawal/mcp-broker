# Adoption Guide

Use this guide if you use Codex, Claude, Cursor, or another MCP client and have too many MCP tools loaded in every session.

## Target State

Each client gets one local MCP entry:

```text
mcp-broker
```

The broker exposes the compact broker facade:

```text
broker_search_tools
broker_describe_tool
broker_call_tool
broker_status
broker_close_session
```

Raw upstream tools stay behind the broker until a task needs them.

## Clone-To-Running Path

Use this path for a public clone on a personal machine or an enterprise laptop.
It starts with the public repo, creates a private config, proves the broker can
run, then writes client config only after an explicit apply flag.

```bash
GITHUB_REPOSITORY_URL="${GITHUB_REPOSITORY_URL}"
git clone "$GITHUB_REPOSITORY_URL" mcp-broker
cd mcp-broker
make setup
make config-init
```

Add one upstream to `config/broker.private.yaml`. Use the public example config
as the contract. Keep local paths, account names, and secrets out of git. Secret
values belong in environment variables or broker-owned runtime secret files, not
in YAML.

```bash
make config-validate
make broker-smoke
make profile-validation PROFILE=codex
make config-backup CLIENT=codex
make config-render CLIENT=codex CONFIG_RENDER_APPLY=0
make broker-status
```

No client config is written before `CONFIG_RENDER_APPLY=1`. Review the rendered
client config under the broker runtime render directory first, then apply:

```bash
make config-render CLIENT=codex CONFIG_RENDER_APPLY=1
```

Rollback stays broker-owned:

```bash
make config-rollback CLIENT=codex
```

Use `CLIENT=claude` or `CLIENT=agy` only after that profile passes validation
and the user intends to wire that client.

## Migration Path

1. Run setup.

```bash
make setup
```

2. Create the private config.

```bash
make config-init
```

3. Add upstream MCPs to `config/broker.private.yaml`.

Use `config/broker.example.yaml` as the contract. Keep local paths, account names, token values, and OAuth state out of git.

4. Validate the config.

```bash
make config-validate
```

5. Run broker smoke.

```bash
make broker-smoke
```

6. Validate one profile.

```bash
make profile-validation PROFILE=codex
```

7. Dry-run client config rendering.

```bash
make config-render CLIENT=codex CONFIG_RENDER_APPLY=0
```

8. Apply after review.

```bash
make config-render CLIENT=codex CONFIG_RENDER_APPLY=1
```

Use the same flow for `CLIENT=claude` or `CLIENT=agy` after that profile
passes validation. For another JSON settings client, generate a starter block:

```bash
make profile-snippet NEW_PROFILE=local-client NEW_CLIENT_FORMAT=mcp-settings-json
```

Keep the generated `mcp_allowed_servers` setting for clients that require an
explicit MCP server allowlist before tools appear in model sessions.

## Profile Shape

Start with compact mode:

```yaml
profiles:
  codex:
    compact_tools_enabled: true
    max_tools: 8
    expose_upstreams:
      - example-store
```

Expose only the upstreams that the profile should use. Do not make every upstream visible to every client by default.

## Shared Versus Per-Session

Use `shared` for read-only upstreams or shared-account tools that can tolerate one process.

Use `shared` plus `serialize_calls: true` for shared SaaS, notes, or write-capable tools where concurrent calls can collide.

Use `per_session` for browser automation, filesystem roots, databases, cloud deploy tools, and project-specific state.

## Validate From The Client

Codex and Claude `/mcp` views show the broker entry, not every hidden upstream. That is expected.

Use `broker_status` for upstream state and auth visibility. Use `broker_search_tools` to confirm discovery, then `broker_describe_tool` and a configured safe `broker_call_tool` smoke probe before trusting an upstream in normal work.

## Rollback

Every apply path writes a broker-owned backup before changing a client config.

```bash
make config-rollback CLIENT=codex
```

Run rollback before editing client config by hand. That keeps the broker's backup chain usable.

## Layered Config Dry Run

For team or enterprise setup, compose layers before rendering client config:

```bash
mcp-broker config compose \
  --org org.yaml \
  --team team.yaml \
  --addon audit.yaml \
  --user user.yaml
```

The command prints the effective config, SHA-256 digest, layer order,
provenance, and conflicts. It does not write runtime state or client config.

Use layers this way:

- org: required defaults, approved clients, shared policy
- team: team-level upstream exposure and tool budgets
- add-on: optional controls such as audit policy or extra catalog entries
- user: final local preferences

Secret values stay out of layer files. A layer may reference an environment
variable name:

```yaml
env:
  GITHUB_TOKEN:
    secret_ref: GITHUB_TOKEN
```

Do not place token values, passwords, API keys, OAuth state, account names, or
private filesystem paths in published layers.
