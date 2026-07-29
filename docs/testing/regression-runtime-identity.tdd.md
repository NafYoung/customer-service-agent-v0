# Regression Runtime Identity TDD Evidence

## Source and journey

This TDD cycle was derived from the Phase 2 post-review finding that a
`dev_repeat` bundle could self-report its source, detailed harness, scorer, and
model runtime while retaining a previously accepted aggregate harness hash.

As the formal holdout operator, I need the 7×4 public regression gate to compare
the verified bundle with a freshly frozen clean local runtime, so that a
rewritten but internally consistent bundle cannot authorize the one-shot
holdout.

## RED

Checkpoint: `fe42995 test: reproduce regression runtime self-attestation`

Command:

```text
.venv/bin/python -m pytest -q tests/test_dev_repeat_paid_gate.py::test_formal_regression_gate_rejects_self_attested_runtime_identity
```

Result: 31 failed and 4 passed. The failures showed that rewritten source
runtime fields, scorer fields, detailed harness fields, provider/host,
generation settings, timeout/retry policy, and semantic-judge settings were
accepted when bundle integrity was regenerated. Four mutations were already
rejected by existing paid-evidence or strict-schema rules.

## GREEN

The validator now independently:

- requires a clean, stable Git source before and after freezing;
- recomputes the source tree and Python/platform/package snapshot;
- records the direct runtime dependencies and the transitive distributions
  that can affect FastAPI, Pydantic, HTTPX, SQLAlchemy, and Uvicorn behavior;
- freezes and compares every harness fingerprint and recomputes its aggregate;
- compares the scorer identity;
- requires the canonical official DeepSeek runtime and compares its complete
  model, generation, timeout/retry, and semantic-judge snapshot;
- requires the approved `30 / 1024 / 2 / 4 / 12` timeout, token, retry, tool
  round, and tool-call profile even if caller settings and bundle agree on a
  different profile;
- requires the source-tree hash before the snapshot, inside the snapshot, and
  after the snapshot to be identical;
- incorporates the full runtime identity into the regression gate hash.

The formal preflight passes its already frozen source tree, harness, and
settings into the validator. Completed and failed chain verification continue
to reopen the actual regression bundle and now repeat the independent runtime
validation.

The coordinated-runtime, dependency-closure, and mixed-snapshot follow-up used
RED checkpoint `309f559`. Its focused six-suite run passed 233 tests.

## Test specification

| Guarantee | Evidence | Result |
|---|---|---|
| Every mutable source, scorer, detailed harness, and runtime field is rejected when forged and bundle integrity is rewritten | `tests/test_dev_repeat_paid_gate.py::test_formal_regression_gate_rejects_self_attested_runtime_identity` | 35/35 PASS |
| A canonical 28/28, security-clean, state-unchanged regression bundle remains accepted | `tests/test_dev_repeat_paid_gate.py::test_formal_regression_gate_accepts_only_verified_28_of_28_bundle` | PASS |
| Existing missing-trial, security, state, source, and harness attacks remain rejected | `tests/test_dev_repeat_paid_gate.py::test_formal_regression_gate_rejects_noncanonical_public_bundle` | 5/5 PASS |
| The complete paid regression gate suite remains green | `.venv/bin/python -m pytest -q tests/test_dev_repeat_paid_gate.py` | 92/92 PASS |
| Declaration/receipt regression bindings remain green | `.venv/bin/python -m pytest -q tests/test_holdout_run_lock.py -k regression` | 4/4 PASS |
| Formal preflight ordering remains green | selected formal CLI tests in `tests/test_readonly_eval_cli.py` | 2/2 PASS |
| Touched code is lint- and type-clean | focused Ruff and mypy commands | PASS |

## Coverage and known gaps

This slice adds branch-heavy unit/integration coverage for all newly enforced
identity fields. The repository-wide coverage and `make verify` gate are
intentionally left to the parent integration pass after the adjacent receipt
and public-verifier RED work is completed; those unrelated tests were already
present but uncommitted in the shared worktree during this slice.
