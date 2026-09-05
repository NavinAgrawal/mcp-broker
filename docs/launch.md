# Launch Notes

`mcp-broker` solves one narrow problem: local MCP users should not pay the full
context cost of every configured upstream before the task needs those tools.

The public release centers on a small broker facade:

```text
broker_search_tools
broker_describe_tool
broker_call_tool
broker_status
broker_close_session
```

The broker keeps the upstream MCP servers available, but it exposes them through
profile gates and namespace routing instead of loading every upstream tool
definition into every new client session.

## Measured Result

The May 24, 2026 measurement compared a direct Codex MCP setup with the broker
facade plus a pruned hosted app cache.

| Surface | Before | After | Reduction |
|---|---:|---:|---:|
| Direct Codex MCP server entries | 11 | 1 | 90.91% |
| MCP tool definitions | 414 | 4 | 99.03% |
| Hosted `codex_apps` tool definitions | 195 | 39 | 80.00% |
| Combined always-loaded tool definitions | 609 | 43 | 92.94% |
| Combined serialized tool payload bytes | 1,026,171 | 185,877 | 81.89% |
| Combined `o200k_base` tool tokens | 276,989 | 45,281 | 83.65% |

The count reduction is 609 to 43 advertised tool definitions. The token
reduction is 276,989 to 45,281 serialized `o200k_base` tool tokens.

The detailed measurement is in
[docs/context-reduction-measurement.md](context-reduction-measurement.md).

## Release Positioning

This is local developer infrastructure. It is not a hosted gateway, marketplace,
policy plane, or package registry.

Use it when:

- a local Codex, Claude, Cursor, or similar client loads too many MCP tools
- several clients repeat the same local MCP configuration
- shared upstream process ownership matters
- OAuth state, browser state, sockets, logs, and cleanup need one local home

Do not use it as a replacement for a hosted enterprise gateway or a managed MCP
marketplace.

## First Demo Script

```bash
pipx install mcp-broker
mcp-broker init
mcp-broker render codex --dry-run
mcp-broker status
```

Then show one client configured with a single `mcp-broker` entry and the five
broker tools listed above.

## Links

- README: `README.md`
- Install: `docs/install.md`
- Safety: `docs/safety.md`
- Distribution: `docs/distribution.md`
- Comparison: `docs/comparison.md`
