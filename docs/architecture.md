# mcp-broker Architecture

## Overview

`mcp-broker` is a local MCP aggregation and process-supervision layer.

Clients connect to one local broker. The broker owns upstream MCP servers.

```text
MCP client
  -> mcp-broker-client
    -> Unix socket
      -> mcp-broker
        -> upstream MCP server processes
```

## Components

### Broker Daemon

Responsibilities:
- Load central config.
- Start and stop upstream MCP processes.
- Aggregate tool lists.
- Route tool calls.
- Enforce idle timeout, CPU watchdog, and memory ceiling policy.
- Serialize calls for upstreams with unsafe write paths when configured.
- Write structured logs.

### Client Shim

Responsibilities:
- Speak stdio MCP to a rendered MCP client profile.
- Forward JSON-RPC messages to the broker.
- Keep no upstream state.

### Upstream Supervisor

Responsibilities:
- Own each upstream process group.
- Track health, restart count, and CPU samples.
- Kill runaway processes.
- Keep one shared process for `shared` upstreams.
- Start isolated processes for `per_session` upstreams.
- Start a fresh process for each `per_call` operation and stop it when that
  operation ends.

The current implementation covers process supervision for local stdio
upstreams: create the configured state directory, start a local subprocess,
capture stdout and stderr to runtime state logs, track lifecycle state, enforce
startup readiness timeout hooks, stop process groups, and restart with backoff
plus circuit-breaker limits. The supervisor can reap idle upstreams and kill CPU
spin based on process-group CPU samples and no recorded MCP progress. It can
also stop an upstream process group when macOS reports resident memory above the
configured per-upstream `resources.memory_ceiling_mb` value. Missing or
unsupported memory samples do not kill the upstream.

The runtime reaper owns cleanup for file-backed broker metadata. It removes
stale broker-owned pidfiles, removes stale broker-owned socket paths only when
their recorded owner PID is gone, and kills orphaned broker-owned process groups
when the recorded broker PID no longer exists.

The daemon uses a broker-owned lockfile under runtime `run/broker.lock` to refuse
a second live daemon for the same runtime. It removes stale locks when the
recorded PID is gone and clears the lock during shutdown.

The daemon writes newline-delimited JSON lifecycle, request, and upstream event
records to runtime `logs/broker.jsonl`. Upstream events cover process start,
ready, call, timeout, stop, kill, restart, backoff, and disabled states.

The daemon also writes a file-backed metrics snapshot to
runtime `state/broker-status.json` at start, after handled socket requests, and
during shutdown. The snapshot carries daemon status, request counters, last
request status, and the current per-upstream health map.

Current process-manager coverage includes broken upstream command checks in
doctor, shared pooling, per-session pooling, and broker-owned orphan reaping.

## Upstream Modes

| Mode | Meaning |
|---|---|
| `shared` | One process is reused across clients. |
| `per_session` | One process per client session. |
| `per_call` | One fresh process per tool call or tool-list operation. |
| `disabled` | Defined but not started or advertised. |

Default to `shared` for read-heavy local tools. Use shared plus
`serialize_calls: true` for broker-smoked auth or write paths that can keep
broker-owned state. Use `per_session` for browser and project-root-bound tools
until tests prove safe sharing. Use `per_call` when each operation requires a
fresh process and the broker must release it as soon as that operation ends.

Upstreams can set `serialize_calls: true` to force one in-flight `tools/call`
per upstream name. The daemon owns the lock registry, so serialization survives
per-request `BrokerCore` construction. Use it for unsafe write paths such as
Obsidian and protected auth MCPs when shared broker-owned state is intentional.
