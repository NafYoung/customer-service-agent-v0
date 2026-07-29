# Paid timeline and formal runtime chain TDD

## Source and user journey

This cycle closes two fresh Phase 2 adversarial-review findings:

- paid evidence could claim that its budget run began before the canonical
  price snapshot became valid;
- formal completed and failed receipt chains bound only a Git commit, so a
  changed source tree, package closure, or runtime profile could still be
  presented as the environment approved by the 7×4 regression gate.

The required journey is one continuous chain: canonical price and runtime are
verified before the first provider call, frozen into the sealed declaration
and start receipt, recorded in completed or failed evidence, and recomputed by
the terminal verifier.

## RED

Checkpoint: `3ab9da5 test: reproduce paid timeline and formal source drift`

The RED tests demonstrated that:

- diagnostic, dev-repeat, and formal-failure evidence accepted a budget
  identity whose start preceded `price.captured_at`;
- a failed formal bundle accepted an arbitrary source-tree SHA-256;
- a completed formal bundle accepted a changed package version.

The follow-up implementation review also reproduced two adjacent gaps before
the final gate: a valid formal context with drifted runtime settings could
reach the model loop, and failed evidence copied the runtime hash without
independently recomputing it.

## GREEN

The implementation now:

- requires every persistent paid budget identity to start within the canonical
  price window;
- binds successful diagnostic, dev-repeat, and formal evidence to the complete
  ordered budget/eval timeline;
- permits a diagnostic to finish after price expiry only when its evidence
  contains the explicit `MODEL_PRICE_EXPIRED` failure and matching uncertain
  attempt accounting;
- preserves both active and completed formal failed-attempt evidence when a
  run starts inside the price window but failure cleanup ends after expiry;
- carries source-tree, full source-snapshot, and full runtime identity hashes
  through the regression gate, sealed manifest, declaration, immutable start
  receipt, formal evidence, and terminal verifier;
- revalidates the canonical settings and recomputes the complete runtime
  identity before the first model call and again before completed evidence is
  written;
- stores strict source, harness, and model snapshots in failed-attempt evidence
  and recomputes its runtime identity instead of trusting a copied hash.

## Verification

| Guarantee | Evidence | Result |
|---|---|---|
| Pre-price budget identities fail closed | diagnostic, dev-repeat, calibration, and formal-failure attack tests | PASS |
| Explicit price-expiry diagnostic remains publicly verifiable | full diagnostic cross-window payload test | PASS |
| Active and completed cross-window formal failures remain verifiable | parameterized failed-attempt lifecycle test | 2/2 PASS |
| Source-tree and package/runtime substitution fail in completed and failed chains | completed/failed receipt-chain attack tests | PASS |
| Drifted formal settings or source identity produce zero model calls | issued-context source/runtime attack tests | PASS |
| Failed evidence independently recomputes source and runtime identity | strict failed-bundle and receipt-chain tests | PASS |

All checks in this cycle are offline. They do not read `.env`, the real budget
ledger, private holdout content, or private Eval artifacts, and they do not
make model or network calls.
