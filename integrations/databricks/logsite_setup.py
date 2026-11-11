# Databricks notebook source
# MAGIC %md
# MAGIC # logsite setup
# MAGIC
# MAGIC 1. In **Jobs** at the top of the page, tick the jobs logsite should explain when they fail.
# MAGIC 2. Leave **Action** on *Add logsite* (or pick *Remove logsite* to take it off them).
# MAGIC 3. Optional: a **History table**, to see if a failure is new and keep every one for SQL.
# MAGIC 4. **Run all**.
# MAGIC
# MAGIC Each picked job gets a **logsite** task that runs only when another task failed, and shows
# MAGIC why on the run's page: the cause, the line of your code, the tasks and the commit. Jobs you
# MAGIC don't pick aren't changed.
# MAGIC
# MAGIC Keep this notebook next to **logsite_explain**: that's the notebook the task runs.

# COMMAND ----------

# MAGIC %pip install --quiet logsite

# COMMAND ----------

from logsite.backends.databricks import SdkApi
from logsite.databricks import list_jobs

api = SdkApi()  # as you: it changes only the jobs you can manage
jobs = list_jobs(api)
labels = {f"{j['name']} ({j['job_id']})": j["job_id"] for j in jobs}

dbutils.widgets.multiselect("jobs", "(none)", ["(none)", *labels], "Jobs")
dbutils.widgets.dropdown("action", "Add logsite", ["Add logsite", "Remove logsite"], "Action")
dbutils.widgets.text("table", "", "History table, e.g. main.ops.logsite_errors (optional)")

watched = [j["name"] for j in jobs if j["watched"]]
print("logsite explains these jobs now:", ", ".join(watched) if watched else "none yet")

# COMMAND ----------

import html
import os
import re

from logsite.databricks import apply

# each choice ends with "(<job id>)": a job's name may have commas, the joined value too
picked = {int(i) for i in re.findall(r"\((\d+)\)(?=,|$)", dbutils.widgets.get("jobs"))}
add = dbutils.widgets.get("action") == "Add logsite"
table = dbutils.widgets.get("table").strip() or None
here = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
explain = os.path.join(os.path.dirname(here), "logsite_explain")

done = apply(api, jobs, picked, add, explain, table)
rows = "".join(f"<tr><td>{html.escape(name)}</td><td>{html.escape(what)}</td></tr>"
               for name, what in done)
summary = (f"<table><tr><th align=left>Job</th><th align=left>logsite task</th></tr>{rows}"
           "</table>" if done else "<p>No jobs picked: tick some in <b>Jobs</b> at the top.</p>")
displayHTML(f"<h3>Done</h3>{summary}<p>When a picked job fails, open the failed run and its "
            "<b>logsite</b> task to see why.</p>")
