# logsite app: set up and see errors in your browser

No terminal needed after the first command. Run it in your project's folder:

```bash
pip install logsite
logsite app
```

Your browser opens on `http://127.0.0.1:8077`.

## Set up, the first time

1. **Connect your logs.** Pick Loki, OpenSearch / Elasticsearch, CloudWatch, Sentry, or a log
   file (an OpenTelemetry Collector export works too), fill in the form, and press **Test
   connection**: it shows a few real lines from your store and how many errors it holds.
2. **Your code.** logsite maps errors to lines in the folder you started it from. Give the
   service a name for the Slack messages.
3. **Slack.** Paste an incoming webhook URL and press **Send test message**, or skip it.
4. **Where to keep errors.** On this machine (SQLite), or in your Postgres (with a test
   button). Tick **Start watching** to begin.

The first check reads the last 24 hours, so the Errors page fills up at once; it doesn't post
those to Slack. After that, logsite checks every 5 minutes and posts new errors, regressions
and spikes (see [Watch production](watch.md)).

## The pages

- **Errors**: causes, newest first, for the last hour, day or week. Click one for where it
  was raised (the line of code, with a link to GitHub or GitLab), the last 7 days, recent
  occurrences with their stack traces, and the alerts sent.
- **Write a failing test**: [`logsite repro`](repro.md) on that error. **Save** puts the
  test in the project's `tests/`; it's never overwritten.
- **Watching**: on or off, the last check, and what it found.
- **Settings**: the same forms as the setup, to change anything later.

## Where settings go

Everything goes in `logsite.toml`, in the project, except passwords, tokens, the Slack URL and
the Postgres URL. Those go in `~/.config/logsite/secrets.json`, a file only your user can read,
and `logsite.toml` names them (`password_env = "LOGSITE_LOKI_PASSWORD"`). So `logsite.toml` is
safe to commit, and the command-line tools read the same settings.

## For a team

```bash
logsite app --host 0.0.0.0 --token "$LOGSITE_APP_TOKEN" --watch --no-browser
```

Run it on a server or in a container that can reach your log store. People sign in with the
token. Put it behind your usual HTTPS proxy.

## Safety

On `127.0.0.1` only your browser can use it. The page carries a random token that every
request must send back, which another website can't read, and requests for any other host
name are refused, which stops DNS rebinding. On another address, `--token` is required.
