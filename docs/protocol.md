# mcp-broker Protocol

## Client Side

Rendered MCP clients see one MCP server: `mcp-broker`.

The client shim speaks normal stdio MCP to the LLM client. Internally it forwards requests to the broker over a Unix socket.

This means the client-facing transport is stdio even when an upstream MCP is
remote HTTP. Local stdio clients launch `mcp-broker-client` as a local command;
the broker then handles each upstream's configured transport.

## Broker Side

The broker must support:
- `initialize`
- `notifications/initialized`
- `tools/list`
- `tools/call`

The protocol core currently supports JSON-RPC 2.0 single-request envelopes and
rejects JSON-RPC batches. MCP initialize negotiation supports these protocol
versions:

- `2025-11-25`
- `2025-06-18`
- `2025-03-26`
- `2024-11-05`

The broker returns the client's requested version when it is in that supported
set. Unsupported versions return JSON-RPC error `-32602` with
`Unsupported protocol version`.

The daemon listens on the configured Unix socket and exposes broker control
methods for local management:

- `broker/health`
- `broker/stop`

These control methods are for the local daemon and Makefile targets. The Codex
and Claude shim will still speak MCP over stdio after the client shim task is
implemented.

`broker/health` returns daemon PID, socket path, requested profile, and one
status record per configured upstream. Each upstream record includes state, PID,
process-group CPU percent, process-group memory MB when macOS reports it,
restart count, and last error.

`broker/stop` enters shutdown, stops each running stdio upstream process group,
verifies broker-owned process groups have no remaining members, and returns the
stopped upstream names plus any remaining broker-owned process IDs.

The broker does not expose a remote listener today. The public config includes
`broker.remote_auth` as the required auth policy for any future remote broker
transport. `broker.remote_auth.required` must stay `true`, and
`broker.remote_auth.enabled` cannot be used without a token source from
`token_env` or `token_file`. A remote listener must not be added until it checks
that policy before accepting requests.

## Remote Broker API Contract

The Phase 3 remote broker API is contract-only. It does not expose a network listener, does not start hosted workers, and does not call upstream tools.

The current contract is:

```yaml
network_listener_supported: false
```

Future remote requests must carry `auth_context, tenant_context, and policy_decision`
before they can be accepted. The request operations are tool discovery, describe, call, status, cancellation. Event contracts cover streaming chunks and audit events.

The code contract lives in `mcp_broker.remote_api_contract`. It validates
request and event payload shape only, so this protocol section does not change
the local stdio client, Unix socket daemon, or upstream transport behavior.

## Upstream Transports

Configured upstreams can use:

- `stdio`: a local command that speaks MCP over stdin/stdout.
- `http`: MCP Streamable HTTP, using JSON-RPC POST and optional session reuse.
- `sse`: treated through the HTTP client path for servers that answer with SSE
  event streams.

The repo does not currently support WebSocket upstreams, raw TCP upstreams, or
MCP batch JSON-RPC envelopes. Most MCP servers in current use are stdio or
Streamable HTTP; legacy SSE servers still exist and are the main compatibility
edge to test per server.

WebSocket is not a standard MCP transport in the MCP 2025-06-18 transport
contract. This repo treats WebSocket as a custom transport extension point, not
as a first-class broker transport. Add WebSocket only when it is opened with a real server compatibility fixture, a documented framing contract, reconnect rules, auth handling, and client-visible status behavior.

## Upstream Modes

`shared` means one broker-owned upstream instance is reused for every request
that targets that upstream. This is the right default for authenticated MCPs
when the account and token cache are intended to be shared by the local user.

`per_session` means the broker keeps separate upstream process groups by client
session. Use it when the upstream has request-local browser state, project-local
filesystem roots, or mutable state that must not bleed between sessions.

Keep Playwright `per_session` for normal chat-client use. A shared Playwright
upstream would make parallel LLM sessions fight over the same browser context,
page state, console buffer, and navigation target.

`disabled` keeps the upstream in the config inventory without exposing or
starting it.

`serialize_calls: true` is separate from mode. It keeps a shared upstream but
allows only one `tools/call` at a time for write-capable or fragile servers.
Use this for shared local-user auth MCPs when one account or token cache should
be reused but writes can conflict.

## Status Visibility

Codex `/mcp` can show the broker process and the broker tools it advertises. It
cannot show every hidden upstream as a separate authenticated or enabled line
while Codex is wired to one compact `mcp-broker` entry. That detail lives behind
the broker facade and the local daemon health endpoint.

