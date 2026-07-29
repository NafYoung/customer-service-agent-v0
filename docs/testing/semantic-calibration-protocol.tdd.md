# Semantic calibration protocol — TDD evidence

## Source and journeys

This cycle was derived from the Phase 2 adversarial review rather than a
separate plan file.

- As the paid calibration operator, I need the CLI to admit only the frozen
  holdout-eligibility corpus and runtime, so invalid input cannot reach a
  budget guard or model.
- As the holdout evidence reviewer, I need every model call, runtime setting,
  price window, and budget lifecycle to form one consistent timeline, so a
  self-consistent forged harness cannot qualify a run.

## RED

Checkpoint: `679c269 test: reproduce calibration protocol bypasses`

Command:

```text
.venv/bin/python -m pytest -q tests/test_calibration_attestation.py tests/test_semantic_calibration_cli.py --tb=short
```

Result: 15 intended failures. The former implementation accepted non-zero
temperatures, attacker or extra-path endpoints, incomplete call protocol
evidence, out-of-window or naive call timestamps, a 2020 budget identity, and
paid diagnostic mode. A source change after case loading also reached budget
construction.

## GREEN

Checkpoint: `6eeb526 fix: fail close semantic calibration protocol`

Focused command:

```text
.venv/bin/python -m pytest -q tests/test_calibration_attestation.py tests/test_semantic_calibration_cli.py tests/test_semantic_calibration.py --tb=short
```

Result: `61 passed`.

Static checks:

```text
.venv/bin/python -m ruff check evals/calibration_attestation.py evals/run_semantic_judge_calibration.py tests/test_calibration_attestation.py tests/test_semantic_calibration_cli.py
.venv/bin/python -m mypy --follow-imports=skip evals/calibration_attestation.py evals/run_semantic_judge_calibration.py
```

Result: both passed.

## Test specification

| Guarantee | Evidence | Type | Result |
|---|---|---|---|
| Paid calibration rejects diagnostic mode and custom fixture/case paths before settings, budget, or model construction | `tests/test_semantic_calibration_cli.py` preflight tests | integration | PASS |
| The canonical 49-fixture and seven-case content is loaded and validated before a second clean-source check immediately preceding budget construction | post-load drift test and canonical calibration tests | integration | PASS |
| Only temperature `0`, canonical model, and official DeepSeek HTTPS endpoint are accepted; a normal trailing slash is allowed | runtime settings attack and positive normalization tests | unit | PASS |
| Each fixture has exactly one successful two-message semantic-judge call with `stop`, no tools/errors/HTTP status, one provider attempt, valid usage, and canonical model | model-call protocol attack tests | unit | PASS |
| Report, budget identity, calls, and canonical price window form one ordered timezone-aware lifecycle | call-time and budget-identity attack tests | unit | PASS |
| The output remains within the fixed private root and is owner-only | valid CLI report test | integration | PASS |

## Coverage and boundary

The focused coverage run reported 85% combined coverage:

```text
evals/calibration_attestation.py            87%
evals/run_semantic_judge_calibration.py     81%
TOTAL                                       85%
```

No network, model, secret, budget-ledger, or private-artifact access was used.
The repository-wide verification gate was intentionally left to the parent
integration pass because parallel Phase 2 fixes were changing holdout and
budget modules during this cycle.
