# bookstore: a demo service with three planted bugs

A small Python service (catalog imports, stock reorders, shipping quotes) used by the
[repro benchmark](../../bench/repro/README.md) and the [repro docs](../../docs/repro.md).
Each bug is there on purpose; the benchmark's incident logs come from running this code.

| Bug | In | What production logs |
|---|---|---|
| Supplier read timeouts are never retried | `bookstore/supplier.py` | `TimeoutError: timed out` |
| ISBNs with spaces are rejected | `bookstore/catalog.py` | `ValueError: invalid ISBN` |
| A renamed setting | `bookstore/shipping.py` | `KeyError: 'rate_per_kg'` |

Plus a harmless one: cache outages, logged as warnings with a traceback.

```bash
cd examples/bookstore
python -m pytest                     # its own tests pass: none of them hit the bugs
logsite repro --from ../../bench/repro/scenarios/supplier-timeout/incident.log --write-test
```
