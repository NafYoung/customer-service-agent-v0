# Eval harness v0

`run_reference_evals.py` verifies that the synthetic environment, deterministic rules, write controls, audit trail, and final-state graders agree with the case definitions.

It is deliberately called a **reference** eval. It does not claim to measure an LLM yet, because the current driver reads the structured `reference_plan` rather than interpreting the natural-language `conversation` field.

Run:

```bash
python evals/run_reference_evals.py
```

This reference runner remains separate from the real model-facing harness. That
separation prevents a successful deterministic backend test from being
misreported as Agent accuracy.

## Read-only Agent Eval

`run_readonly_agent_evals.py` sends only each case's natural-language
`user_message` and the six read-only tool contracts to the model. Expected
outcomes and `semantic_contract` remain outside the tested Agent context.
Deterministic code scores tools, permissions, writes, and database state. Once
the answer is frozen, an isolated tool-free JSON judge scores atomic language
claims; malformed, ungrounded, ambiguous, or missing verdicts fail closed.

Before a new paid holdout, calibrate the judge against the fixed 49-fixture
public, human-labelled corpus:

```bash
set -a
source .env
set +a
python evals/run_semantic_judge_calibration.py
```

Holdout eligibility requires 49/49 exact matches. A protocol error, corpus or
runtime drift, observed-model mismatch, missing usage, or unsettled budget
evidence fails closed. The schema-v2 report preserves every verdict and binds
the canonical corpus, contract set, calibration implementation, runner, and
model runtime.

An independent reviewer must then create a private schema-v1 review receipt
that references the exact report SHA-256, records a `GO` conclusion, and lists
the five canonical fixture IDs selected by the deterministic stratified sample.
The sealed holdout manifest binds both files and the exact private evidence
bundle from the canonical 7-case by 4-trial public regression. The formal
runner requires `--regression-bundle`, verifies its integrity, owner-only
permissions, 28/28 strict and security results, unchanged business state,
settled canonical budget, source commit, and frozen harness before constructing
the budget guard or model client. Those identities are copied into the sealed
manifest, exclusive start receipt, completed or failed evidence, and terminal
receipt chain.

The independence field is a procedural reviewer declaration, not a
cryptographic third-party identity proof. The exclusive local receipts and
SHA-256 links detect accidental replacement and chain mismatch; they are not
tamper-proof against an actor with the same operating-system user privileges.

After calibration passes, run the public regression set:

```bash
set -a
source .env
set +a
python evals/run_readonly_agent_evals.py \
  --purpose dev_repeat \
  --split dev \
  --case-dir evals/readonly_regression_cases \
  --case-set-name readonly-regression-v1 \
  --trials 4
```

The current DeepSeek baseline is `deepseek-v4-flash`, non-streaming, with
thinking explicitly disabled and temperature fixed at 0. The run checks
declared tools and structured results, atomic answer semantics, and whether any
business state changed. The judge is not a security authority and the result
does not certify production safety.

Every non-formal paid entry point is fail-closed against a repository-owned
case allowlist before budget or model construction. `diagnostic` is bound to
the canonical 10-case development set; `dev_repeat` is bound to the canonical
7-case regression set, its name, count, and content hash. Completed
`diagnostic` evidence recomputes canonical usage cost for every successful
call and requires every retry or error attempt to match an `uncertain` bucket;
the producer and public Schema apply the same check. Offline reference
evidence cannot claim a DeepSeek observation or a non-zero provider attempt.
Completed `dev_repeat` evidence must contain 28 settled records whose provider
attempts, canonical usage costs, and attempt buckets reconcile exactly with
the persistent run budget and remain within the CNY 18 execution limit.

The first live `deepseek-v4-flash` run on 2026-07-29 passed 7/10 cases under
the strict tool-call budgets. All three failures were efficiency failures; all
10 cases added no approval, confirmation, execution or support-ticket records.
See
`docs/testing/deepseek-readonly-agent-live-eval.md`.

A Prompt-only B run on the unchanged harness then passed 10/10 and reduced
total tool calls from 25 to 12. A later declared four-trial development run
passed 40/40 with `pass^4=1.00`, 40/40 security checks, and no business-state
changes. It used 94 provider attempts and the persistent ledger calculated
CNY 0.04357292 from returned usage, with no uncertain reservations.

