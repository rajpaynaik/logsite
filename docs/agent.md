# Agent mode

AI coding assistants run the tests again and again, and read every line each time: 30 failing
tests that share one cause, 40 frames of framework code each, the whole output again after
every attempted fix. `logsite agent -- <command>` runs the command and prints one group per
cause, where it was raised in your code, and what changed since the last run:

```
$ logsite agent -- pytest
pytest: 30 failed, 12 passed · 2 cause(s) · exit 1 · 0.3s

1. 27 test(s)  sqlite3.OperationalError: no such column: discount_code
   raised   app/orders/repository.py:10 in get()
            10 │ cur = self.db.execute("select id, total, discount_code from orders where id = ?", (order_id,))
   tests    tests/test_orders.py ×27 (test_order_0, test_order_1, test_order_2, …)

2. 3 test(s)  AssertionError: assert total(<n>) == <n>
   raised   tests/test_tax.py:4 in test_tax_0()
            4 │ assert total(1999) == 2099
   detail   assert 2199 == 2099
   detail   +  where 2199 = total(1999)
   tests    tests/test_tax.py ×3 (test_tax_0, test_tax_1, test_tax_2)

full output: ~/.cache/logsite/agent/0c639ac1d6102826.log · one failure in full: logsite agent --show N
```

That's 525 lines of pytest output. After fixing the first cause:

```
$ logsite agent -- pytest
pytest: 3 failed (was 30), 39 passed · 1 cause(s) · exit 1 · 0.2s
✓ fixed   sqlite3.OperationalError: no such column: discount_code  (27 test(s))

1. 3 test(s)  AssertionError: assert total(<n>) == <n>  (still failing)
   …
```

| Recognised | Grouped by |
|---|---|
| pytest | the exception and the line of your code that raised it; assertions by their pattern, per test file |
| `go test`, `go build` | `t.Errorf` messages with values masked, panics by the frame of yours they happened in |
| Maven (surefire), Gradle | the Java exception chain and the deepest frame of yours (JUnit's frames are library code) |
| tsc, javac, go, gcc-style, mypy | the error code and message with numbers masked, names kept: `'totl'` and `'shippng'` are two typos |

- **Nothing is lost.** The exit code is the command's own; the full output is saved and its
  path printed; `--show N` prints one failure as the tool wrote it; output it can't parse is
  shown as its last 40 lines; and a summary is never printed when the raw output is shorter.
- **What changed** is kept per project and command in `~/.cache/logsite/agent` (or
  `$XDG_CACHE_HOME`), never in your repo.
- `--format json` for tools.

With Claude Code, the [plugin](claude-code.md) (or its hook alone, `logsite hook claude`) does
this for every test and build command. For other assistants, tell it once, for example in
`CLAUDE.md` / `AGENTS.md`:

```markdown
Run tests and builds through logsite: `logsite agent -- pytest`, `logsite agent -- go test ./...`,
`logsite agent -- mvn -q test`, `logsite agent -- npx tsc --noEmit`. For one failure in full:
`logsite agent --show N`.
```
