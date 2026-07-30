# Formal failure live-ledger binding TDD

## Boundary

Any gate-usable `persistent_sqlite` formal failure package must re-read the
fixed private budget ledger. Self-reported run amounts / attempt buckets that
do not match the live ledger fail closed. Retired formal v1 completed packages
follow the same binding when validated.

## RED

Synthetic formal failure budgets with `enforcement_mode=persistent_sqlite` and
no matching private ledger were accepted by `validate_formal_failure_bundle`.

## GREEN

`FormalFailureEvidenceBundle` calls
`require_persistent_budget_matches_trusted_ledger` whenever a captured budget
is present. Adversarial coverage:

- missing ledger → reject
- matching temporary private ledger → accept
- tampered settled/committed cost → reject

Focused offline verification uses temporary ledgers only; no DeepSeek calls.
