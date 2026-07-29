# Formal private evidence chain TDD

## Source and user journey

This cycle was derived from the Phase 2 adversarial review of the one-shot
holdout path.

As the formal holdout operator, I need the issued execution context, immutable
receipts, private paths, and public verifier to fail closed before any provider
call, so that an alternate output root or a structurally similar bundle cannot
escape the sealed evidence chain.

## RED

Checkpoints:

- `837382d test: reproduce formal private chain bypasses`
- `10452d3 test: reproduce formal output symlink escape`

The RED tests reproduced three failures:

- an otherwise valid one-use formal context could be consumed with a caller
  supplied output root;
- an issued context accepted the fixed lexical output path when that path was a
  symlink resolving outside the private root;
- start and terminal receipts accepted unknown fields, and chain verification
  accepted paths or directory ancestry that were not owner-only under the
  declared private root;
- the public verifier reached an assertion for a non-formal bundle supplied
  with formal-chain arguments instead of returning a controlled invalid result.

## GREEN

The implementation now:

- binds the one-use formal context to the fixed absolute
  `artifacts/private/eval-runs/` root, rejects aliases and symlink components,
  and checks resolved containment before the model loop;
- validates start schema v1 and terminal schema v2 with unknown fields
  forbidden, aware timestamps required, and mutually exclusive completed or
  failed evidence hashes;
- requires the manifest, receipts, and bundle to resolve below the explicit
  private root with `0700` directory ancestry, `0600` files, and no symlinks;
- makes the private evidence writer create or tighten its output root to
  `0700`;
- rejects non-formal or incomplete formal-chain verifier invocations with a
  normal invalid exit rather than an assertion.

## Test specification

| Guarantee | Test area | Type | Result |
|---|---|---|---|
| An issued context cannot redirect a formal run to another output root | `tests/test_dev_repeat_paid_gate.py` | adversarial integration | PASS |
| Start and terminal receipts reject extra fields and invalid chain bindings | `tests/test_holdout_run_lock.py` | protocol/adversarial | PASS |
| Root escape, symlink, broad file modes, and broad directory ancestry fail closed | `tests/test_holdout_run_lock.py` | filesystem security | PASS |
| A non-formal bundle plus chain arguments is rejected without an assertion | `tests/test_readonly_eval_cli.py` | CLI regression | PASS |
| Formal failure evidence and existing CLI paths remain compatible | focused four-suite run | regression | 170/170 PASS |

## Evidence boundary

The chain protects against accidental relocation, structurally forged receipts,
unsafe local permissions, and ordinary programmatic misuse. It does not claim
to resist an actor who can rewrite the repository, process memory, and private
files as the same operating-system user. No network, model call, `.env` read,
private artifact read, or budget-ledger read was used in this cycle.
