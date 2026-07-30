# Calibration live-ledger attestation TDD evidence

## Source and user journey

No separate plan file was used. The audit journey is:

> As the formal holdout owner, I need a 49/49 semantic calibration report to
> be accepted only when every reported paid model call can be independently
> recovered from the fixed private budget ledger, so a synthetic report and
> caller-supplied budget dictionary cannot self-attest.

## RED

Before production changes:

```text
.venv/bin/python -m pytest \
  tests/test_calibration_attestation.py::test_calibration_attestation_rejects_synthetic_report_without_live_ledger \
  -q
```

Result: `1 failed` with `Failed: DID NOT RAISE
CalibrationAttestationError`. A complete synthetic 49/49 report with no
provider call and no persistent ledger was accepted, and the missing ledger
path remained absent.

## GREEN

The validator now reads the fixed private SQLite ledger through
`SQLiteBudgetLedger.read_existing_evidence_snapshot(...)`. The reader requires
an existing owner-only regular file and owner-private real parent directory,
opens SQLite with `mode=ro&immutable=1`, enables query-only mode, and validates
the exact schema and budget metadata before reading. A missing file is never
created; a public, malformed, symlinked, or mismatched ledger is rejected.

The adapter assigns one private logical-call ID per logical model call and
exports only its SHA-256. Calibration model-call evidence carries that digest
through success and error paths. Attestation requires 49 unique call digests
and 49 matching single-attempt settled ledger records, then checks run identity,
run totals, usage-derived cost, settlement mode, and the call/run/price
timeline. It does not accept a caller-supplied budget dictionary as the source
of truth.

The validator also freezes a clean current Git commit and repository-owned
harness itself. Optional caller snapshots are comparison inputs only; a caller
cannot self-certify a forged harness or source commit.

In addition to tamper-matrix unit tests, the GREEN path now creates a real
temporary owner-private ledger with 49 settled calls, builds a matching report,
and validates it through the production read-only ledger path. Companion tests
reject missing and public-permission ledgers.

Focused offline checks:

```text
test_calibration_attestation_accepts_matching_temporary_ledger: PASS
test_calibration_attestation_rejects_untrusted_ledger_permissions: PASS
test_calibration_attestation_rejects_synthetic_report_without_live_ledger: PASS
```

The assembled offline repository gate also passed 586 tests with 83.37% branch
coverage, Ruff, 53-file Mypy, fresh contracts, and Reference Eval 8/8.

## Required guarantees

| Guarantee | Planned evidence |
|---|---|
| A missing, public, malformed, symlinked, or mismatched trusted ledger fails without creating a file | temporary-ledger, permission, schema, and path tests |
| The adapter exposes only an anonymous logical-call SHA-256, never the raw logical call id | adapter and observed-evidence tests |
| All 49 report calls have unique hashes and match 49 settled one-attempt ledger records | real temporary-ledger integration test and tamper matrix |
| Run identity, run totals, usage-derived costs, settlement modes, and time windows match the live ledger | calibration attestation tamper matrix |
| Callers cannot inject a raw budget dictionary, forged harness, or forged source commit | validator source-freeze and programmatic attack tests |

## Boundaries

- Tests use temporary ledgers or a monkeypatched trusted loader only.
- No real provider call, `.env`, fixed private ledger, or holdout corpus is
  accessed.
- No SQLite schema migration is in scope for unrelated tables; calibration and
  paid ledgers may store nullable `response_content_sha256` on settled attempts.
- Calibration attestation binds each model call's `response_content_sha256` to
  the canonical JSON digest of the report verdict **and** to the digest sealed
  into the trusted ledger attempt. Rewriting both `results[].verdict` and the
  report-side digests still fails when the ledger retains the original digest.
