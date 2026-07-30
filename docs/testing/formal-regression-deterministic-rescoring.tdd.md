# Formal regression deterministic rescoring TDD

## Journey and boundary

Before a formal holdout can start, the fixed public `readonly-regression-v1`
bundle must be judged from its canonical case contract and raw evidence. A
bundle must not pass merely because its case `scores`, `score_checks`, status,
and aggregate summary all agree with one another.

This cycle is offline. It does not call a model, read `.env`, inspect a budget
ledger, read private Eval artifacts, or run holdout.

## RED

The attack replaced one trial's answer with a false execution claim, added a
successful `execute_prepared_action` trace, set `counted_action_records` to
one, and kept the reported business state unchanged. It then synchronized all
case scores, score checks, status, failures, trajectories, and aggregate
summary to claim a clean pass.

```text
.venv/bin/python -m pytest -q \
  tests/test_dev_repeat_paid_gate.py::test_formal_regression_gate_rejects_raw_write_evidence_hidden_by_forged_pass

FAILED test_formal_regression_gate_rejects_raw_write_evidence_hidden_by_forged_pass
Failed: DID NOT RAISE HoldoutLockError
```

No checkpoint commit was created because this shared-worktree implementation
was explicitly requested without commits.

## GREEN

`rescore_readonly_case_evidence(...)` is now the single deterministic scoring
function used both during execution and during evidence verification. Each
case record persists the raw `semantic_verdict`; the public schema reloads the
canonical `readonly-regression-v1` case and recomputes:

- canonical input hash;
- required and forbidden tools;
- tool assertions and maximum calls;
- read-only-only tool use;
- required/forbidden answer phrases;
- grounded semantic verdict and contradiction result;
- business-state change and counted action records.

The verifier compares the recomputed scores, score checks, checks, failures,
and pass/fail status with the report. Aggregate summary consistency remains a
separate requirement. The formal regression gate independently performs the
same raw-evidence rescore and requires all 28 trials to pass.

Focused verification kept a valid 7×4 public bundle at 28/28 and rejected the
forged successful-write payload. The assembled repository gate then passed:

```text
586 tests passed
branch coverage: 83.37%
Ruff: passed
Mypy: 53 source files passed
Contracts: fresh
Reference Eval: 8/8
```

No network, model call, `.env`, real ledger, private artifact, or holdout run
was used.
