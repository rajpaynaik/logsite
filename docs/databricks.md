# Databricks

When a Databricks job fails, logsite shows why on the failed run's page in Databricks: the
cause, the line of your code that raised it, the tasks it failed and the commit. No command
line, no server, no token.

> ### 🔴 nightly-etl failed: `ValueError: bad amount`
> **New:** the first time this job failed this way
>
> | | |
> |---|---|
> | **Where** | `src/udfs.py:8` in `parse()` (inside Py4JJavaError) |
> | **Code** | `raise ValueError("bad amount")` |
> | **Tasks** | write |
> | **Commit** | `a1b2c3d4` |
> | **Same cause** | earlier runs 812201, 812110 |

## Set it up

It takes about five minutes, all in the Databricks web page.

**1. Import the two logsite notebooks.** In **Workspace**, open a folder everyone can read,
such as **Shared**, and create a folder named `logsite`. In it, choose **Import → URL** twice:

- `https://raw.githubusercontent.com/rajpaynaik/logsite/main/integrations/databricks/logsite_setup.py`
- `https://raw.githubusercontent.com/rajpaynaik/logsite/main/integrations/databricks/logsite_explain.py`

Keep them together: the setup notebook adds tasks that run the other one.

**2. Open `logsite_setup` and choose your jobs.** At the top of the notebook:

- **Jobs:** tick the jobs logsite should explain when they fail.
- **Action:** leave it on *Add logsite*.
- **History table** (optional): a table name such as `main.ops.logsite_errors`. With it,
  logsite says whether a failure is new and which earlier runs failed the same way, and you
  can query every failure in SQL.

**3. Click Run all.** Each job you ticked gets a **logsite** task. Nothing else in the job
changes. The notebook lists what it did.

To stop, tick the jobs, choose *Remove logsite*, and Run all again.

## When a job fails

Open the failed run: **Workflows → your job → the failed run → the logsite task**. The page
shows each cause, with where it was raised and the code. When the job succeeds, the logsite
task doesn't run.

Your notifications stay as they are. The job's failure email, Teams or PagerDuty message
still arrives, and its link leads to a run that now explains itself.

## See every failure

With a history table, each failure is a row: `failed_at`, `job`, `run_id`, `run_url`,
`cause`, `raised_at`, `tasks`, `commit` and `new`. A few queries to start a dashboard:

```sql
-- the causes that fail jobs most, this week
SELECT cause, raised_at, COUNT(*) AS runs, COLLECT_SET(job) AS jobs
FROM main.ops.logsite_errors WHERE failed_at > now() - INTERVAL 7 DAYS
GROUP BY cause, raised_at ORDER BY runs DESC;

-- new causes: the first time a job failed this way
SELECT failed_at, job, cause, raised_at, run_url
FROM main.ops.logsite_errors WHERE new ORDER BY failed_at DESC;
```

A Databricks SQL alert on the second query tells you about every new cause, through any
notification destination your workspace has.

## Pointing at your code

logsite finds the line in your repository when the job runs code from it:

| The job runs | The trace says | logsite shows |
|---|---|---|
| A Git folder | `/Workspace/Repos/ana@acme.com/etl/src/transform.py` | `src/transform.py` |
| An asset bundle | `/Workspace/Users/.../.bundle/etl/prod/files/src/udfs.py` | `src/udfs.py` |
| A notebook calling your modules | the module's frame | that module's line |

**Spark errors.** When a UDF fails on an executor, Spark wraps it in a `Py4JJavaError`.
logsite unwraps it: the cause is the UDF's own error, at the UDF's line.

**Code typed into a notebook cell** has no file to point at. Its errors are still explained,
with the task that failed.

## Permissions

- **The setup notebook** runs as you, and changes only jobs you can manage.
- **The logsite task** runs as the job's **Run as** identity. It reads its own run, which
  that identity can already do. With a history table, that identity also needs to create and
  write tables in its schema.
- **Packages:** both notebooks install `logsite` from PyPI. If your workspace can't reach
  PyPI, add it to your package mirror first.

## For platform teams: every job, from outside

To watch every job in the workspace from one place, with Slack and the rest of logsite
(`errors`, `release`, the Claude connector), run `logsite watch` with Databricks as its log
store:

```toml
[backend]
type = "databricks"
host = "https://adb-1234567890123456.7.azuredatabricks.net"
token_env = "DATABRICKS_TOKEN"           # a service principal's, with Can View on the jobs
jobs = ["nightly-etl", "orders-sync"]    # optional: leave out to watch every job
```

See [Watch production](watch.md) for where to run it.

## Not yet

- A ready-made dashboard to import. The queries above are the start of one.
- Errors that don't fail a job, in driver or executor logs.
