# Non-formal paid Eval gates — TDD evidence

## Scope

This change closes two paid-evaluation gaps without calling a model or reading
private artifacts:

1. bind every non-formal paid runner entry to a repository-owned public case
   allowlist before budget or model construction;
2. make completed `dev_repeat` evidence prove that its 7 cases × 4 trials,
   provider attempts, usage costs, canonical price, reservations, attempt
   buckets, and settled budget agree.

The same schema work also rejects coerced usage/attempt types and validates any
persisted attempt-bucket evidence against its run and cumulative totals.

The 2026-07-29 follow-up also binds paid evidence to each individual completed
trial, instead of accepting a correct flattened call total, and moves the
non-formal case contract into the programmatic runner before any model call.

## RED

| Commit | Reproducer | Observed gap |
|---|---|---|
| `95c2396` | `tests/test_dev_repeat_paid_gate.py` | external case copies, wrong names/content, unsettled budgets, forged prices, and contradictory buckets were accepted |
| `c10214a` | strict model-call evidence cases | booleans and numeric strings were coerced into provider attempts or token counts |
| `82be994` | completed bucket/call binding cases | completed paid evidence could omit buckets or coordinate a false bucket cost |
| `47f4f2f` | duplicate-key multiset attack | duplicate run bucket keys could be overwritten during subset comparison |
| `3a012c2` | unsafe run-id path cases | absolute and traversal-like run IDs reached an output path existence check |
| `7d1e5e7` | per-trial call relocation and phase attacks | calls could be moved between trials; missing phases, broken phase sequences, forged judge tools, and out-of-window calls were accepted |
| `b8f0e44` | programmatic runner and diagnostic identity attacks | direct callers could run arbitrary or unknown scopes before rejection; diagnostic bundles could claim arbitrary case identities |

The first targeted run produced 17 intended failures; each later attack was
also executed and failed before its corresponding production change.

## GREEN guarantees

| Guarantee | Evidence |
|---|---|
| `diagnostic` accepts only `evals/readonly_cases`, `readonly-dev-v1`, 10 cases, and canonical hash `86e7…af7d` | CLI pre-budget attack tests |
| `dev_repeat` accepts only `evals/readonly_regression_cases`, `readonly-regression-v1`, 7 cases, and canonical hash `6340…2edb` | CLI pre-budget and manifest tests |
| unsafe run IDs fail parsing before any output path check | absolute/traversal run-id tests |
| completed `dev_repeat` is exactly 28 records and has completed, settled, canonical, ≤¥18 budget evidence | manifest attack matrix |
| provider attempts and per-call canonical usage cost match current-run attempt buckets as a multiset | producer and public payload validator tests |
| offline evidence cannot claim attempt buckets; paid buckets recompute totals and run buckets are a multiset subset of cumulative buckets | `BudgetSummary` attack matrix |
| booleans and numeric strings cannot masquerade as paid usage or attempt integers | strict `ModelCallRecord` tests |
| every completed paid `dev_repeat` or formal trial has one or more consecutive Agent calls and exactly one isolated semantic-judge call | producer and public bundle attack tests; GREEN commit `ffea278` |
| every paid call is successful, single-attempt, priced, model-bound, and timestamped inside its own trial; successful calls cannot carry an error status | per-trial producer and Schema matrices |
| Agent calls expose the current read-only contract count; judge calls expose no tool contracts or tool requests | phase-integrity attack tests |
| manifest `observed_models` is recomputed from a non-empty per-record call set | empty-call and forged-manifest public payload attack |
| direct `run_eval_suite()` rejects unknown purposes and non-canonical diagnostic/dev-repeat payloads before timing, looping, or calling a model | zero-model-call preflight tests; GREEN commit `a5a0d68` |
| diagnostic builder and public Schema accept only the canonical 10-case, one-trial identity | builder and forged-bundle identity tests |

## Validation

- Targeted paid-gate test: `tests/test_dev_repeat_paid_gate.py`
- Adjacent protocol tests: reporting, CLI, holdout lock, failed-attempt, and
  calibration-attestation suites
- Static checks: Ruff and mypy on all touched source files
- Full offline gate: `make verify PYTHON=.venv/bin/python`

Final result: Ruff passed, mypy passed for 51 source files, contracts were
fresh, 373 tests passed, branch coverage was 82.54%, the runtime dependency
audit found no known vulnerabilities, and Reference Eval passed 8/8.

No DeepSeek request, network call, secret read, private artifact read, or real
budget-ledger read is part of this TDD cycle.
