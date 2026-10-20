# logsite

Production errors in Grafana, grouped by cause and mapped to the line of code that raised
them, with the incident they belong to, the deploy and the commit before it, and the ranked
explanations with the evidence for and against each.

[logsite](https://github.com/rajpaynaik/logsite) is an open-source tool that reads the logs you
already have (Loki, OpenSearch / Elasticsearch, CloudWatch Logs or a log file) next to your
code. No SDK and no agent in your services. This data source shows what it finds.

## What you get

**Error groups.** Thousands of stack traces become a few causes: the exception chain and the
frames in your code. IDs, timestamps and framework frames don't split a group. Each row has
the trend (new, back after a fix, spiking), the count, the root cause, the line that raised
it (linked to the code), the code itself, and the history: when it was first seen and which
commit fixed it last time.

**Incidents.** Groups that belong together (one request's errors, a caller and the service it
called, errors that started together), with the cause first, the deploy just before it and
the suspect commit.

**Ranked explanations.** For each incident: a code change, more traffic, a failing dependency,
a pool or limit running out, a deadlock, bad config, bad data... Each one comes with the
evidence for and against it, what the logs can't tell, and the next check to tell them apart.
Scores rank the explanations by evidence; they aren't probabilities.

**History annotations.** Markers on your time graphs where an error was first seen, where it
went away, and where it came back.

**A dashboard**, included: open the data source's **Dashboards** tab and import
**logsite errors**.

## Requirements

`logsite alert-server` running next to your code, where Grafana's server can reach it. One
container is enough:

```bash
pip install logsite        # or the Docker image in the repository
logsite alert-server --host 0.0.0.0 --token "$LOGSITE_ALERT_TOKEN" --sync-every 15m
```

It reads the `logsite.toml` in your repository, which says where your logs are. See
[the Grafana guide](https://github.com/rajpaynaik/logsite/blob/main/docs/grafana.md).

## Setup

1. **Connections → Add new connection → logsite**, then **Add new data source**.
2. **URL**: where the alert-server listens, e.g. `http://logsite:8085`.
3. **Token**: the alert-server's `--token`. Grafana keeps it on its server and adds it to each
   request; the browser never sees it.
4. **Save & test**: it says how many errors the server found in the last 15 minutes.

Or provision it:

```yaml
apiVersion: 1
datasources:
  - name: logsite
    type: rajpaynaik-logsite-datasource
    jsonData:
      url: http://logsite:8085
    secureJsonData:
      token: ${LOGSITE_ALERT_TOKEN}
```

## Queries

| Show                | One row per                                                                     |
| ------------------- | ------------------------------------------------------------------------------- |
| Summary             | the time range: errors, before, groups, new groups, incidents (for stat panels) |
| Error groups        | error group, new and spiking first                                              |
| Incidents           | incident, with the most likely explanation and what to check first              |
| Ranked explanations | explanation of each incident, with the evidence for and against                 |
| History events      | first seen / gone / came back (for annotations)                                 |

**Labels** narrows the Loki selector, like an alert's labels do: `app=$app, namespace=prod`.
The keys must be listed in `[alerts] selector_labels` in `logsite.toml`.

The same summary also goes to Slack with your Grafana alerts: point a webhook contact point
at the alert-server's `/grafana/alert`.
