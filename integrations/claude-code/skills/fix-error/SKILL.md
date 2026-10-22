---
name: fix-error
description: Fix a production error end to end - find where it's raised, what changed, make the fix, and run the tests until they pass. Use when the user says "fix this production error", "fix error <id>", "fix the timeout in checkout", or asks to fix an incident logsite reported.
argument-hint: "[error id, title or file:line]"
---

# Fix a production error

1. Identify the error. With an id or title from logsite, call `error_summary` (or
   `investigate`) and pick that group; with a file:line, call `production_context` on it.
   Given a log file or a pasted stack trace instead, save it to a file and pass it as
   `log_file` in the next step. Note `raised_at`, the root cause, `history` (a regression
   and its `fixed_by` commits) and the suspect commits.
2. Reproduce it before changing any code: call `reproduction_case` with the group id (and
   `log_file`). For Python it returns a pytest `skeleton`: write it to `test_file`, and
   replace its TODOs from the case (`inputs`, `missing`) and the code. For Java or Go,
   write the test from the case the same way: call `entry`, fake `fake`, use `inputs`.
3. Run it with `logsite agent --expect <id> -- <test command for that file>`. Only
   `reproduced` counts. On `wrong failure`, change the test (not the code) as the message
   says: don't raise the error in the test, don't fake anything on the path before
   `raised_at`. If it can't be reproduced, say why and what the logs are missing.
4. Call `production_context` on the file and line where it's raised. Read what it reports
   before editing: an earlier fix you must not undo, other errors passing through, log
   calls that are very busy or expensive there.
5. Read the code and the suspect commit (`git show <sha>`). If it's a regression, compare
   with the commit that fixed it before: the right fix often restores what that commit did.
6. Make the smallest change that fixes the cause, not the symptom. Keep error logging
   (`logger.exception` / `exc_info=True`, or the language's equivalent) so the next
   occurrence is still visible.
7. Run the reproduction test, then the whole suite, through agent mode
   (`logsite agent -- <the test command>`; the plugin's hook does this for you). If the
   fixed code now reaches another dependency, fake that in the test. Repeat until green.
8. Summarize: the cause, the test that reproduces it (and that it said `reproduced`
   before the fix), the change. Don't commit unless asked.
