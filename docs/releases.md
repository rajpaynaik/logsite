# Release health

`logsite release` tells you whether a release made errors worse: which errors are new, which
came back, which are spiking and which it fixed, with the commits in the release that touched
the code of each new one. It reads the [error store](watch.md#what-it-stores), so run
`logsite watch` (or `logsite db backfill`) first.

```
$ logsite release
Release v1.4.0 · BAD
deployed 2026-10-29 14:02 UTC · a1b2c3d4 · 12 commits since v1.3.2
6h after it, against 6h before

              BEFORE    AFTER   CHANGE
Errors/hour       41      127   ×3.1
Error rate      0.4%     1.3%   ×3.2
Groups            14       16   2 new, 1 back, 3 fixed

  #  STATUS     COUNT    ERROR                              RAISED AT
  ────────────────────────────────────────────────────────────────────────
  1  new        0 → 38   TimeoutError: payment gateway ti…  payments.py:88
  2  regressed  0 → 9    KeyError: 'currency'               orders.py:41
  3  spiking   13 → 57   ConnectionError: redis             cache.py:22
  4  resolved  22 → 0    ValueError: bad sku                catalog.py:17

Suspect commits
  1  a1b2c3d4  Retry payments with backoff  Sam
```

```
$ logsite releases
  RELEASE  DEPLOYED       ERRORS/H  NEW  BACK  FIXED  VERDICT
  ───────────────────────────────────────────────────────────
  v1.4.0   Oct 29 14:02   41 → 127    2     1      3  BAD
  v1.3.2   Oct 27 09:15    45 → 41    0     0      1  HEALTHY
```

## Where releases come from

1. **A release field in your logs:** `release`, `service.version`, `version`, `app_version`
   or `git_sha`. Each stored error keeps it, and a release went out when its first error
   was stored. This is the exact one, and works with rolling deploys.
2. **A git tag:** `logsite release v1.4.0` in the repository, at the tag's date.

Leave the release out for the latest one. `--deployed-at 2026-10-29T14:02:00Z` sets the
time when neither is right.

## How it compares

The time from the release until now, at most `window_hours` (24), against as long just
before it.

| Status | Means |
|---|---|
| `new` | First seen after the release |
| `regressed` | Seen before, quiet in the window before, back after the release |
| `spiking` | At least `[watch] spike_min_count` (20) times, and `spike_ratio` (5) times the window before |
| `resolved` | In the window before, gone after the release |

When the logs carry timings ([latency](latency.md)), a `p95 latency` row compares them too.

The error rate is errors as a share of all log lines, when `logsite watch` counts them.

**Verdict:**

- **BAD**: errors per hour up `bad_rate` (3) times or more, or a new or regressed error
  `bad_count` (10) times or more;
- **DEGRADED**: any new, regressed or spiking error, or p95 latency up `[latency] ratio` (2)
  times over at least `min_calls` (100) timed calls;
- **HEALTHY**: none of those.

## In a deploy pipeline

```bash
logsite release v1.4.0 --fail-on bad      # exit 1 if the release is bad
logsite release --format json             # the same, as JSON
```

## Slack

`logsite watch` posts the latest release's verdict once it turns degraded or bad, with the
table above. It waits until the release has been out `settle_minutes` (30), and posts each
verdict once per release: degraded, then bad if it gets worse. Turn it off by leaving
`"release"` out of `[watch] alert_on`.

## Configuration

```toml
[release]
window_hours = 24          # how long after a release to look, at most
bad_rate = 3.0             # errors per hour this many times the window before: bad
bad_count = 10             # a new or regressed error this many times: bad
settle_minutes = 30        # watch judges a release once it has been out this long
```

## Ask Claude

The [MCP server](mcp.md) has a `release_health` tool: "is the latest release healthy?"
