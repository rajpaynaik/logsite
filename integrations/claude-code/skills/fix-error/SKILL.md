---
name: fix-error
description: Fix a production error end to end - find where it's raised, what changed, make the fix, and run the tests until they pass. Use when the user says "fix this production error", "fix error <id>", "fix the timeout in checkout", or asks to fix an incident logsite reported.
argument-hint: "[error id, title or file:line]"
---

# Fix a production error

1. Identify the error. With an id or title from logsite, call `error_summary` (or
   `investigate`) and pick that group; with a file:line, call `production_context` on it.
   Note `raised_at`, the root cause, `history` (a regression and its `fixed_by` commits)
   and the suspect commits.
2. Call `production_context` on the file and line where it's raised. Read what it reports
   before editing: an earlier fix you must not undo, other errors passing through, log
   calls that are very busy or expensive there.
3. Read the code and the suspect commit (`git show <sha>`). If it's a regression, compare
   with the commit that fixed it before: the right fix often restores what that commit did.
4. Make the smallest change that fixes the cause, not the symptom. Keep error logging
   (`logger.exception` / `exc_info=True`, or the language's equivalent) so the next
   occurrence is still visible.
5. Run the tests through agent mode (`logsite agent -- <the test command>`; the plugin's
   hook does this for you). Fix what fails; repeat until it's green. Add a test that
   reproduces the error if there isn't one.
6. Summarize: the cause, the change, the test that covers it. Don't commit unless asked.
