# Alloy rules: drop noisy logs before you pay for them

`logsite pipeline` writes Grafana Alloy rules that drop or sample your noisiest log lines
before they're shipped. Each rule targets one log call, sized from its real volume and priced
like [`logsite cost`](cost-and-leaks.md).

```bash
logsite pipeline --max-per-window 100000 --drop-level debug -o logsite.alloy
```

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

## Where rules come from

- **Your config:** `drop` and `sample` in `[pipeline]`.
- **Volume:** lines above `max_per_window` are sampled down to it, and lines at a
  `drop_levels` level are dropped.

## What it never touches

- **Only `trace`, `debug`, `info`, `notice` and `success` log calls get a rule.** The level
  in the code is checked first, and no config can override it. Warnings, errors, levels set
  at runtime (`log.log(level, …)`) and unknown levels are skipped, each with the reason.
- **Lines that look like errors** (`error`, `exception`, `traceback`, `critical`, `fatal`)
  are always kept, even from a call that logs at `info`.
- **logsite only writes the file.** It never applies rules to Alloy, and the MCP server
  can't write config. Review the file, then deploy it yourself.

## How lines are matched

By the file and line fields records carry, through a temporary label that's removed before
anything reaches Loki. Records without them are matched by message (`match = "template"`,
with your stream `selector`). Messages with too little fixed text get no rule.

Wire it in with `forward_to = [loki.process.logsite.receiver]` in your source. Logs go on to
`forward_to` in `[pipeline]` (default `loki.write.default.receiver`).

Tested with Alloy 1.20 and Loki: of 2,530 lines, the noisy line was kept at 1 in 10, the
debug line dropped, and all 30 error lines kept.

## Joining stack traces

`--multiline` also joins each stack trace into one entry before the rules run, so Loki and
logsite see whole exceptions. See
[stack traces split into lines](errors.md#stack-traces-split-into-lines-loki).

## Configuration

```toml
[pipeline]
drop = ["app/health.py:12"]
sample = { "app/api.py:88" = 0.01 }   # keep 1 in 100
max_per_window = 100000
drop_levels = ["debug"]
multiline = true                    # join stack traces into one entry (--multiline)
# match = "auto"                    # location | template
# selector = '{app="checkout"}'     # template rules: the stream as Alloy sees it
# forward_to = "loki.write.default.receiver"
```
