# Formal runtime capability binding TDD evidence

## Source and user journey

No separate plan file was used. The journey was derived from the Phase 2
same-commit audit:

> As the formal holdout owner, I need the exclusive start receipt to authorize
> exactly one already-constructed canonical runtime object graph, so a
> programmatic caller cannot replace the actor, judge, harness, budget guard,
> budget report provider, or model configuration before the first paid call.

## RED

Before production changes:

```text
.venv/bin/python -m pytest tests/test_dev_repeat_paid_gate.py \
  -k 'formal_execution_capability' -q
```

Result: `8 failed`. Every test reached the intended missing boundary and
failed because `_create_validated_formal_execution_capability` did not exist.
The cases covered actor replacement, judge replacement, prompt/policy/tool
replacement with unchanged caller-supplied fingerprints, budget-guard
replacement, report-provider replacement, and forged capability replacement.

### RED — live transport / budget graph seals (Phase 2 re-audit P0/P1)

Before sealing `model._client` identity and deriving transport mode from the
live httpx client (and before sealing budget ledger / price snapshot ids):

```text
.venv/bin/python -m pytest tests/test_dev_repeat_paid_gate.py \
  -k 'httpx_client_swap or transport_mode_lie or budget_graph_rebinding' -q
```

Expected on baseline `a10facb`: the post-issue `_client` swap and
`_transport_mode = "default"` lie cases fail open (binding still accepts the
swapped / lied channel), and budget `_ledger` / `_price_snapshot` rebinding is
not rejected at consume.

## GREEN

After implementation:

```text
.venv/bin/python -m pytest tests/test_dev_repeat_paid_gate.py \
  -k 'formal_execution_capability or formal_model_public_runtime_config or \
  issued_formal_context_binds_output or programmatic_formal_run' -q
```

Result: `17 passed`, with one unrelated Starlette deprecation warning.

```text
.venv/bin/python -m pytest \
  tests/test_readonly_eval_cli.py::test_formal_runtime_failure_keeps_partial_evidence_and_terminal \
  -q
```

Result: `3 passed`, with the same unrelated warning.

```text
.venv/bin/python -m ruff check app/agent/openai_compatible.py \
  app/agent/factory.py evals/run_readonly_agent_evals.py \
  tests/test_dev_repeat_paid_gate.py tests/test_readonly_eval_cli.py
```

Result: `All checks passed!`

```text
.venv/bin/python -m mypy app/agent/openai_compatible.py \
  app/agent/factory.py evals/run_readonly_agent_evals.py
```

Result: `Success: no issues found in 3 source files`.

### GREEN — live transport / budget graph seals (Phase 2 re-audit P0/P1)

After sealing:

- `OpenAICompatibleChatClient.public_runtime_config` / formal binding derive
  `transport_mode` from the live `httpx.Client._transport` type (not the
  writable `_transport_mode` attribute alone).
- Issue records `sealed_httpx_client_id`, `sealed_budget_ledger_id`, and
  `sealed_budget_price_snapshot_id`; consume re-checks object identity.

```text
.venv/bin/python -m pytest tests/test_dev_repeat_paid_gate.py \
  -k 'httpx_client_swap or transport_mode_lie or budget_graph_rebinding or \
  formal_execution_capability or formal_model_public_runtime_config' -q
```

Result: `18 passed` (includes the four new adversarial cases: client swap,
transport-mode lie, and two budget-graph rebinding variants).

```text
.venv/bin/python -m ruff check app/agent/openai_compatible.py \
  evals/run_readonly_agent_evals.py evals/readonly_reporting.py \
  tests/test_dev_repeat_paid_gate.py tests/test_readonly_eval_cli.py
.venv/bin/python -m mypy app/agent/openai_compatible.py \
  evals/run_readonly_agent_evals.py evals/readonly_reporting.py
```

Result: ruff and mypy clean on the sealed-transport change surface.

## Test specification

| Guarantee | Test coverage | Type | Result |
|---|---|---|---|
| Actor or semantic-judge replacement is rejected before any model call or budget attempt | `test_formal_execution_capability_rejects_actor_model_replacement_zero_calls`; judge equivalent | integration | PASS |
| Prompt, policy, or tool entity replacement is rejected even when the old fingerprint mapping is retained | `test_formal_execution_capability_rejects_harness_entity_replacement_zero_calls` | integration | PASS |
| Consumption independently re-freezes the repository-owned harness | `test_formal_execution_capability_refreezes_harness_before_model_call` | integration | PASS |
| Budget guard, exact bound report provider, capability, and public model configuration cannot be substituted | `test_formal_execution_capability_rejects_runtime_object_replacement_zero_calls` | integration | PASS |
| Post-issue live `httpx` client / MockTransport channel swap is rejected before any model call or budget attempt | `test_formal_execution_capability_rejects_post_issue_httpx_client_swap_zero_calls` | integration | PASS |
| Custom transport plus `_transport_mode = "default"` lie fails closed at issue (live transport derivation) | `test_formal_execution_capability_rejects_transport_mode_lie_zero_calls` | integration | PASS |
| Post-issue budget `_ledger` / `_price_snapshot` rebinding is rejected before any model call or budget attempt | `test_formal_execution_capability_rejects_budget_graph_rebinding_zero_calls` | integration | PASS |
| Instance- or class-level execution-method overrides are rejected before any model call or budget attempt; budget-guard execution methods are bound the same way | instance/class method override tests | integration | PASS |
| A rejected attempt consumes the capability, preventing reuse | actor replacement test, second exact-object attempt | integration | PASS |
| Public runtime configuration equals the factory contract and contains no credential field or key value | `test_formal_model_public_runtime_config_is_canonical_and_credential_free` | unit | PASS |
| Existing output-root, symlink, source, runtime-drift, and formal failure-evidence behavior remains closed before calls | issued-context binding and formal runtime failure tests | integration | PASS |

## Scope and known gaps

- All checks were offline. No provider request, `.env`, real budget ledger, or
  holdout case set was accessed.
- The full repository suite was not used as this task's GREEN gate because a
  parallel budget/schema change was still updating shared fixtures. Its
  repository-wide verification belongs to the integration owner.
- No checkpoint commit was created because this phase explicitly required
  leaving the shared worktree uncommitted; this document preserves the RED and
  GREEN evidence instead.
- Live transport sealing treats `httpx.HTTPTransport` as the default channel.
  An explicit `transport=httpx.HTTPTransport(...)` injection is therefore
  treated as default-equivalent; MockTransport / DummyTransport and post-issue
  `_client` identity swaps are fail-closed.
- Same-OS-user rewrite of private lock files remains outside the claimed TCB.
