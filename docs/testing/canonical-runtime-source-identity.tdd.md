# Canonical runtime and source identity TDD

## Source and journey

This cycle addresses three Phase 2 post-review findings:

- a caller could coordinate non-default `Settings` with an equally changed
  regression bundle and still receive a gate described as canonical;
- the source snapshot recorded only four top-level packages;
- the regression validator compared source hashes before and after collection
  but did not require the hash inside the collected snapshot to match them.

For a holdout operator, the approved runtime must mean fixed reviewed values,
not merely internal agreement, and one runtime identity must come from one
stable source snapshot.

## RED

Checkpoint: `309f559 test: reproduce coordinated runtime identity drift`

The targeted RED run reproduced 12 failures:

- five paid-Eval limit changes were accepted;
- five coordinated settings-and-bundle changes received a regression gate;
- an `A -> B -> A` source snapshot was accepted;
- the package snapshot omitted relevant runtime dependencies.

## GREEN

The implementation now:

- defines the holdout-eligible profile as temperature `0`, model
  `deepseek-v4-flash`, official HTTPS endpoint, timeout `30`, maximum output
  tokens `1024`, retries `2`, tool rounds `4`, and tool calls `12`;
- applies the fixed limits before paid Eval budget or model construction and
  applies the complete canonical profile to calibration, public regression,
  and formal holdout eligibility;
- records the direct runtime packages plus their relevant FastAPI, Pydantic,
  HTTPX, SQLAlchemy, and Uvicorn dependency closure;
- requires the source-tree SHA-256 before collection, inside the source
  snapshot, and after collection to be equal.

## Verification

| Guarantee | Evidence | Result |
|---|---|---|
| Coordinated changes to each of the five runtime limits fail | paid settings and regression-gate parameterized tests | 10/10 PASS |
| Mixed source snapshot fails even when the outer source returns to its original hash | regression mixed-snapshot test | PASS |
| Runtime package identity includes relevant direct and transitive distributions | reporting dependency-closure test | PASS |
| Calibration, reporting, formal chain, CLI, and regression suites remain compatible | focused six-suite run | 233/233 PASS |

No network, model call, `.env` read, private artifact read, or budget-ledger
read was used.
