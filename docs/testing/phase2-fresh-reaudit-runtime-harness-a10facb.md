# Phase 2 Fresh Re-Audit — Runtime / Capability / Source / Harness

Independent adversarial review. Reviewer did **not** implement the code under
review. Report-only; no production changes; no DeepSeek calls; no secrets.

## Reviewed SHA

- Requested: `a10facb87019249b4fc48adc20d8b254a1131bc0`
- Verified: `git rev-parse HEAD` → `a10facb87019249b4fc48adc20d8b254a1131bc0`
- Working tree at review start: clean (`git status --porcelain` empty)
- HEAD subject: `fix: refresh DeepSeek price snapshot and bind budget clock`

## Scope

Track **only**: runtime / capability / source / harness identity for formal
`holdout_formal` execution.

In scope:

1. Formal execution capability binding (Settings, model client, judge, budget
   guard, frozen harness) — especially post-issue replacement.
2. Source-tree / harness fingerprints; symlink and private-output path escape.
3. Eval profile freeze (`30` / `1024` / `2` / `4` / `12`) and credential-free
   public runtime config.
4. Holdout lock, formal failure evidence, private chain; one-shot lock bypass
   via rename of run id / output dir / lock files.
5. Read-only agent tool whitelist / preparation-agent boundary only insofar as
   they affect formal harness identity.

Primary surfaces: `evals/run_readonly_agent_evals.py`,
`evals/readonly_eval.py`, `evals/holdout_lock.py`,
`evals/formal_failure_evidence.py`, `evals/readonly_reporting.py`,
`evals/private_paths.py`, `evals/calibration_attestation.py`,
`app/agent/{readonly,factory,openai_compatible,deepseek_budget,preparation}.py`,
and adversarial tests (`test_dev_repeat_paid_gate.py`,
`test_holdout_run_lock.py`, `test_formal_failure_evidence.py`) plus
`docs/testing/formal-*.tdd.md`.

Out of scope for this track: case-set content quality, LLM judge calibration
semantics, schema-only docs, and same-OS-user arbitrary process/memory rewrite
as a claimed hard boundary (project TDD already excludes full same-user FS/RCE
TCB).

## Method

1. Confirm SHA + clean tree; stop rule satisfied.
2. Read capability issue/consume path, lock/receipt chain, harness freeze,
   private path helpers, agent allowlists.
3. Map attack goals to concrete substitution points after
   `_create_validated_formal_execution_capability` and before first paid call.
4. Offline predicate probes (no provider I/O): prove which binding checks still
   pass after `_client` / `transport_mode` tampering.
5. Cross-check existing adversarial tests for coverage gaps vs claimed TDD
   (“complete formal execution object graph”).

## Findings

### P0 — Post-issue / pre-consume `httpx` transport channel is unbound

**Claim broken:** Formal capability binding is supposed to freeze the actor
channel so a programmatic caller cannot replace the model client before the
first paid call (`docs/testing/formal-runtime-capability-binding.tdd.md`).

**What is bound today** (`_require_bound_formal_runtime_objects` in
`evals/run_readonly_agent_evals.py`):

- exact types `OpenAICompatibleChatClient` / `DeepSeekBudgetGuard`
- no instance-dict shadows of `complete` / `complete_json` / `_complete`
- class method identity vs import-time `_FORMAL_MODEL_METHODS`
- judge `is` actor; report provider is bound `DeepSeekBudgetGuard.snapshot`
- `public_runtime_config(model) == deepseek_public_runtime_config(settings)`
- `uses_budget_guard(model, budget_guard)`

**What is not bound:** the live network object `model._client` (and thus its
transport). Consume/create paths never mention `_client`, transport identity,
or `httpx.Client`.

**Exploit shape (programmatic, same threat model as existing capability tests):**

1. Issue a valid `ValidatedFormalExecutionCapability` with a factory-built
   client (`transport_mode == "default"`).
2. Replace `model._client` with `httpx.Client(transport=MockTransport(...))`
   (or any interceptor).