The local status sources are `make broker-status` and the compact MCP tool
`broker_status`. `make broker-status` calls `broker/health` and reports daemon
state. `broker_status` reports the configured socket path, broker status, and
profile-scoped upstream view through MCP: enabled or disabled state, profile
exposure, mode, transport, mutation flag, PID, restart count, session count,
last error, `auth_probe`, auth state, and auth-repair counters. `auth_probe` is
a passive credential-source check that reports `credentials_missing`,
`credentials_present`, `oauth_refresh_expired`, `auth_repair_configured`, or
`none`.

The status payload also reports broker identity fields from `broker.identity`:
`broker_id`, `environment`, `bundle_version`, `schema_version`, the active
profile, and the configured profile set. These fields are the local contract
consumed by future bundle and fleet governance flows. Auth state is passive:
`unknown` unless the upstream health snapshot exposes an auth signal, the
passive probe proves a missing or expired credential source, or the last error is
an auth-shaped failure. The status tool does not open browsers, run OAuth setup
tools, or expose secret values. It still appears inside the broker server, not as
separate Codex `/mcp` rows.

`make profile-validation PROFILE=<profile>` loads the configured upstream list
from YAML and validates each enabled profile-visible upstream through the
compact facade. The broker does not infer safe tools from upstream catalogs;
each upstream must provide a `smoke` block with a safe read probe.

`make codex-deferred-acceptance` is the maintainer bridge from broker-owned
validation to Codex operator acceptance. It reads the same `smoke` probes and
prints the `mcp__mcp_broker__` wrapper calls to run from inside Codex. It does
not invoke `codex exec`, does not call an external LLM session, and is not part
of `make quality-gate`.

MCP `tools/list` and `tools/call` may include `params.profile` to apply a
file-backed client profile. The daemon resolves that profile from central
config, skips denied upstreams before opening stdio or HTTP sessions, and
rejects mutating upstream calls unless the profile allowlists that upstream.

`mcp-broker-client` is the stateless stdio shim. It reads MCP JSON-RPC payloads
from stdin, forwards each payload to the configured broker Unix socket, and
writes broker responses to stdout. It does not start upstream MCP processes.

## Tool Namespacing

Upstream tools are exposed with prefixes:

```text
example-store.read_graph
example-http.search
example-mutating.write_note
```

The separator is configured by `broker.tool_namespace_separator`.

## Routing

The broker routes by prefix:

```text
<prefix>.<tool_name> -> upstream[prefix].tools/call(tool_name)
```

No routing table is hardcoded in source. Prefixes come from config.

`mcp_broker.tool_namespace.ToolNamespaceRouter` owns this mapping. It rewrites
upstream tool names for advertisement and resolves advertised names back to the
configured upstream and original upstream tool name.

`mcp_broker.broker.BrokerCore` owns broker-level `tools/list` behavior. It
aggregates configured upstream tool lists, skips disabled or profile-denied
upstreams, rejects duplicate advertised names, and returns compact broker tools
when compact mode is enabled and the full list would exceed the profile budget.
Profiles may set `broker_tool_name_style: snake` to advertise compact broker
facade names such as `broker_status` while preserving the canonical dotted
names for routing and validation.
The daemon `tools/list` path starts enabled stdio upstreams or opens configured
HTTP upstream sessions on demand, reads each `tools/list` response, then passes
those upstream tool lists through `BrokerCore` before returning the namespaced
broker response.

For stdio upstreams, the broker performs the upstream MCP `initialize`
handshake before the first upstream `tools/list` request. It also keeps a retry
fallback for upstream `tools/call` responses that reject a request as not
initialized. This avoids poisoning strict MCP servers that reject any
pre-initialize request while still recovering from servers that report the
initialization error on the first call.

Broker-owned stdio requests use numeric JSON-RPC IDs starting at zero. This
keeps compatibility with upstream MCP servers that assume numeric IDs even
though JSON-RPC permits strings. The stdio reader can skip JSON-RPC
notifications while it waits for the matching response, and it owns a byte
buffer over stdout so an interleaved notification cannot strand the following
response in Python's pipe buffer.

For HTTP upstreams, the broker uses MCP Streamable HTTP: one JSON-RPC POST per
request, `Accept: application/json, text/event-stream`,
`MCP-Protocol-Version`, optional `Mcp-Session-Id` reuse after initialization,
and JSON or SSE response parsing. Env values configured under the upstream are
resolved at runtime and token values are not written to broker errors. Remote
HTTP retry is configured per upstream under `health.http_retry_attempts` and
`health.http_retry_backoff_seconds`; retries apply only to transient HTTP
statuses `429`, `500`, `502`, `503`, and `504`, while auth failures such as
`401` fail without retry.

`BrokerCore.call_tool` resolves a namespaced tool name to the configured
upstream and original upstream tool name, passes the upstream call timeout from
config, and maps upstream timeout, crash, unknown-tool, invalid-argument,
disabled-prefix, profile-denial, and mutating-profile-denial failures to
broker-owned error codes.
