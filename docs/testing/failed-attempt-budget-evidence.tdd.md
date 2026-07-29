# Failed-attempt budget evidence TDD

## Source and journey

This repair was derived from the fresh Phase 2 adversarial budget review.
For a failed paid formal run, an evaluator must be unable to hide provider
usage, retries, interrupted reservations, or an over-reservation cost behind
forged zero totals or a false `budget_limit_breached` value.

## RED evidence

The first attack test was run before production changes:

```text
.venv/bin/python -m pytest -q tests/test_formal_failure_evidence.py \
  -k 'visible_usage_hidden_by_zero_budget'
2 failed: both non-zero usage with zero budget and an approximately CNY 19
usage claim were accepted.
```

A second RED test confirmed that an error model call could carry usage while
claiming a matching budget:

```text
.venv/bin/python -m pytest -q tests/test_formal_failure_evidence.py \
  -k 'usage_on_an_error_model_call'
1 failed: the inconsistent error-call protocol was accepted.
```

## GREEN guarantees

| Guarantee | Evidence | Type |
|---|---|---|
| Success usage is repriced with the canonical DeepSeek snapshot and matched one-for-one to an anonymous settled ledger bucket | `test_failed_attempt_accepts_canonical_usage_matched_to_ledger_bucket` | Integration |
| Non-zero usage cannot be reported with zero budget, including a real overrun hidden behind `breach=false` | `test_failed_attempt_rejects_visible_usage_hidden_by_zero_budget`, `test_failed_attempt_rejects_false_breach_with_hidden_bucket_cost` | Adversarial |
| Error calls cannot carry success-only usage or response fields | `test_failed_attempt_rejects_usage_on_an_error_model_call` | Protocol |
| Success retries and error attempts require enough reserved or uncertain ledger buckets | `test_failed_attempt_matches_retries_to_uncertain_attempt_buckets` | Integration |
| Current-run reservations are bound to canonical price and `max_output_tokens` | `test_failed_attempt_rejects_coordinated_underreservation` | Adversarial |
| A known cost above its reservation remains visible as an uncertain bucket | `test_cost_over_reservation_commits_observed_cost_and_blocks_next_attempt` | Ledger integration |
| Run and cumulative totals, counts, remaining budget, and breach are recomputed from same-transaction anonymous buckets | failed-attempt bundle validation and ledger snapshot tests | Integration |

## Verification

```text
make verify PYTHON=.venv/bin/python
ruff: PASS
mypy: PASS
contracts: fresh
pytest: 320 passed
branch coverage: 82.34%
pip-audit: no known vulnerabilities
```

The final verification includes the false-breach tamper case. No network,
model, paid call, secret, private artifact, or persistent budget ledger was
accessed.
