# Configuration

`logsite.toml` at your project root, or `[tool.logsite]` in `pyproject.toml`. In the editor,
each file uses the nearest one, so a monorepo can give every service its own:

```toml
window_hours = 24

[backend]
type = "loki"                       # or "opensearch", "elasticsearch", "cloudwatch", "file"
url = "http://localhost:3100"
selector = '{app="checkout"}'
username = "me"
password_env = "LOKI_PASSWORD"      # any key ending in _env is read from the environment
# tenant_id = "team-a"
aggregate = true                    # let Loki count per file/line (see below); needs pathname + lineno
# parser = "json"                   # or "logfmt": how Loki reads pathname/lineno from each line

[lint]
ignore = ["LL004"]
extend_select = ["LL008"]           # opt-in rules on top of the defaults
required_fields = ["trace_id"]      # LL006 for every log; see [context] for situational rules
noisy_threshold = 100000
spike_ratio = 5                     # LL102 / `logsite spikes`: growth vs the previous window
spike_min_count = 1000              # ... and at least this many emits
error_noisy_threshold = 10000       # LL104: error lines firing this often per window
# failure_pattern = '\b(failed|timeout|declined)\b'   # LL007/LL104: what counts as a failure

[context]                           # LL006: what each log call must carry
all = ["request_id"]
error = ["tenant_id", "user_id"]    # who was affected
in_retry = ["attempt"]              # retry loops, @retry / @backoff functions
near_dependency = ["dependency"]    # except blocks around HTTP / DB / client calls
injected = ["trace_id"]             # added outside the code, e.g. by your log shipper
# min_coverage = 0.95               # LL105: share of production records that must carry a field
# min_records = 20                  # LL105: skip lines with fewer sampled records

[cost]
price_per_gb = 0.50                 # what your log store charges per GB ingested
price_per_million_events = 0        # per-event indexing charges, if your vendor has them
# currency = "€"

[timeline]                          # logsite trace
field = "trace_id"                  # or request_id; common spellings (traceId, trace.id) match
service = "checkout"                # this repo's name in the timeline (default: its folder)
[timeline.backend]                  # optional: a wider scope than [backend], e.g. every service
type = "loki"
url = "http://localhost:3100"
selector = '{env="prod"}'

[leaks]
ignore = ["email"]                  # detector kinds to skip
allow = ['@example\.com$']          # regexes for values that are fine, e.g. test data
ignore_fields = ["user_hash"]       # structured fields not to scan
```

Other backends:

```toml
[backend]                           # OpenSearch or Elasticsearch (same _search API)
type = "opensearch"
url = "https://search.internal:9200"
index = "logs-checkout-*"
query = 'service.name:checkout'     # optional Lucene query_string
# timestamp_field = "@timestamp"    # defaults
# message_field = "message"
api_key_env = "OPENSEARCH_API_KEY"  # or username + password_env
aggregate = true                    # count per file/line with a composite aggregation
# file_field = "log.origin.file.name"  # defaults (ECS); must be keyword/numeric fields,
# line_field = "log.origin.file.line"  # e.g. "pathname.keyword" / "lineno"
```

```toml
[backend]                           # AWS CloudWatch Logs: pip install "logsite[cloudwatch]"
type = "cloudwatch"
log_group = "/ecs/checkout"
region = "us-east-1"
# profile = "prod"                  # credentials come from the usual AWS chain
# filter_pattern = "INFO"           # CloudWatch filter syntax
# log_stream_prefix = "web-"
```
