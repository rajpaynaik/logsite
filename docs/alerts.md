# Alerts that explain themselves

When a Grafana alert fires, `logsite alert-server` sends a report with it: what failed, the
error as it was logged, the line of code that raised it, what followed, and the likely cause
with its evidence. Whoever is on call sees it in Slack next to the page, without opening a
stack trace.

```
🔴 xyz job failed: the shipping-label job is failing (firing since 01:55 UTC)
732 error(s) in 2 group(s), 2 new · Open in Grafana · Silence

1. What failed: java.lang.IllegalStateException: Shipping label request failed for shipment 88121
    366 error(s) since 01:52 UTC (732 with what followed) · back after a fix
    Root cause: java.net.SocketTimeoutException: Read timed out (thrown in java.net)
    Raised at: src/main/java/com/example/shipping/CarrierApiClient.java:58 in requestLabel()  `return restTemplate.postForObject(carrierUrl + "/labels", request, LabelResponse.class);`
    From the log (latest, 02:52 UTC):
      2026-10-19 02:52:00 ERROR [shipping] [traceId=tr-00480] c.e.s.ShipmentEventListener : label failed
      java.lang.IllegalStateException: Shipping label request failed for shipment 88121
          at com.example.shipping.CarrierApiClient.requestLabel(CarrierApiClient.java:58)
      Caused by: org.springframework.web.client.ResourceAccessException: I/O error on POST request for "https://carrier.example.com/labels": Read timed out
      Caused by: java.net.SocketTimeoutException: Read timed out
    Then: Record processing failed topic=shipment-events offset=<n> · 366 — same requests (366 shared trace id(s))
    Seen before: 2026-08-05 → 2026-09-03
    Likely cause (strong evidence): The change in v1.4: 60b67447 Lower carrier read timeout to 3s
      • v1.4 was deployed 4 min before the errors started
      • 60b67447 changed the lines around src/main/java/com/example/shipping/CarrierApiClient.java:58, where it's raised
      • the error wasn't there in the window before
      • it was fixed before (an earlier release) and came back: the change may undo that fix
    Or: A dependency failing: SocketTimeoutException from the service requestLabel() calls
    Check first: read `git show 60b67447 -- src/main/java/com/example/shipping/CarrierApiClient.java`; revert it (or roll back) in staging and see whether the error stops
    Example run: `logsite trace tr-00480`
```

That's a dry run with a real Grafana 13 alert, on the shipping example: a fix in v1.3,
undone in v1.4. In Slack, the log excerpt is a code block and the code location links to
your repository.

## Set it up

1. **Run it next to your code.** It reads the code to tell your frames from library ones.

   ```bash
   export LOGSITE_ALERT_TOKEN=$(openssl rand -hex 16) SLACK_WEBHOOK_URL=https://hooks.slack.com/...
   logsite alert-server --host 0.0.0.0 --port 8085
   ```

2. **Point Grafana at it.** **Alerting → Contact points → Add → Webhook**, with URL
   `http://<logsite host>:8085/grafana/alert`, **Authorization header** scheme `Bearer` and
   the token as credentials. Route your alerts to it. It can sit next to your existing Slack
   or PagerDuty contact point, so the page goes out as usual and the report lands at the same
   time.

```toml
[alerts]
slack_webhook_env = "SLACK_WEBHOOK_URL"
# webhook = "https://example.com/hooks/logsite"   # gets {alert, labels, text, summary}
token_env = "LOGSITE_ALERT_TOKEN"   # required unless it only listens on localhost
lookback_minutes = 30
top = 3                             # groups per alert
selector_labels = ["service_name"]  # alert labels that narrow the Loki selector
dedupe_minutes = 30                 # alerts with the same cause: one report, then one line each
```

## What's in the report

| | |
|---|---|
| **From the log** | The latest occurrence as logged: the message, each exception in the chain, and the frame of yours where it was raised. Library frames are left out, values masked |
| **Likely cause** | The same ranking as [`logsite investigate --report`](incidents.md#the-incident-report). When the evidence doesn't point anywhere, it says "unclear" |
| **Also failing** | Other failing groups, after up to two incidents per alert. With only steady errors, the top groups instead |

**One incident, many alerts.** When several alerts fire for the same cause within
`dedupe_minutes` (30), the first gets the report and each of the others one line: "same cause
as … · now also in checkout-service · 8 alerts for this incident so far".

**Which errors.** For each firing alert, logsite reads from `lookback_minutes` before it
started until now, against the same span just before. On Loki, it narrows to the alerted
service with the alert's labels (`selector_labels`). It posts to `slack_webhook` and/or
`webhook`, and returns the summary in the response.

`GET /errors?from=&to=` serves the same summary for any range (Grafana's `${__from}` and
`${__to}` work), for the [Error groups panel](grafana.md#error-groups-on-the-cost-dashboard).

## For a team: one container

`alert-server` also serves the dashboard panel and its history annotations. With
`--sync-every 15m`, it keeps the [history](history.md) filled too. In a container, mount your
repository (for code lines and the commits that fixed an error) and keep the history on a
volume:

```bash
docker build -t logsite integrations/docker
docker run -d -p 8085:8085 \
  -v "$PWD:/repo:ro" -v logsite-history:/data \
  -e LOGSITE_ALERT_TOKEN=<shared secret> logsite
```

Or `docker compose -f integrations/docker/compose.yaml up -d` from the repository. Fill the
history once with `docker exec <container> logsite --config-dir /repo sync --backfill 30d`.

## Databricks jobs

`alert-server` also takes a Databricks job's failure webhook at `/databricks`, and posts the
failed run explained. See [Databricks](databricks.md#in-seconds-a-jobs-failure-webhook).
