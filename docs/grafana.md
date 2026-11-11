# Grafana

logsite adds three things to Grafana:

- **Dashboards:** what each line of your code costs in logs, and which lines are failing.
  This page.
- **[Alerts that explain themselves](alerts.md):** when a Grafana alert fires, Slack gets the
  error, the line of code and the likely cause with it.
- **[Alloy rules](alloy.md):** drop or sample noisy log lines before you pay to store them.

## The cost and errors dashboard

```bash
logsite grafana --uid checkout-logs -o dashboard.json
```

In Grafana, choose **Dashboards → New → Import** and pick your Loki data source. The
dashboard queries Loki directly, so it stays live:

- **Totals:** volume, lines, estimated cost per month and error lines. The price per GB is
  a variable you can change.
- **Cost by code line:** every log call, most expensive first, with its message
  (`app/api.py:88  info('request %s %s')`) and a link to that line on GitHub, GitLab or
  Bitbucket.
- **Errors by code line**, against the same range just before; the **loudest log calls**
  over time; and **recent errors**.

logsite works out the rest from your code and a sample of records: the path prefix to strip
(`/srv/` in `/srv/app/api.py`), the level field, and the code host from your git remote.
Records need file and line fields. Messages are built into the dashboard, so regenerate it
when log calls change.

### Releases and deploys

During a rolling deploy, or over a time range that spans one, old and new replicas both log
from `app/api.py:42`, and that line can be a different log call in each release. When records
carry their release (`release`, `service.version`, `version`, `git_sha`, or a field you name
with `--revision-field`), the dashboard:

- groups every per-line panel by release first, so two different calls at one `file:line`
  are never added together;
- shows each release's row with the message from the code that release ran, linked to that
  line at its commit (release `1.4.0` is the git ref `1.4.0` or `v1.4.0`);
- puts lines from any other release, or with no release, in a separate **Cost by code line:
  unknown release** table, with no message or link to guess.

It knows the releases in a sample of recent records (up to 10), so regenerate it after a
release. Without a release field, messages and links come from the current commit, and
`logsite grafana` warns you. `--ref` pins every link to one commit.

### For any project

`logsite grafana --generic` writes a version with the selector, fields and code link as
dashboard variables. It's in [`integrations/grafana`](../integrations/grafana), for the
grafana.com catalog.

## The logsite data source (Grafana plugin)

The [logsite data source plugin](../integrations/grafana-plugin/) puts logsite under
**Connections** in Grafana, like Loki. You set `logsite alert-server`'s URL and token once,
then panels pick what to show from a list:

| | Shows |
|---|---|
| **Summary** | Errors, groups, new groups and incidents, for stat panels |
| **Error groups** | Errors grouped by cause, with code links and history |
| **Incidents** | The cause, the deploy, the suspect commit, and what to check first |
| **Ranked explanations** | Each incident's explanations, with the evidence for and against |
| **History events** | First seen, gone and came back, as annotations |

It comes with a **logsite errors** dashboard, on the data source's **Dashboards** tab.
Grafana keeps the token on its server, so the browser never sees it.

**Installing it:** until the plugin is in Grafana's catalog, unzip the release's
`rajpaynaik-logsite-datasource-<version>.zip` into Grafana's plugins directory and allow it
unsigned:

```ini
[plugins]
allow_loading_unsigned_plugins = rajpaynaik-logsite-datasource
```

Grafana Cloud and other managed Grafana load only plugins signed by Grafana Labs, so they
need the catalog version.

## Error groups on the cost dashboard

`logsite grafana --error-groups http://<logsite host>:8085` adds an **Error groups** panel
at the top of the dashboard: errors grouped by cause for the dashboard's time range.

| Trend | Count | Error | Root cause | Raised at | Code | Logged at |
|---|---|---|---|---|---|---|
| new | 1800 | OrderProcessingException: Unable to save order ORD-&lt;n&gt; | PSQLException: canceling statement due to statement timeout | OrderRepository.java:87 | `return jpa.save(order);` | OrderController.java:56 |
| new | 640 | Retry failed for order ORD-&lt;n&gt; | | | | OrderService.java:170 |

"Raised at" and "Logged at" link to the code. The panel reads `logsite alert-server`'s
`/errors` through the free
[Infinity](https://grafana.com/grafana/plugins/yesoreyeram-infinity-datasource/) data
source. Add one with the token as a header, so it never appears in a dashboard:

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

Then pick it in the dashboard's **logsite (Infinity)** variable. The panel is optional, so
the catalog dashboard doesn't need the plugin.

### History in the dashboard

With the [history store](history.md), the panel also shows which errors came back after a
fix, and how long each has been around:

| Trend | Count | Error | History | … | First seen | Fixed before by |
|---|---|---|---|---|---|---|
| back | 660 | IllegalStateException: Shipping label request failed | ↩ back, gone after v1.3 | … | 2026-08-03 | 41bdce40 Retry carrier timeouts with backoff (v1.3) |
| new | 40 | KeyError: 'zone' | first time ever | … | 2026-10-18 | |

A **logsite history** annotation (toggle it at the top) puts red markers on the time graphs
where an error was first seen, went away, and came back. Hovering one shows, for example,
*came back: IllegalStateException: Shipping label request failed · was gone 2026-09-03,
fixed by 41bdce40 Retry carrier timeouts with backoff*. It reads `/history/events` through
your Infinity data source. With several, pick one in the dashboard's annotation settings.
