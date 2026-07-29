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

## Read-only Agent Eval v1

`run_readonly_agent_evals.py` sends only each case's natural-language
`user_message` and the six read-only tool contracts to the model. Expected
outcomes remain private to the deterministic grader.

Load the local environment without printing it, then run:

```bash
set -a
source .env
set +a
python evals/run_readonly_agent_evals.py \
  --purpose dev_repeat \
  --split dev \
  --case-set-name readonly-dev-v2 \
  --trials 4
```

The current DeepSeek baseline is `deepseek-v4-flash`, non-streaming, with
thinking explicitly disabled. The run checks declared required and forbidden
tools, selected structured results, exact obvious-write-overclaim prohibitions,
and whether approvals, confirmations, executions, or support tickets were
added. It does not exhaustively inspect the final answer, certify production
safety, or evaluate the future transaction flow.

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
budget semantics, official pricing sources, and evidence boundaries.
