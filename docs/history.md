# History: seen before, fixed by, came back

Without a history, logsite only compares the window you ask for with the one before it:
it can say an error is *new since yesterday*, not that it's *new ever*, and it can't tell
you that the same error was fixed last month and is back.

The history store is logsite's memory. Every run of `logsite errors`, `investigate`,
`alert-server` and the MCP tools records the error groups it found; `logsite sync` does the
same on a schedule. From that, logsite knows for each group:

- when it was **first seen ever**, and in which release;
- how many errors it caused, on how many days;
- when it **went away**, the release it went away in, and the commits in that release that
  changed the code where it's raised (**probably fixed by**);
- when it **came back**: a regression.

## What you see

A regression in `logsite errors`: `back` in the table, and the story in the card:

```
660 errors in the last 24h · 1 group(s) · 1 new · 1 back · before: 0

  #  TREND  COUNT  ERROR                                                 RAISED AT                 LOGGED AT
  1  back     660  IllegalStateException: Shipping label request failed  CarrierApiClient.java:58  —

━━ 1. IllegalStateException: Shipping label request failed ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 660 back
   Root cause   SocketTimeoutException: Read timed out  (java.net)
   Raised at    src/main/java/com/example/shipping/CarrierApiClient.java:58  requestLabel()
   History      ↩ came back · seen 3 Aug 2026 → 3 Sep 2026, gone after v1.3
   Fixed by     41bdce40 2026-09-03 Maya: Retry carrier timeouts with backoff
```

`logsite investigate` puts it next to the deploy and the commit that probably undid the fix:

```
   History      ↩ came back · seen 3 Aug 2026 → 3 Sep 2026, gone after v1.3
   Fixed by     41bdce40 2026-09-03 Maya: Retry carrier timeouts with backoff
   Deploy       v1.4 at 2026-10-18 02:48 UTC, 4 min before it started
   Suspects     0df4dac1 2026-10-18 Maya: Lower carrier read timeout to 3s
```

Other groups get one line: `first time ever (history since 3 Aug 2026)`, or
`since 3 Aug 2026 (v1.2) · 41,230 errors on 12 day(s) before this`. Alerts from
`alert-server` say `↩ came back … gone after v1.3` and `fixed before by …` too, and
`--format json` has a `history` object per group.

One group's whole story, by its id:

```
$ logsite seen ed64f864
ed64f864  java.lang.IllegalStateException: Shipping label request failed
   Status          active, came back 1 time(s)
   First seen      2026-08-03T08:00 UTC (v1.2)
   Last seen       2026-10-18T03:57 UTC
   Raised at       src/main/java/com/example/shipping/CarrierApiClient.java:58
   Was gone        2026-09-03T08:14 UTC, after v1.3 → back 2026-10-18T02:52 UTC
   Fixed before by 41bdce40 2026-09-03 Maya: Retry carrier timeouts with backoff
   Per day
     2026-09-02  ██ 36
     2026-09-03  █ 15
     … 44 day(s) without it
     2026-10-18  ██████████████████████████████ 660
```

## Filling it

It fills itself whenever you run the commands above. To make it complete, also run
`logsite sync` on a schedule, and backfill once from what your log store still has:

```bash
logsite sync --backfill 30d          # once: read back 30 days, 6h at a time
logsite sync                         # every 15 min or hourly (cron, Kubernetes CronJob)
```

A long-running `logsite alert-server --sync-every 15m` does the same without cron, and serves
the history to Grafana: an Error groups panel column and annotations on the time graphs
([Grafana](grafana.md#history-in-the-dashboard)).

`--step` sets how much is queried at a time (default `6h`); make it smaller if a step hits
`--limit` (5000 error records), and logsite says so when it does.

## How it decides

- **First time ever** needs the history to have been watching before the window you're
  looking at; on the first run nothing is called "first time ever".
- **Gone** means runs have watched for `resolve_after_hours` (default 24) after the last
  sighting without seeing it. Time nobody ran logsite doesn't count: a week without runs
  doesn't make an error "fixed".
- **Fixed by** is the first release tagged between 30 minutes before and 6 hours after the
  last sighting, and the commits in it that changed the lines around where the error was
  raised (or logged). Without a release, the commits of the three days before.
- **Came back** is a group that was gone and is seen again. Its earlier fix is kept.
- Counts are kept per hour; runs over the same hours don't add up twice.

## What's stored, and where

One SQLite file per project, by default `~/.local/share/logsite/history/<project>-<hash>.db`
(`$XDG_DATA_HOME` if set), never in your repository. It holds only summaries: each group's
masked title, exception type, the code locations, hourly counts, dates, release names and
commit ids. No log lines, stack traces or field values. About 1 KB per group per day.

```toml
[history]
enabled = true                  # false: no history
path = "/var/lib/logsite/history.db"   # e.g. a volume for the alert-server container
resolve_after_hours = 24
```

`LOGSITE_HISTORY=/path/to/history.db` overrides the path (the Docker image uses
`/data/history.db`). `--no-history` on `errors` and `investigate` skips it for one run. Alerts narrowed to some of
your labels read the history but don't record into it, since they saw only part.
