# Diagnostic paid-evidence TDD

## Scope

This change binds the canonical 10-case diagnostic bundle to its actual model
calls and persistent budget evidence. It preserves an explicit offline
reference mode, but that mode cannot claim DeepSeek observations or paid
provider attempts.

## RED evidence

- Commit: `0f1b4c8` (`test: reproduce unbound diagnostic paid evidence`)
- Command:
  `.venv/bin/python -m pytest -q tests/test_diagnostic_paid_evidence.py tests/test_dev_repeat_paid_gate.py::test_dev_repeat_payload_cannot_be_relabelled_as_diagnostic --tb=short`
- Result: 17 intended failures. Both the producer and public Schema accepted
  unbound success, retry, error, and zero-call evidence; count fields also
  accepted coercible non-integers.

## GREEN evidence

- Commit message: `fix: bind diagnostic evidence to paid attempts`
- Focused command:
  `.venv/bin/python -m pytest -q tests/test_diagnostic_paid_evidence.py tests/test_dev_repeat_paid_gate.py tests/test_readonly_reporting.py tests/test_readonly_eval_cli.py tests/test_eval_runtime_snapshot.py --tb=short`
- Focused result: 116 passed.
- Full command: `make verify PYTHON=.venv/bin/python`
- Full result: 395 passed; branch coverage 82.56%; Ruff, MyPy, contract
  freshness, and dependency audit passed.

## Guarantees

| Guarantee | Evidence |
|---|---|
| Paid diagnostic identity, canonical price, reservation, and price window are bound to the run | producer and public-Schema paid diagnostic tests |
| Every passed record has consecutive agent calls with the read-only tool contract and no semantic-judge call | call-protocol attack tests |
| Successful usage has one matching settled bucket; retries and errors have an exact uncertain-attempt count | settled/uncertain attack and positive retry/error tests |
| Run attempt count equals the sum of recorded provider attempts; extra settled, uncertain, or reserved buckets fail closed | producer and public-Schema budget attacks |
| `observed_models` is recomputed from successful calls | offline forgery and public bundle tests |
| Zero-call records cannot pass and require an explicit local error | zero-call attack and offline local-failure tests |
| Budget amount and bucket counts reject booleans, strings, and floats | strict count-type tests |
| A valid dev-repeat bundle cannot be relabelled as diagnostic | cross-purpose public-Schema attack |

## Known boundary

An `uncertain` attempt proves that the ledger retained a conservative
commitment after a retry or error; it does not assert an exact provider charge.
The public bundle remains evidence for this prototype run, not a production
safety certification.
