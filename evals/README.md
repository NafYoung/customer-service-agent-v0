# Eval harness v0

`run_reference_evals.py` verifies that the synthetic environment, deterministic rules, write controls, audit trail, and final-state graders agree with the case definitions.

It is deliberately called a **reference** eval. It does not claim to measure an LLM yet, because the current driver reads the structured `reference_plan` rather than interpreting the natural-language `conversation` field.

Run:

```bash
python evals/run_reference_evals.py
```

This reference runner remains separate from the real model-facing harness. That
separation prevents a successful deterministic backend test from being
misreported as Agent accuracy.

## Read-only Agent Eval

`run_readonly_agent_evals.py` sends only each case's natural-language
`user_message` and the six read-only tool contracts to the model. Expected
outcomes and `semantic_contract` remain outside the tested Agent context.
Deterministic code scores tools, permissions, writes, and database state. Once
the answer is frozen, an isolated tool-free JSON judge scores atomic language
claims; malformed, ungrounded, ambiguous, or missing verdicts fail closed.

Before a new paid holdout, calibrate the judge against the 37 public,
human-labelled fixtures:

```bash
set -a
source .env
set +a
python evals/run_semantic_judge_calibration.py
```

All expected-failure fixtures must match exactly and the positive match rate
must be at least 95%. The same persistent CNY 20 budget guard covers both the
tested Agent and the judge.

After calibration passes, run the public regression set:

```bash
set -a
source .env
set +a
python evals/run_readonly_agent_evals.py \
  --purpose dev_repeat \
  --split dev \
  --case-dir evals/readonly_regression_cases \
  --case-set-name readonly-regression-v1 \
  --trials 4
```

The current DeepSeek baseline is `deepseek-v4-flash`, non-streaming, with
thinking explicitly disabled and temperature fixed at 0. The run checks
declared tools and structured results, atomic answer semantics, and whether any
business state changed. The judge is not a security authority and the result
does not certify production safety.

The first live `deepseek-v4-flash` run on 2026-07-29 passed 7/10 cases under
the strict tool-call budgets. All three failures were efficiency failures; all
10 cases added no approval, confirmation, execution or support-ticket records.
See
`docs/testing/deepseek-readonly-agent-live-eval.md`.

A Prompt-only B run on the unchanged harness then passed 10/10 and reduced
total tool calls from 25 to 12. A later declared four-trial development run
passed 40/40 with `pass^4=1.00`, 40/40 security checks, and no business-state
changes. It used 94 provider attempts and the persistent ledger calculated
CNY 0.04357292 from returned usage, with no uncertain reservations.

The independently sealed holdout v1 was then run exactly once. It scored 46/80
with `pass^4=0.35`, cost CNY 0.08381112, and was retired. Post-run review found
no true security-critical violation, but identified missing-information,
efficiency, and semantic-scoring weaknesses. Its score remains unchanged and
the v1 case set must never be rerun. Confirmed failures now form the public
regression set; a new v2 requires the calibration and one-run protocol in
`docs/testing/readonly-holdout-v2-protocol.md`.

Every paid HTTP attempt, including retries, must first reserve its conservative
upper-bound cost in the private SQLite budget ledger. The hard limit is CNY 20
and the automatic execution limit is CNY 18. Missing or inconsistent billing
evidence retains the full reservation. The public demo will not deploy the
project API key.

Each run writes a private integrity-checked bundle under `artifacts/eval-runs/`.
Verify one independently:

```bash
python evals/verify_eval_bundle.py artifacts/eval-runs/<run-id>
```

See `docs/testing/eval-evidence-budget-guard.tdd.md` for the artifact contract,
budget semantics, official pricing sources, and evidence boundaries. See
`docs/testing/semantic-judge-v1.tdd.md` for the semantic gate and its remaining
evidence boundary.
