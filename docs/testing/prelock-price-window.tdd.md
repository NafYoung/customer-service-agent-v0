# Formal pre-lock price-window TDD evidence

## Source and user journey

This focused TDD run was derived from the formal holdout reliability review.

As the formal holdout operator, I need an insufficient DeepSeek price-validity
window to fail during model construction, so the one-time formal start receipt
is not consumed by a run that cannot make its first paid attempt.

## RED

Checkpoint: `d0989ba`

Command:

```text
.venv/bin/python -m pytest -q tests/test_paid_price_window.py \
  -k 'client_construction_rejects or formal_cli_rejects_short'
```

Observed result: `2 failed`. Client construction did not raise when only 31
seconds remained for a 30-second timeout plus the 2-second safety margin, and
the formal CLI reached the start-lock function once.

## GREEN

The budget guard now validates the combined timeout and safety margin before
committing the timeout binding. The read-only Eval CLI treats that pricing
failure as a configuration failure and closes the guard before any formal lock.
The existing per-attempt and post-response price checks remain active.

Command:

```text
.venv/bin/python -m pytest -q tests/test_paid_price_window.py \
  tests/test_semantic_calibration_cli.py tests/test_readonly_eval_cli.py
```

Observed result: `50 passed`.

Coverage command:

```text
.venv/bin/python -m pytest -q tests/test_paid_price_window.py \
  tests/test_deepseek_budget.py tests/test_openai_compatible_client.py \
  tests/test_eval_runtime_snapshot.py --cov=app.agent.deepseek_budget \
  --cov=app.agent.openai_compatible --cov=app.agent.factory --cov-branch
```

Observed result: `73 passed`; focused branch coverage was `80%`.

## Test specification

| Guarantee | Test | Type | Result |
|---|---|---|---|
| Client construction rejects a price window shorter than timeout plus safety margin | `test_client_construction_rejects_price_window_shorter_than_timeout` | Unit | PASS |
| Formal CLI fails before the O_EXCL start lock, with zero HTTP calls and no receipt | `test_formal_cli_rejects_short_price_window_before_start_receipt` | Integration | PASS |
| A window that becomes too short after construction is still blocked before reservation | `test_request_is_blocked_before_reservation_when_price_window_is_too_short` | Integration | PASS |
| Calibration and read-only CLI configuration-error paths remain green | focused CLI suites | Regression | PASS |

## Coverage and known gaps

The focused adapter and budget modules met the 80% branch-coverage threshold.
The repository-wide `make verify` gate is delegated to the parent integration
run. No network, model, environment-secret, private artifact, or real
budget-ledger access occurred.
