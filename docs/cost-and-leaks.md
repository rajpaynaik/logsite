# Cost and leaks

## Cost per line

In most services a handful of log calls write most of the bytes. `logsite cost` ranks every
log call by projected monthly spend (usage over the configured window, scaled to 30 days):

```
 per month     volume   events      avg  line
   $1,840   3.7 TB    9.1B    410 B  app/api.py:88  'request received %s %s'
     $212   424.0 GB  1.3B    330 B  app/cache.py:41  'cache miss for %s'
```

| Backend | Bytes come from |
|---------|-----------------|
| Loki with `aggregate = true` | `bytes_over_time` per file/line: exact at any volume |
| OpenSearch / Elasticsearch with `aggregate = true` | exact counts × each line's average document size from a sample (marked `~`) |
| CloudWatch Logs | each event's size + the 26 bytes CloudWatch bills per event |
| Others, or without `aggregate` | the size of each record fetched (exact, but capped at `--limit`) |

Set `price_per_gb` to what you pay for ingestion (CloudWatch Logs is about $0.50/GB), and
`price_per_million_events` if your vendor also charges per indexed event. Prices are yours
to set because they vary by vendor, plan and region. Once a price is set, the editor shows
the cost next to each log call and in its hover.

## Leak scanning

LL002 guesses from variable names (`password`, `card_number`) in the code. `logsite leaks`
checks what really reached production: it scans the message and every structured field of
recent log records (up to `--limit`, default 5,000) and groups findings by the log call
that wrote them, secrets first.

```
app/checkout.py:13  1 record(s)  'card {} charged'
    pii    card_number     x1      e.g. ************1111
```

| Kind | Detection |
|------|-----------|
| Secrets | AWS / GitHub / Slack / Stripe / Google keys, JWTs, `Bearer` tokens, private key blocks, `password=` / `api_key:` style values |
| Personal data | Emails, card numbers (Luhn-checked, plus card-style grouping or a nearby word like "card"), IBANs (checksum-validated), US SSNs |

Card numbers are the classic false positive: a bare 16-digit order ID passes the checksum
1 time in 10. logsite only flags one when it is grouped like a card (`4111 1111 …`) or
appears near a card-related word or in a card-named field.

Matched values are never printed in full, so the report is safe to keep in CI logs.
