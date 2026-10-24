# Watch production: errors to Slack, stored for later

`logsite watch` checks your logs every few minutes, groups the errors by cause, **stores every
occurrence in your own database**, and posts to Slack when something needs a person. No SDK:
it reads the log store you already have (Loki, OpenSearch / Elasticsearch, CloudWatch,
OpenTelemetry, Sentry or a file).

```
:red_circle: checkout: 1 new error(s), 1 back after a fix (last 15 min)

1. KeyError: 'id'  ·  31  ·  new
    raised: app/checkout.py:17  `return gateway.refund(order["id"])`
    logged: app/checkout.py:19
    example: `logsite trace 9f2c41d0`

2. TimeoutError: timed out  ·  12  ·  back
    raised: app/supplier.py:31  `with urllib.request.urlopen(request, timeout=3.5)`
    came back: gone after v1.3, fixed by 41bdce40 Retry carrier timeouts with backoff
```

## Set it up

1. Create a Slack **incoming webhook** for the channel (Slack: **Apps → Incoming Webhooks →
   Add to Slack**), and put its URL in an environment variable.
2. Add to `logsite.toml`, next to your `[backend]`:

```toml
[alerts]
slack_webhook_env = "SLACK_WEBHOOK_URL"

[watch]
every = "5m"                         # how often to check
alert_on = ["new", "regression", "spike"]
cooldown_minutes = 60                # per error and reason

[store]                              # optional: default is a SQLite file
url_env = "LOGSITE_DATABASE_URL"     # postgresql://user:pass@host/db
retention_days = 90
```

3. Run it where it can reach your log store (a server, a container, a Kubernetes pod):

```bash
pip install "logsite[postgres]"      # without [postgres] for SQLite
export SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
logsite watch --dry-run              # print the messages first
logsite watch
```

`logsite watch --once` checks once and exits, for cron or a scheduled CI job.

## When it posts

| Reason | When |
|---|---|
| **new** | an error group logsite has never seen. In its first hour, before the history knows what's normal: one that wasn't there in the window before, so errors that already failed before you started it don't page |
| **regression** | a group that had stopped is back; the message says which release and commit fixed it last time |
| **spike** | a group at least `spike_ratio` (5) times its usual rate, with at least `spike_min_count` (20) in the window |

Each error alerts at most once per `cooldown_minutes` for each reason. The first check reads
the last 24 hours and stores them without posting, so you start with today's errors in the
store and no flood in Slack. Warnings that carry a
stack trace never page. Messages show where each error was raised, the line of code, and a
link to it on GitHub or GitLab.

## What it stores

Every check writes to the error store, in the database you choose with `[store] url`:

| Database | `url` | Install |
|---|---|---|
| SQLite (the default) | none: a file under `~/.local/share/logsite/store/` | built in |
| Postgres | `postgresql://user:pass@host:5432/db` | `pip install "logsite[postgres]"` |
| MySQL / MariaDB | `mysql://user:pass@host:3306/db` | `pip install "logsite[mysql]"` |
| SQL Server / Azure SQL | `mssql://user:pass@host:1433/db?TrustServerCertificate=yes` | `pip install "logsite[mssql]"` and Microsoft's ODBC Driver 18 |
| ClickHouse | `clickhouse://user:pass@host:8123/db` (`clickhouses://` for HTTPS) | `pip install "logsite[clickhouse]"` |

Put the URL in an environment variable (`url_env = "LOGSITE_DATABASE_URL"`), not in the file.

**The tables are created on first use** and upgraded when a new logsite needs more: the
database records its schema version. To create them up front, or to check on them:

```bash
logsite db init      # create or upgrade the tables
logsite db status    # schema version and rows per table
logsite db sql       # print the SQL, for a DBA who applies schema changes by hand
```

Each error read twice by overlapping checks is stored once, and values are masked (secrets,
emails, card numbers) before they're written. On ClickHouse, groups and line counts are kept
in `ReplacingMergeTree` tables: query them with `FINAL` to see the latest version of each row.

