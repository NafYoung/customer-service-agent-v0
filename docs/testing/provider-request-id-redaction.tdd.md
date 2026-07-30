# Provider request identity redaction TDD

## Boundary

Provider request IDs may remain in transient in-memory diagnostics, but they
must not enter an Eval case record, semantic calibration report, integrity
bundle, Git artifact, or public build. The anonymous
`logical_call_sha256` is the only provider-attempt correlation identifier
exported by these evidence paths. Provider `response_id` values are redacted
the same way as `provider_request_id`.

## RED

Two tests injected canary provider request IDs into a read-only Eval result and
a semantic calibration judge response. Both canaries were serialized:

```text
FAILED test_result_record_contains_trial_trajectory_without_eval_expectations
assert 'private-provider-request-id' is None

FAILED test_calibration_result_preserves_full_validated_verdict
assert 'private-calibration-request-id' is None
```

## GREEN

All model-call artifact serialization now goes through
`model_call_evidence_record(...)`, which preserves non-content call evidence
but writes `provider_request_id: null`. The generic evidence sanitizer applies
the same null-only rule to caller-built bundle dictionaries. The strict public
`ModelCallRecord` schema accepts only null for this field, so a handcrafted
public payload cannot reintroduce it.

Focused offline verification:

```text
4 focused privacy tests passed
Ruff: All checks passed
```

No network, model, `.env`, real ledger, private artifact, or holdout input was
used.
