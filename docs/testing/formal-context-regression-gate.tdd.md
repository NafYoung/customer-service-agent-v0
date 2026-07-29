# Formal context and public regression gate TDD evidence

## Source and journeys

No external plan file was used. The guarantees were derived from the Phase 2
adversarial review.

- A formal holdout caller cannot reach a model without the one-use context
  issued after the exclusive start receipt.
- A v2 holdout cannot be sealed, started, completed, failed, or publicly
  verified without the exact passing 7×4 public regression bundle.
- A price-window-crossing response with known cost below its reservation
  remains representable as conservative `uncertain` evidence.

## RED and GREEN checkpoints

| Guarantee | RED evidence | GREEN evidence |
|---|---|---|
| Programmatic formal calls require a validated context before model or output | `ba238cf`; focused run failed because the old entry point called the model and did not accept a context | `40289d9`; missing, forged-sentinel, wrong-hash, wrong-purpose and wrong-split paths fail with zero model calls and zero output |
| Sealed v2 binds the canonical passing public regression | `bf16b9a`; parser, declaration and start-receipt attacks all failed against the missing gate | `40289d9`; validator checks private containment, permissions, integrity, schema, canonical 7×4 identity, 28/28 strict/security, `pass^4=1`, no errors, unchanged state, settled canonical budget, current source and harness |
| Cross-window known cost remains verifiable without weakening settlement | `7912a90`; real adapter snapshot failed `BudgetSummary` validation because `0.0012 <= 1.002048` | `40289d9`; `uncertain` accepts the known cost, recomputation still commits `max(reservation, known)`, while a settled cost above reservation remains invalid |

## Test specification

| # | What is guaranteed | Test target | Type | Result |
|---|---|---|---|---|
| 1 | Missing or forged formal context makes zero model calls and writes no bundle | `tests/test_dev_repeat_paid_gate.py` programmatic formal context tests | integration/adversarial | PASS |
| 2 | The fixed public regression must be 28/28, safe, state-unchanged, current and owner-only | `tests/test_dev_repeat_paid_gate.py` formal regression gate tests | integration/adversarial | PASS |
| 3 | The v2 manifest and immutable start receipt bind the validated regression identity | `tests/test_holdout_run_lock.py` regression declaration/start tests | protocol | PASS |
| 4 | Completed and failed chains cross-check regression fields and the exact regression bundle under an explicit private root | `tests/test_holdout_run_lock.py` receipt-chain tests | protocol/adversarial | PASS |
| 5 | A real 2xx response crossing `valid_until` produces schema-valid conservative evidence | `tests/test_paid_price_window.py::test_response_crossing_price_window_is_uncertain_and_not_retried` | end-to-end adapter/schema | PASS |
| 6 | Settled evidence still cannot exceed its reservation | `tests/test_paid_price_window.py::test_settled_bucket_still_cannot_exceed_its_reservation` | unit | PASS |

## Validation

Executed on the GREEN worktree:

```text
focused formal/regression/failure/reporting/CLI/price-window tests: 168 passed
full pytest: passed
ruff: passed
mypy: 52 source files passed
schema freshness: passed
branch coverage: 82.84%
pip-audit: no known runtime vulnerabilities
Reference Eval: 8/8
```

`make verify PYTHON=.venv/bin/python` passed. No DeepSeek call, `.env`
access, budget-ledger access, private artifact read, holdout v1 run, or holdout
v2 generation/run occurred.

## Evidence boundary

The exclusive receipts, SHA-256 links, fixed private-root checks and one-use
in-process registry fail closed against accidental bypass and ordinary
programmatic misuse. They do not defend against an actor who can arbitrarily
modify the process memory or files as the same operating-system user.
