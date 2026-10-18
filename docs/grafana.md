# Grafana, alerts and Alloy

### Dashboard: cost and errors per code line

```bash
logsite grafana --uid checkout-logs -o dashboard.json
```

Import it in Grafana (Dashboards → New → Import) and pick your Loki data source. It queries
Loki directly, so it stays live:

- **Totals**: volume, lines, estimated cost per month (the price per GB is a dashboard
  variable you can change) and error lines.
- **Cost by code line**: every log call, most expensive first, shown with its message
  (`app/api.py:88  info('request %s %s')`) and linked to that line on GitHub, GitLab or
  Bitbucket at the current commit.
- **Errors by code line**, compared with the same range just before; the **loudest log
  calls** over time; **recent errors**.

logsite works out the rest from the code and a sample of records: the deployed path prefix to
strip (`/srv/` in `/srv/app/api.py`), the level field, and the link to the code host from
the git remote. It needs records with file and line fields (as for `aggregate = true`).
Message templates are baked in, so regenerate it when log calls change.

`logsite grafana --generic` writes a version for any project, with the selector, fields and
code link as dashboard variables instead; it's in
[`integrations/grafana`](../integrations/grafana) for the grafana.com catalog.

### Alerts: the error summary in the notification

When a Grafana alert fires, `logsite alert-server` sends the error summary with it, so whoever
is on call sees what broke and on which line without opening a single stack trace:

```
🔴 order-service errors: order-service is logging more than 10 errors in 5 minutes (firing since 16:49 UTC)
758 error(s) in 2 group(s) · Open in Grafana · Silence

1. com.example.orders.exception.OrderProcessingException: Unable to save order ORD-<n>  ·  561  ·  new
    root cause: org.postgresql.util.PSQLException: ERROR: canceling statement due to statement timeout (org.postgresql)
    raised: src/main/java/com/example/orders/repository/OrderRepository.java:87  `return jpa.save(order);`
    logged: src/main/java/com/example/orders/OrderController.java:56
    example: `logsite trace 77064c2c`

2. Retry failed for order ORD-<n>  ·  197  ·  1.0x
    logged: src/main/java/com/example/orders/service/OrderService.java:170
```

That's a real run: a Grafana 13 alert rule on Loki, its webhook contact point, and logsite
summarizing the Spring Boot traces in the alert's window.

1. Run it next to your checkout (it reads the code to tell your frames from library ones):

   ```bash
   export LOGSITE_ALERT_TOKEN=$(openssl rand -hex 16) SLACK_WEBHOOK_URL=https://hooks.slack.com/...
   logsite alert-server --host 0.0.0.0 --port 8085
   ```

2. In Grafana: **Alerting → Contact points → Add → Webhook**, URL
   `http://<logsite host>:8085/grafana/alert`, **Authorization header** scheme `Bearer`,
   credentials = the token. Route your alerts' notification policy to it (it can sit next to
   your existing Slack / PagerDuty contact point).