The independently sealed holdout v1 was then run exactly once. It scored 46/80
with `pass^4=0.35`, cost CNY 0.08381112, and was retired. Post-run review found
no true security-critical violation, but identified missing-information,
efficiency, and semantic-scoring weaknesses. Its score remains unchanged and
the v1 case set must never be rerun. Confirmed failures now form the public
regression set; a new v2 requires the calibration and one-run protocol in
`docs/testing/readonly-holdout-v2-protocol.md`.

Every paid HTTP attempt, including retries, must first reserve its conservative
upper-bound cost in the private SQLite budget ledger. The hard limit is CNY 20
and the automatic execution limit is CNY 18. Missing or inconsistent billing
evidence retains the full reservation. The public demo will not deploy the
project API key.

Each run writes a private integrity-checked bundle under
`artifacts/private/eval-runs/`. Diagnostic and development bundles can be
verified directly:

```bash
python evals/verify_eval_bundle.py artifacts/private/eval-runs/<run-id>
```

Formal v2 completed and failed-attempt bundles are not valid as standalone
artifacts. The public verifier requires the sealed manifest plus the immutable
start and terminal receipts and the bound `--regression-bundle`. It rejects a
renamed, replaced, out-of-root, stale-commit, stale-harness, non-28/28, unsafe,
state-changing, unsettled, or non-owner-only regression artifact. It also
rejects symlinks or any formal directory/file whose mode is not exactly
`0700`/`0600`. The formal runner's console output withholds both case details
and private filesystem paths.

The programmatic `run_eval_suite` entry point cannot start a formal run from
ordinary arguments. The CLI creates a one-use in-process formal context only
after the `O_EXCL` start receipt succeeds. Before the first model call, the
suite consumes that context and rechecks the fixed receipt path, owner-only
permissions, exact bytes hash, run/case/source/harness/calibration/regression
bindings, purpose, and split. This is a local fail-closed coordination control,
not protection against an actor with the same operating-system user privileges.

See `docs/testing/eval-evidence-budget-guard.tdd.md` for the artifact contract,
budget semantics, official pricing sources, and evidence boundaries. See
`docs/testing/semantic-judge-v1.tdd.md` for the semantic gate and its remaining
evidence boundary.

## Shadow 离线回放（零付费、零写入）

`run_shadow_offline.py` 把案例的自然语言消息逐条走一遍公开演示同款
scripted 路径（无模型、无网络、零成本），产出「自动覆盖 / 风险 / 写入 /
成本」报告。风险口径 = 生成了待确认预览但与该案例冻结期望中的
`forbidden_tools`（prepare_*）冲突——因此 scripted 演示路径的覆盖与风险
同时统计，**它不是模型评测，也不能被报告为模型评测**：

```bash
python evals/run_shadow_offline.py [--case-dir evals/readonly_regression_cases]
```

当前基线（公开回归 7 条）：覆盖 3/7、风险 3/7、业务写入 0、成本 0。
基线变更必须显式重审（见 `tests/test_shadow_offline.py`）。

引用校验（P1-7）：独立目录 `evals/readonly_citation_cases/`（3 条友好用例）
通过同一 shadow 通道校验 scripted 回复必须携带 `POL-* v0.1` 政策引用；
该目录不属于冻结的 diagnostic/dev_repeat 案例集，不会改变任何付费评测绑定。

## 指标口径（对行业语言的翻译）

本项目的机器可复核指标是 `pass^1` / `pass^4` 与安全硬门；与行业主叙事
「独立解决率 / 转人工率」的映射如下（口径不同，不直接混用）：

| 行业口径 | RIVET 对应 | 当前状态 |
|---|---|---|
| resolution（解决） | 单任务单次 trial 全部原子命题通过 + 安全硬门通过 + 业务写入 0（即 `pass^1` 的构成） | 已机器化统计 |
| deflection（拦截分流） | 无需人工介入完成的任务占比 | 待宿主转人工闭环接线后启用正式口径（当前未接线） |
| handoff（转人工） | 资格不通过需人工兜底 / 宿主拒绝 / 预算耗尽的占比 | 部分可数（如 `cases/06_defect_handoff.json`、demo 拒绝路径）；接线后统一统计 |
| 成本口径 | attempt 级预算预扣；已结算开发集 40 任务 ¥0.04357292（约 ¥0.0011/任务） | 已结算；per-resolution 仅为成本口径对照，非商业定价 |

行业头部自报独立解决率 80–91% 为厂商口径；本项目的题面难度、系统范围与
安全门槛均不同，不宣称与行业数字直接可比。相关决策见
`docs/14_architecture_decisions.md`（ADR-4、ADR-5、ADR-6）。
