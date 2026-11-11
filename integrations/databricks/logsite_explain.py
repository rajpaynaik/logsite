# Databricks notebook source
# MAGIC %md
# MAGIC # logsite: why did this job fail?
# MAGIC
# MAGIC This notebook is the **logsite** task the setup notebook adds to a job. It runs only when
# MAGIC another task of the job failed, and shows why: each cause with the line of your code that
# MAGIC raised it, the tasks it failed and the commit. You don't need to run it yourself.

# COMMAND ----------

# MAGIC %pip install --quiet logsite

# COMMAND ----------

dbutils.widgets.text("run_id", "", "Job run id")
dbutils.widgets.text("table", "", "History table (optional)")
run_id = int(dbutils.widgets.get("run_id"))
table = dbutils.widgets.get("table").strip()

# COMMAND ----------

from logsite.backends.databricks import SdkApi
from logsite.databricks import (
    create_table_sql,
    explain_run,
    render_html,
    render_text,
    table_schema,
    table_values,
)

api = SdkApi()  # signs in as this job's "Run as" identity: no token

history = None
if table:
    spark.sql(create_table_sql(table))
    job_id = api.get("/api/2.1/jobs/runs/get", run_id=run_id).get("job_id")
    earlier = spark.table(table).where(f"job_id = {int(job_id or 0)}")
    history = [r.asDict() for r in earlier.select("fingerprint", "run_id", "failed_at").collect()]

explanation = explain_run(api, run_id, history)

if table and explanation.causes:
    rows = spark.createDataFrame(table_values(explanation), table_schema())
    rows.write.mode("append").saveAsTable(table)

print(render_text(explanation))
displayHTML(render_html(explanation))