3. `public_runtime_config` still reports `transport_mode: "default"` because
   that flag is a separate constructor attribute, not derived from the live
   client.
4. All `_require_bound_formal_runtime_objects` predicates still pass
   (offline probe on this SHA: `BINDING_PREDICATES_PASS_AFTER_CLIENT_SWAP`).
5. `run_eval_suite(..., purpose="holdout_formal", formal_execution_capability=...)`
   consumes the capability and drives the case loop over the **swapped**
   channel → formal identity evidence can be produced without the canonical
   DeepSeek transport.

**Variant at issue time:** construct with `transport=DummyTransport()`, then
set `model._transport_mode = "default"`. Offline probe:
`TRANSPORT_MODE_LIE_PASSSES_FACTORY_CONFIG` — factory config equality holds,
so issue binding can accept a non-default transport.

**Coverage gap:** `tests/test_dev_repeat_paid_gate.py` rejects actor object
replacement, instance/class method overrides, `_model` string swap, judge
swap, harness entity swap, and budget-guard object swap — but **never**
`_client` / transport identity. That is exactly the remaining actor-channel
hole.

**Impact:** Formal eval can run with a swapped provider identity while the
capability registry, harness fingerprints, and lock receipts still look
canonical. This is a direct hit on attack goal (1).

### P1 — Budget-guard ledger / price object graph not identity-bound after issue

Capability claim is a “complete formal execution object graph”, but binding
only checks guard **type**, **method identity**, and `uses_budget_guard`.

After issue, a caller can rebind private state such as:

- `budget_guard._ledger`
- `budget_guard._price_snapshot`

without shadowing `reserve_attempt` / `settle_attempt` / `snapshot` and without
failing `_require_bound_formal_runtime_objects` (consume/create do not mention
`_ledger` or `_price_snapshot`).

Downstream manifest/budget schema checks may still reject some crude forgeries,
but they are **post-call** controls. The pre-call capability gate does not seal
the ledger/price objects that actually authorize and meter attempts.

**Impact:** Weakens the “one issued capability ⇒ one frozen guard graph”
guarantee; enables attempt metering / pricing channel substitution in the same
programmatic window as the P0 transport swap.

### P2 — Source-tree fingerprint follows intermediate directory symlinks on open

Harness/prompt/policy freeze uses `read_file_snapshot` with `O_NOFOLLOW` on the
final path component (good).

`current_source_tree_sha256()` / `_source_fingerprints()` walk with
`Path.rglob` + `is_file()`, then hash via `read_file_snapshot`. Final-component
symlinks fail closed, but **intermediate** directory symlinks are followed by
`open`, so bytes from outside the repo can enter the source-tree digest under a
lexical in-repo path.

Ordinary untracked symlink dirties git and fails `require_clean_git_worktree`.
A **committed** directory symlink with a clean worktree can still pull external
bytes into `source_tree_sha256` while remaining “clean”. Self-consistent but
not “repo-bytes-only”.

**Impact:** Weaker than P0 for casual misuse; relevant if source identity is
marketed as pure in-tree content binding.

### P2 — Paid vs calibration endpoint path rules disagree

- `validate_paid_eval_settings`: allows path `""` or `/v1` (after rstrip).
- `require_canonical_calibration_runtime`: allows only `""` or `/`.

Formal paths call both. Default URL works; a `/v1` base URL that passes paid
validation fails calibration. Fail-closed inconsistency, not an open bypass.

### note — Holdout one-shot lock vs same-user FS rename

Lock file is fixed: `readonly-holdout-v2.start.json` under
`artifacts/private/holdout/formal-run-locks/`, created with `O_EXCL` (+
`O_NOFOLLOW` when available). Tests correctly reject:

- second acquire while start exists (including renamed declaration /
  different `run_id`) — `test_same_case_hash_cannot_get_a_second_lock_by_renaming`
- alternate / symlink output roots before model call —
  `test_issued_formal_context_binds_output_and_source_before_model_call`
