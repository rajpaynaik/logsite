# Sentry

Sentry already groups errors into issues. logsite reads a project's error events (each
exception chain with its frames and lines of code, the trace id, the release) and puts them
through what it does for errors from logs, against the code at your commit:

- [`logsite repro`](repro.md): a failing test for a Sentry error, checked to fail the same way
- the [GitHub Action](github-action.md): a draft pull request with that test for each new error
- [incidents](errors.md) linked with the deploy and commits before them, and [history](history.md)
- `logsite about`, the editor and the MCP server: what production says about a line, before you change it

## Set it up

Create an auth token with the `event:read` scope (Sentry: **Settings → Auth Tokens**; on
self-hosted Sentry, add `project:read`), and point `logsite.toml` at the project:

```toml
[backend]
type = "sentry"
org = "acme"                         # the organization slug
project = "checkout"                 # the project slug
token_env = "SENTRY_AUTH_TOKEN"      # the environment variable that holds the token
# url = "https://sentry.example.com" # self-hosted; default https://sentry.io
```

```bash
export SENTRY_AUTH_TOKEN=...
logsite errors --hours 24
logsite repro 1 --write-test
```

The token is read from the environment and never written to disk. logsite only reads.

## What it reads

The project's error events in the window, newest first, up to `--limit` (5,000): each
event's exception chain, every frame's file, line, function and line of code (from the
source context Sentry captured), the trace id, the release, the environment and the
transaction. Java frames get their package path (`com/acme/shop/Inventory.java`), so they
map to your source tree.

Sentry's own issue grouping isn't used: logsite groups by the exception chain and your
frames, the same way for every source, so a Sentry error and the same error in your logs
land in one group.
