---
name: investigate
description: Find out what's failing in production and why, from the project's logs. Use when the user asks "what's broken in production", "why is X erroring", "what happened after the deploy", "is this error new", or pastes an alert or a stack trace from production.
---

# Investigate production errors

Use the logsite MCP tools; they read the project's log store and git history and return
facts, not guesses.

1. Call `investigate` (pass `since_deploy` if the user names a release). It returns
   incidents: the cause, the root cause, the line of code that raised it, the errors that
   followed, the deploy just before and suspect commits.
2. For each incident's cause, look at `history` on the group: `regression: true` means it
   was fixed before and came back; `fixed_by` lists the commits that fixed it then. Say so
   plainly: a regression usually means a recent change undid that fix.
3. Open the raising line (`raised_at`) and the suspect commits (`git show <sha>`) and
   explain the likely cause in two or three sentences, pointing at the exact change.
4. If the user pasted a trace id, call `request_timeline` with it to follow that request.

Report: the incident (when, how many errors), the cause and where it's raised, what
changed (deploy, commit, the lines), and what fixed it last time if it's a regression. Don't
reconstruct masked values. Don't change code unless asked; offer `/logsite:fix-error`.

If the tools say no backend is configured, tell the user to add `[backend]` to
`logsite.toml` (see https://github.com/rajpaynaik/logsite/blob/main/docs/configuration.md).