| Table | One row per |
|---|---|
| `error_groups` | cause: title, root cause, where it was raised and logged, first and last seen, total occurrences |
| `error_events` | occurrence: time, group, service, release, trace id, message, stack trace, the record's other fields (`jsonb` in Postgres) |
| `line_counts` | log call per hour: lines and bytes, for volume and cost |
| `alerts` | message sent: the group, the reason, where it went, when |

Events and line counts older than `retention_days` are deleted once a day; groups are kept.

## Query any time range, instantly

Finding last Tuesday's errors in Loki means scanning every line in that range, for every new
question. The error store has them already, grouped and indexed, so any range is a lookup:

```bash
logsite db backfill --since 30d      # read your log store's history in, an hour at a time
```

On the demo's day of noisy traffic (18.5 MB, 64,580 lines, 2,660 errors), the errors between
10:00 and 12:00 took **609 ms** to find by scanning the log and grouping, and **3.5 ms** from
the store, with the same answer (4 causes, 1,171 errors). That was measured on a local file; a
remote Loki adds the network on top.

- **In the app**: pick any range (or 1 h, 24 h, 7 days, 30 days), a service, a release, or
  search the error, its cause or its file.
- **In Grafana**: `logsite grafana --store postgres -o errors.json` (or `mysql`, `mssql`)
  writes a dashboard on the store, with counts, errors over time, the top causes with their
  lines, by service and by release, and the latest occurrences. Import it and pick the SQL
  data source that points at the store. The time picker drives every panel, and nothing
  scans Loki.
- **In SQL**: see below.

`logsite db backfill` is safe to repeat (nothing is stored twice) and records the history as it
goes, so what's new and what came back is right from the start. If a chunk holds more than
`--limit` errors, it says so; use a smaller `--chunk`.

## Export to a warehouse: BigQuery, Snowflake

For analytics teams that work in a warehouse, each check can also append its new errors there.
The dataset or schema and two tables are created when missing:

- `logsite_error_events`: every new occurrence (the same columns as `error_events`, plus
  `project` and `exported_at`);
- `logsite_error_groups`: a snapshot of each group seen, per check. For a group's latest state,
  take its row with the latest `exported_at`.

```toml
[export]
type = "bigquery"                # credentials: Application Default Credentials
project = "acme-analytics"
dataset = "logsite"
# location = "EU"
```

```toml
[export]
type = "snowflake"
account = "acme-xy12345"
user = "LOGSITE"
password_env = "SNOWFLAKE_PASSWORD"   # or authenticator / private_key_file
warehouse = "COMPUTE_WH"
database = "ANALYTICS"
schema = "LOGSITE"
# role = "LOGSITE_WRITER"
```

`pip install "logsite[bigquery]"` or `"logsite[snowflake]"`. BigQuery gets batch load jobs,
which are free (unlike streaming inserts). An export that fails, say because the warehouse is
down, is reported by `logsite watch` and on the app's Watching page. The errors are in the
error store regardless, and watching goes on.

## Analysing it later

The tables are plain SQL, so Grafana (the Postgres data source), Metabase, a notebook or `psql`
can read them:

```sql
-- the errors of the last week, by day and cause
select date_trunc('day', e.at) as day, g.title, count(*)
from error_events e join error_groups g on g.id = e.group_id
where e.at > now() - interval '7 days'
group by 1, 2 order by 1, 3 desc;

-- which release brought an error
select release, min(at) as first, count(*) from error_events
where group_id = '2d79f303' group by release order by first;

-- every failure one customer hit
select at, message from error_events
where fields->>'customer_id' = 'C-1042' order by at;

-- the log calls that cost the most this month
select site, template, sum(bytes) / 1e9 as gb from line_counts
where hour > now() - interval '30 days' group by 1, 2 order by gb desc limit 20;
```
