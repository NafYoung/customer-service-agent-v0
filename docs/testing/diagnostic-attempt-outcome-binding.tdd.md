# Diagnostic attempt outcome binding TDD

## Journey and boundary

A reviewer of public paid diagnostic evidence must be able to distinguish a
provider attempt that actually ended because the canonical price expired from
an ordinary transport or protocol failure whose synchronized case, trajectory,
and summary labels were later changed to `MODEL_PRICE_EXPIRED`.

This cycle is offline and limited to anonymous budget-attempt outcome evidence.
It does not expose provider request IDs, read secrets, call a model, inspect a
real ledger, or run holdout.

## RED

The regression test first built budget evidence from an ordinary transport
failure, synchronized the public case, trajectory, and summary labels to
`MODEL_PRICE_EXPIRED`, and moved the diagnostic completion past the price
window. The public validator accepted the relabelled payload:

```text
.venv/bin/python -m pytest -q \
  tests/test_diagnostic_paid_evidence.py::test_public_diagnostic_rejects_cross_window_transport_outcome_relabelled_as_price_expiry

FAILED tests/test_diagnostic_paid_evidence.py::test_public_diagnostic_rejects_cross_window_transport_outcome_relabelled_as_price_expiry
Failed: DID NOT RAISE ValueError
```

Two ledger-interface tests were also red before implementation:

```text
FAILED test_attempt_evidence_exposes_anonymous_logical_call_outcome_and_completion
KeyError: 'logical_call_sha256'

FAILED test_read_existing_evidence_snapshot_is_read_only_and_requires_existing_file
AttributeError: SQLiteBudgetLedger has no attribute read_existing_evidence_snapshot
```

No checkpoint commit was created because this implementation was explicitly
requested in the shared worktree without commits.

## GREEN

The ledger now exports only anonymous attempt identity and outcome facts:

- `logical_call_sha256`: SHA-256 of the private ledger `logical_call_id`;
- `error_code`: the ledger-origin terminal error for uncertain attempts;
- `completed_at`: the timezone-aware settlement/outcome timestamp.

The original logical call ID, attempt ID, and provider request ID are not
exported. Reserved buckets require no error or completion, uncertain buckets
require both, and settled buckets require a completion with no error.
Run-to-cumulative subset checks include all three new fields.

The diagnostic validator now binds every attempted model call to ledger
outcomes with the same anonymous logical-call hash. Bucket counts must equal
the call's provider-attempt count; success has exactly one settled outcome;
provider failure has only uncertain outcomes and an allowed same-hash terminal
outcome. A provider-attempt `MODEL_PRICE_EXPIRED` additionally requires a
same-hash ledger outcome completed at or after canonical price expiry.
Reserve-time terminal errors, including a zero-attempt price expiry, remain
legal. Formal failed-attempt and completed paid evidence use the same shared
binding helper.

`SQLiteBudgetLedger.read_existing_evidence_snapshot(...)` opens an existing
private regular SQLite file in immutable read-only mode, validates its exact
table columns and budget metadata, and exports the selected run without
creating a missing file.

Focused offline verification:

```text
186/186 focused tests passed
Success: no issues found in 52 mypy-checked source files
Ruff: All checks passed
git diff --check: clean
```

The focused test set covered budget ledger behavior, diagnostic evidence,
formal failed-attempt evidence, price-window behavior, the OpenAI-compatible
adapter, read-only reporting, calibration attestation, and the semantic
calibration CLI. No network, model call, `.env` read, real ledger read, private
artifact read, or holdout run was used.

`response_sha256` is intentionally deferred: making it a settled-attempt
invariant requires a coordinated adapter change and an explicitly controlled
SQLite schema migration.
