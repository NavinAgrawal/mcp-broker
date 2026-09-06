# Auth Recipes

Use these patterns in `config/broker.private.yaml`. Keep token values out of
YAML, source, tests, and logs.

## Host Environment Variables

Use `env:` when an upstream can read a secret from the environment of the
broker process.

```yaml
upstreams:
  example-env-auth:
    enabled: true
    mode: shared
    transport: stdio
    tool_prefix: example-env
    command: npx
    args:
      - -y
      - example-env-auth-mcp
    state_dir: upstreams/example-env-auth
    profiles:
      - manual-test
    env:
      EXAMPLE_API_TOKEN: EXAMPLE_API_TOKEN
```

The left side is the variable name passed to the upstream process. The right
side is the variable name the broker reads from its own host environment.

Use this when the broker runs in the same shell that has the secret loaded. Do
not use it for LaunchAgent-owned daemons unless the LaunchAgent supplies that
environment.

## Runtime Secret Files

Use `env_files:` when the broker should read a token from its runtime secret
store and pass it to the upstream as an environment variable.

```yaml
upstreams:
  example-file-auth:
    enabled: true
    mode: shared
    transport: stdio
    tool_prefix: example-file
    command: npx
    args:
      - -y
      - example-file-auth-mcp
    state_dir: upstreams/example-file-auth
    profiles:
      - manual-test
    env_files:
      EXAMPLE_API_TOKEN: "{runtime.secrets_dir}/EXAMPLE_API_TOKEN"
```

Import an existing shell variable into the runtime secret store:

```bash
make secret-import-env SECRET_NAME=<name>
```

The target writes the secret file with mode `600`. This is the preferred
pattern for LaunchAgent-managed broker daemons.

## Request Metadata

Use `request_meta:` when the upstream expects a token inside each MCP tool call,
for example `params._meta.authToken`.

```yaml
upstreams:
  example-request-meta-auth:
    enabled: true
    mode: shared
    transport: stdio
    tool_prefix: example-request-meta
    command: npx
    args:
      - -y
      - example-request-meta-auth-mcp
    state_dir: upstreams/example-request-meta-auth
    profiles:
      - manual-test
    env_files:
      EXAMPLE_AUTH_TOKEN: "{runtime.secrets_dir}/EXAMPLE_AUTH_TOKEN"
    request_meta:
      authToken: EXAMPLE_AUTH_TOKEN
```

Each `request_meta` value must reference an `env` or `env_files` key. Config
validation rejects metadata that points at an unknown source.

## OAuth And Browser Setup

For OAuth or browser-backed MCPs, keep browser state and token caches under the
upstream state directory when the upstream supports a configurable state path.

```yaml
upstreams:
  example-oauth:
    enabled: true
    mode: shared
    transport: stdio
    tool_prefix: example-oauth
    command: npx
    args:
      - -y
      - example-oauth-mcp
      - --state-dir
      - "{runtime.state_dir}/upstreams/example-oauth"
    state_dir: upstreams/example-oauth
    profiles:
      - protected
    serialize_calls: true
```

Use `mode: shared` plus `serialize_calls: true` for shared local-user auth
state.

When one CLI can manage multiple accounts, create one upstream per account and
give each upstream its own state directory. If the CLI chooses the account from
an environment variable, feed that value through `env_files:` so a LaunchAgent
daemon gets the same account selection as an interactive shell.

Example for a second Google Workspace account:

```yaml
upstreams:
  google-workspace-business:
    enabled: true
    mode: shared
    transport: stdio
    mutating: true
    serialize_calls: true
    tool_prefix: google-workspace-business
    command: gws
    args:
      - mcp
      - --services
      - drive,docs,sheets,calendar,gmail
      - --tool-mode
      - compact
    state_dir: upstreams/google-workspace-business
    env_files:
      GOOGLE_WORKSPACE_CLI_CONFIG_DIR: "{runtime.secrets_dir}/GWS_BUSINESS_CONFIG_DIR"
```

`GWS_BUSINESS_CONFIG_DIR` should contain the path to that account's GWS config
directory, for example
`$HOME/mcp/mcp-broker/state/upstreams/google-workspace-business/gws`.

Shell tokens and API keys follow a different rule: import the token into
`{runtime.secrets_dir}` with `make secret-import-env SECRET_NAME=<name>` and map
the upstream's child variable to that file with `env_files:`.

Use `mode: shared` plus `serialize_calls: true` for shared local-user auth
state where parallel calls could fight over the same browser profile, token
refresh, or draft state. Use `mode: per_session` when each LLM session needs its own
process-local state.

## Session Context Environment

Use `session_env:` when an upstream chooses project state from a startup
environment variable. The broker passes the LLM client's current working
directory as `client_cwd`; config maps that source into the variable name the
upstream expects.

```yaml
upstreams:
  example-project-state:
    enabled: true
    mode: per_session
    transport: stdio
    tool_prefix: example-project-state
    command: $HOME/mcp/vendor/example-project-state/.venv/bin/python
    args:
      - -m
      - example_project_state_mcp
    state_dir: upstreams/example-project-state
    profiles:
      - manual-test
    session_env:
      PROJECT_DIR: client_cwd
```

`session_env` requires `mode: per_session` or `mode: per_call` because the
upstream process reads this value at startup. A shared process would keep the
first caller's project state for later callers. `per_call` resolves the current
call context into a fresh process and stops that process after the operation.

## Auth Repair

Use `auth_repair:` when an upstream exposes a setup tool that can open browser
auth, save state, and let the broker retry the original call after an auth
error.

```yaml
upstreams:
  example-request-meta-auth:
    auth_repair:
      tool: setup_auth
      arguments:
        show_browser: true
        headless: false
      trigger_errors:
        - "Not authenticated"
        - "setup_auth"
      retry_original: true
      timeout_seconds: 300
```

The broker runs the repair tool only after the upstream returns a matching
auth-shaped error. Passive status checks do not open browsers or run setup
tools.

## Status Checks

Use `broker_status` through the broker facade after an auth failure. It reports
the broker-owned view of each upstream:

- `auth_probe`
- `auth_state`
- `last_error`
- `auth_repair_attempts`
- `auth_repair_successes`
- `auth_repair_failures`
- `mode`
- `session_count`
- `pid`

`auth_probe` reports what the broker can prove without starting a browser auth
flow:

- `credentials_missing`: one or more configured `env` or `env_files` sources
  are absent or empty.
- `credentials_present`: configured credential sources exist, but the broker
  has not called the upstream to prove the token is accepted.
- `oauth_refresh_expired`: a configured broker-owned OAuth token JSON file has
  an expired refresh-token timestamp.
- `auth_repair_configured`: the upstream has a configured setup tool that can
  run only after a matching auth error.
- `none`: no passive auth source is configured.

For OAuth-backed MCPs that store token JSON in a broker-owned secret or state
file, configure a passive token-file probe:

```yaml
auth_probe:
  type: oauth_token_file
  token_file: "{runtime.secrets_dir}/example-oauth.json"
  required_fields:
    - access_token
    - refresh_token
  refresh_token_expiry_field: refresh_token_expires_at
```

The broker reads only enough structure to report missing fields, invalid JSON,
or expired refresh tokens. It does not log token values or file paths.

`auth_state` is `unauthenticated` when the broker has seen an auth-shaped
failure, `authenticated` after a repair succeeds, and `unknown` when no passive
signal exists.

## Validation

Run these after editing auth config:

```bash
make config-validate
make profile-validation PROFILE=manual-test
make broker-smoke
```

For a client profile, replace `manual-test` with the profile you expose in the
YAML. Do not apply client config until validation passes for that profile.
