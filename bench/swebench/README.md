# Real-bug benchmark: SWE-bench issues that look like production errors

The [repro benchmark](../repro/README.md) uses a demo service with planted bugs. This one
uses real bugs: issues from [SWE-bench Verified](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified)
(500 human-checked GitHub issues from 12 Python projects, each with the commit before the
fix, the real fix, and the tests that check it), filtered to the ones that read like a
production error: a traceback raised inside the project's own code.

## Selecting the issues

```bash
mkdir -p bench/swebench/data && cd bench/swebench/data
for o in 0 100 200 300 400; do
  curl -sf "https://datasets-server.huggingface.co/rows?dataset=princeton-nlp/SWE-bench_Verified&config=default&split=test&offset=$o&length=100" -o rows_$o.json
done
cd ../../.. && python bench/swebench/select_issues.py
```

| | Issues |
|---|---|
| SWE-bench Verified | 500 |
| with a traceback | 69 |
| raised in the project's own code | 59 |
| the fix changes a file on the traceback | 46 |
| the fix changes the deepest project frame's file | 33 |

`shortlist.json` keeps the 46 whose fix is on the traceback, at most 8 per project, different
exception types first: 36 issues from Django, sympy, scikit-learn, astropy, matplotlib,
requests, xarray and pylint.

## logsite alone (free)

```bash
cd bench/swebench/repos
for r in django/django sympy/sympy scikit-learn/scikit-learn astropy/astropy \
         matplotlib/matplotlib psf/requests pydata/xarray pylint-dev/pylint; do
  git clone -q --filter=blob:none --no-checkout https://github.com/$r.git
done
cd .. && python run_logsite.py      # ~300 MB of clones; results/logsite.json
```

Each issue's traceback, as an error log would carry it (no issue text, no reproduction
snippet), goes through `logsite repro --write-test` at the issue's commit. Scored against
the real fix:

| | 36 issues |
|---|---|
| read the traceback, right exception | 36 |
| the line it says raised is in the fix's file | 25 |
| ... in the function the fix changed | 14 |
| ... within 3 lines of the fix | 10 |
| the function the fix changed is somewhere on the trace | 28 |
| entry function found, test written and compiles | 36 |
| the caller's line (`est.fit_transform(X)`, from a script or notebook) in the test | 19 |

Reporters ran other versions than the issue's commit, so the line in a traceback is often
a few lines off (scikit-learn-14894 reports line 302; it's 291 at the issue's commit).
logsite moves it to where the code it shows is now (`local_at`), and that is what's scored;
the function is scored as it is at the issue's commit.

The tests are skeletons here: a library bug's traceback doesn't carry the arguments of the
call, so most tests have `TODO`s, which is expected. The question for the Claude arms is
whether the skeleton and the pointers help.

## Caveats

- **Memorization.** These issues are from 2013 to 2023, before the model's training cutoff.
  Issues opened after it (SWE-bench-Live, or picked by hand) are needed for numbers to trust.
- **Few issues.** Only 69 of the 500 have a traceback; 20 to 30 is about the ceiling here.
