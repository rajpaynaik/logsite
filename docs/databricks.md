# Databricks

When a Databricks job fails, logsite finds out, works out why, and points at the line in your
code that caused it.

```
$ logsite errors
40 errors in databricks · 2 group(s)

  #  TREND  COUNT  ERROR                      RAISED AT          LOGGED AT
  1  new       28  KeyError: 'customer_id'    transform.py:41    —
  2            12  ValueError: bad amount     udfs.py:8          —
```

28 failed runs with the same error are one row, not 28 alerts.

## Set it up

1. **Copy your workspace URL** from the browser, for example
   `https://adb-1234567890123456.7.azuredatabricks.net`.
2. **Create a token.** To try it, use your own: **Settings → Developer → Access tokens**. For
   production, use a service principal with **Can View** on the jobs: it can read failed runs,
   and can't change or start anything.
3. **Add it to `logsite.toml`:**

   ```toml
   [backend]
   type = "databricks"
   host = "https://adb-1234567890123456.7.azuredatabricks.net"
   token_env = "DATABRICKS_TOKEN"
   jobs = ["nightly-etl", "orders-sync"]   # optional: leave out to watch every job
   ```

4. **Run it:**

   ```bash
   export DATABRICKS_TOKEN=dapi...
   logsite errors          # failed runs in the last 24 hours, grouped by cause
   logsite watch           # check every 5 minutes, post new failures to Slack
   ```

`logsite init` asks for the same three things and tests the connection.

## What it reads

Every job run that ended as failed or timed out, and each task in it that failed. Tasks that
were skipped because an earlier one failed, and runs someone cancelled, aren't errors.

For each failed task: its error and stack trace, the job and task, when it ended, a link to
the run, and the commit it ran when the job runs from git. That commit lets
[`logsite release`](releases.md) tell you which change broke the job.

## Pointing at your code

logsite finds the line in your repository when the job runs code from it:

| The job runs | The trace says | logsite shows |
|---|---|---|
| A Git folder | `/Workspace/Repos/ana@acme.com/etl/src/transform.py` | `src/transform.py` |
| An asset bundle | `/Workspace/Users/.../.bundle/etl/prod/files/src/udfs.py` | `src/udfs.py` |
| A notebook calling your modules | IPython's traceback, with your module's frame | that module's line |

**Spark errors.** When a UDF fails on an executor, Spark wraps it in a `Py4JJavaError`.
logsite unwraps it: the row is the UDF's own error, at the UDF's line.

**Code typed into a notebook cell** has no file to point at. Its errors are still grouped,
with a link to the run.

## Not yet

- **Instant alerts.** `logsite watch` finds a failure within its check interval (5 minutes
  by default). Hearing from a job's own failure webhook in seconds is planned.
- **Errors that don't fail a job**, in driver or executor logs, aren't read yet.