- receipt schema extras and private-chain path/permission escapes —
  `test_holdout_run_lock.py` / formal private-chain TDD

**Residual (acknowledged by project TDD):** declaration
`formal_runs_completed` is not mutated on acquire; exclusivity is the presence
of the start (and later terminal) file. An actor who can rename/delete those
private lock files as the same OS user can reopen a “first” formal start.
This is outside the claimed TCB, not a capability-binding bypass.

Renaming CLI `--run-id` alone does **not** bypass the lock while start exists.
Redirecting `--output-root` away from the fixed private root fails closed.

### note — Profile freeze and credential-free config (mostly closed)

Canonical constants in `evals/calibration_attestation.py`:

- timeout `30.0`, max tokens `1024`, retries `2`, tool rounds `4`, tool calls
  `12`, model `deepseek-v4-flash`, temperature `0`, host `api.deepseek.com`.

Enforced on formal-eligible paths via `validate_paid_eval_settings` +
`require_canonical_calibration_runtime`.  
`deepseek_public_runtime_config` / `OpenAICompatibleChatClient.public_runtime_config`
are credential-free (covered by
`test_formal_model_public_runtime_config_is_canonical_and_credential_free`).
Caveat: `transport_mode` is spoofable metadata (see P0), not a live-channel
seal.

### note — Read-only agent / preparation boundary

Formal suite builds `ReadOnlyAgent` only (`evals/readonly_eval.py`), with
dispatcher allowlist = `READ_ONLY_TOOL_NAMES` and contract set equality checks
in `ReadOnlyAgent.__init__`. Preparation tools (`prepare_*`) live in
`PreparationAgent` and are not part of the formal readonly harness fingerprint
path. No formal identity bypass found via preparation agent for this track.

Harness freeze binds prompt, policies, tool contracts, scorer, agent loop,
price snapshot, evidence protocol sources; consume re-freezes and compares
entity + fingerprint digests (covered by entity-replacement and refreeze
tests). That side of attack goal (2) looks closed for in-process entity swap
**when fingerprints are left stale**.

### note — Failure evidence / private chain

Failed-attempt bundles enforce owner-only modes, no symlinks, schema cross
links, and receipt-chain regression bindings. Quarantine of unverified
completed bundle dirs exists. Residual same-user FS rewrite applies equally
here. No fresh P0 in failure-evidence schema alone for this track.

## Verdict Gate

**NO-GO**

Blocking reason: **P0** — formal execution capability does not bind the live
model transport/`httpx.Client`, so formal eval can execute with a swapped
provider channel after issue (and transport_mode can be lied into matching the
factory seal). This defeats the stated actor-binding guarantee for
`holdout_formal`.

## Residual risks / required fixes

Minimum fixes before GO on this track:

1. **Seal live transport identity in issue + consume** (fail closed, zero
   calls):
   - require `transport_mode == "default"` and `model._client` is the
     instance created at bind time (store `id(client)` / object identity on
     the capability), **or**
   - disallow any non-default transport for formal issue and re-derive
     transport mode from the live client (not a spoofable attribute), **or**
   - replace httpx client with a sealed wrapper whose send path cannot be
     rebound without failing method/identity checks.
2. Add adversarial tests mirroring existing capability cases:
   - post-issue `_client` swap → reject, zero model calls, zero budget
     attempts;
   - construct-with-custom-transport + `_transport_mode = "default"` lie →
     reject at issue or consume.
3. **Bind budget-guard ledger and price snapshot identities** (or re-hash
   their canonical freeze) at consume time — closes P1 of the same class.
4. (P2) Source-tree walk: refuse directory symlink components while
   fingerprinting, or hash symlink metadata explicitly instead of following
   intermediates.
5. Do **not** treat same-user lock-file deletion as fixed by capability work;
   keep it documented as residual unless a stronger durable seal (e.g.
   append-only / remote attestation) is intentionally added.

Until (1)+(2) land and pass offline adversarial tests, this track must remain
**NO-GO** for formal runtime/harness identity.