For each firing alert, logsite summarizes from `lookback_minutes` before it started until now,
compared with the same span just before, narrowed to the alerted service with the alert's
labels (`selector_labels`, for Loki). It posts to `slack_webhook` and/or `webhook` (the
summary as JSON) and returns it in the response. `GET /errors?from=&to=` serves the same
summary for any range (Grafana's `${__from}` / `${__to}` work), for an "Error groups" panel.

```toml
[alerts]
slack_webhook_env = "SLACK_WEBHOOK_URL"
# webhook = "https://example.com/hooks/logsite"   # gets {alert, labels, text, summary}
token_env = "LOGSITE_ALERT_TOKEN"   # required unless it only listens on localhost
lookback_minutes = 30
top = 3                             # groups per alert
selector_labels = ["service_name"]  # alert labels that narrow the Loki selector
```

#### The same summary on the dashboard

`logsite grafana --error-groups http://<logsite host>:8085` adds an **Error groups** panel at
the top of the dashboard: errors grouped by cause for the dashboard's time range, so whoever
opens Grafana from the alert sees it first.

| Trend | Count | Error | Root cause | Raised at | Code | Logged at |
|---|---|---|---|---|---|---|
| new | 1800 | OrderProcessingException: Unable to save order ORD-&lt;n&gt; | PSQLException: canceling statement due to statement timeout | OrderRepository.java:87 | `return jpa.save(order);` | OrderController.java:56 |
| new | 640 | Retry failed for order ORD-&lt;n&gt; | | | | OrderService.java:170 |

"Raised at" and "Logged at" link to the code. The panel reads `logsite alert-server`'s
`/errors` through the free [Infinity](https://grafana.com/grafana/plugins/yesoreyeram-infinity-datasource/)
data source plugin. Install it, then add an Infinity data source with the token as a header,
so it never appears in a dashboard:

```yaml
# provisioning/datasources/logsite.yaml (or the same fields in the UI)
apiVersion: 1
datasources:
  - name: logsite
    type: yesoreyeram-infinity-datasource
    jsonData:
      httpHeaderName1: Authorization
      allowedHosts: ["http://logsite:8085"]
    secureJsonData:
      httpHeaderValue1: Bearer <LOGSITE_ALERT_TOKEN>
```

Pick it in the dashboard's **logsite (Infinity)** variable. The panel is opt-in, so the
catalog dashboard doesn't require the plugin.

#### History in the dashboard

With the [history store](history.md), the panel also says which errors are back after a fix
and since when each one has been around:

| Trend | Count | Error | History | … | First seen | Fixed before by |
|---|---|---|---|---|---|---|
| back | 660 | IllegalStateException: Shipping label request failed | ↩ back, gone after v1.3 | … | 2026-08-03 | 41bdce40 Retry carrier timeouts with backoff (v1.3) |
| new | 40 | KeyError: 'zone' | first time ever | … | 2026-10-18 | |

The dashboard also gets a **logsite history** annotation (toggle at the top): red markers
on the time graphs where an error was first seen, where it went away and where it came back.
Hovering one shows, for example, *came back: IllegalStateException: Shipping label request
failed · was gone 2026-09-03, fixed by 41bdce40 Retry carrier timeouts with backoff*. It
reads `/history/events` and uses your Infinity data source (Grafana doesn't run annotations
through a data source variable; with several Infinity data sources, pick one in the
dashboard's annotation settings).

#### Running it for a team: one container

`alert-server` serves the panel, the annotations and the alert messages; with
`--sync-every 15m` it also keeps the history filled. In a container, with your repository
mounted (for code lines and the commits that fixed an error) and the history on a volume:

```bash
docker build -t logsite integrations/docker
docker run -d -p 8085:8085 \
  -v "$PWD:/repo:ro" -v logsite-history:/data \
  -e LOGSITE_ALERT_TOKEN=<shared secret> logsite
```

or `docker compose -f integrations/docker/compose.yaml up -d` from the repository. The image
runs `logsite alert-server --host 0.0.0.0 --sync-every 15m` with `LOGSITE_HISTORY=/data/history.db`;
backfill once with `docker exec <container> logsite --config-dir /repo sync --backfill 30d`.

### Alloy rules: drop or sample noisy lines before they're shipped

```bash
logsite pipeline --max-per-window 100000 --drop-level debug -o logsite.alloy
```

writes a Grafana Alloy `loki.process` component with one rule per log call, sized from real
volume and priced like `logsite cost`:

```alloy
  // app/api.py:88  info('request %s %s')
  // keep 1% (9,100,000 per 24h, over 100,000): saves 3.4 TB/month ($1,706)
  stage.match {
    selector = "{logsite_site=~\"(.*/)?app/api[.]py:88\"} !~ \"(?i)(error|exception|traceback|critical|fatal)\""

    stage.sampling {
      rate                = 0.011
      drop_counter_reason = "logsite"
    }
  }
```

- Rules come from `[pipeline]` (`drop`, `sample`) and from volume: lines above
  `max_per_window` are sampled down to it, and lines at a `drop_levels` level are dropped.
- **Only `trace`, `debug`, `info`, `notice` and `success` log calls can get a rule.** The
  level written in the code is checked before anything is scored or sorted, and nothing in
  the config can override it: warning and error lines, a level set at runtime
  (`log.log(level, …)`) and unknown levels are skipped, each with the reason.
- Every rule also skips lines that look like errors (`error`, `exception`, `traceback`,
  `critical`, `fatal`), for a call that logs an error at runtime anyway.
- Lines are matched by the file and line fields records carry, through a temporary label
  that's removed before anything reaches Loki. Records without them are matched by message
  (`match = "template"`, with your stream `selector`); messages with too little fixed text
  get no rule.
- logsite only writes the file. It never applies rules to Alloy, and the MCP server has no
  tool that writes config: review the file, then deploy it yourself.
- Wire it in with `forward_to = [loki.process.logsite.receiver]` in your source; logs go on
  to `forward_to` in `[pipeline]` (default `loki.write.default.receiver`).

Checked against Alloy 1.20 and Loki: of 2,530 lines, the noisy line was kept at 1 in 10, the
debug line dropped, and all 30 error lines kept.

```toml
[pipeline]
drop = ["app/health.py:12"]
sample = { "app/api.py:88" = 0.01 }   # keep 1 in 100
max_per_window = 100000
drop_levels = ["debug"]
# match = "auto"                    # location | template
# selector = '{app="checkout"}'     # template rules: the stream as Alloy sees it
# forward_to = "loki.write.default.receiver"
```
