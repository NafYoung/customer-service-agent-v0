# Paid attempt hash and terminal-stage binding TDD

## Journey and boundary

A reviewer of paid Eval evidence must be able to prove that every provider
attempt belongs to the model call that reports its outcome. A synchronized
rewrite of public call labels must not make another logical call's ledger
outcomes satisfy the validator.

This cycle is limited to anonymous SHA-256 call identities, deterministic
terminal-stage labels, and existing public budget buckets. It does not expose
provider request IDs, change the private SQLite schema, read `.env`, access a
real ledger, call a model, use the network, or run a holdout.

## RED

The first test run failed during collection because the shared binding helper
did not exist:

```text
ModuleNotFoundError: No module named 'evals.paid_attempt_binding'
```

After adding only the structural scaffold, the runtime RED run produced 17
failures. The failing journeys showed that the previous validators accepted:

- two failed calls whose public error labels were swapped across logical calls;
- missing or duplicate logical-call hashes;
- a successful call without exactly one settled attempt;
- a provider-side price-expiry outcome completed before `valid_until`.

They also rejected legitimate evidence where the public adapter and private
ledger intentionally use different error namespaces:

- `MODEL_BUDGET_USAGE_ERROR` maps to `MISSING_PROVIDER_USAGE` or
  `INVALID_PROVIDER_USAGE`;
- `MODEL_BUDGET_ERROR` maps to `COST_EXCEEDS_RESERVATION` or
  `MODEL_BUDGET_ERROR`.

Retry evidence followed by a local reserve-time budget or price terminal error,
including a zero-provider-attempt reserve failure, was also red.

No checkpoint commit was created because this shared-worktree task explicitly
forbade commits.

## GREEN

The adapter now records whether a terminal error happened during
`provider_attempt` or `reserve_attempt`. The public evidence keeps the
anonymous `logical_call_sha256` and never exports a provider request ID.

The shared validator enforces:

- every paid call with `provider_attempts > 0` has a valid, unique call hash;
- bucket counts for that hash equal the call's provider-attempt count;
- success has exactly one settled attempt and all prior attempts uncertain;
- failure has only uncertain provider-attempt buckets;
- provider errors match an allowed same-hash ledger outcome;
- provider-side `MODEL_PRICE_EXPIRED` has a same-hash completion at or after
  the canonical price expiry;
- reserve-time budget and price errors can terminate after prior retries or
  before any provider attempt.

The same helper runs before public artifact construction and again in the
strict public schema validator. Diagnostic, formal-failure, dev-repeat, and
formal-success paths therefore share one rule set.

Focused offline verification:

```text
8 focused test modules passed
tests/test_dev_repeat_paid_gate.py: 118 passed
Ruff: All checks passed
Mypy: Success, 53 source files checked
Contracts: fresh
```

The full repository gate is intentionally left to the integration owner after
all concurrent worktree changes are assembled.

## Residual trust boundary

The public hash proves internal consistency with the exported anonymous ledger
buckets; it does not independently attest to provider truth. The deterministic
runtime is responsible for assigning the terminal stage and logical call ID,
while the private persistent ledger remains the authority for attempt
settlement and outcome timestamps.
