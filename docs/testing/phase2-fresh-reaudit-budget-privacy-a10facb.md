# Phase 2 Fresh Re-audit — 预算 / 结果 / 隐私

独立对抗复审（未参与实现）。只读审查；未调用 DeepSeek；未读取 `.env`、真实私有账本或私有 Eval artifact 内容；未打印任何密钥。

## Reviewed SHA + dirty-tree note

| 项 | 值 |
|---|---|
| 要求 SHA | `a10facb87019249b4fc48adc20d8b254a1131bc0` |
| 实测 `git rev-parse HEAD` | `a10facb87019249b4fc48adc20d8b254a1131bc0`（一致） |
| HEAD 主题 | `fix: refresh DeepSeek price snapshot and bind budget clock` |
| 工作树 | **不干净**（相对 HEAD 有未提交改动） |

**脏文件清单（`git status --short`）：**

```
 M app/agent/openai_compatible.py
 M docs/testing/calibration-live-ledger-attestation.tdd.md
 M docs/testing/formal-runtime-capability-binding.tdd.md
 M docs/testing/semantic-calibration-protocol.tdd.md
 M evals/calibration_attestation.py
 M evals/evidence.py
 M evals/evidence_schema.py
 M evals/readonly_reporting.py
 M evals/run_readonly_agent_evals.py
 M evals/run_semantic_judge_calibration.py
 M evals/semantic_judge.py
 M tests/test_calibration_attestation.py
 M tests/test_dev_repeat_paid_gate.py
 M tests/test_readonly_eval_cli.py
 M tests/test_semantic_judge.py
?? docs/testing/phase2-fresh-reaudit-runtime-harness-a10facb.md
?? docs/testing/phase2-fresh-reaudit-semantic-evidence-a10facb.md
```

（本报告写入时另新增本文件。）

**审查约定：** 每条 finding 标明适用面为 **HEAD-only**、**dirty-only** 或 **both**。预算/隐私相关脏 diff 主要落在 attestation 响应摘要、review 重放、校准 CLI 失败落盘、formal capability 封印 `httpx`/ledger/price 对象身份、source-tree 拒绝目录符号链接跟随、以及付费 endpoint path 收紧（去掉 `/v1`）。

## Scope

本轨仅覆盖：

1. DeepSeek 预算账本：reserve / settle / uncertain、价格窗、时钟注入、双花、`run_id` 重放
2. Canonical pricing 身份绑定
3. Evidence schema 是否信任自报成本
4. 隐私泄漏（`.env`、provider 请求/响应关联 ID、token、PII、私有路径）进入 git 跟踪或公开产物
5. diagnostic / `dev_repeat` 付费门 allowlist 绕过
6. 语义修复残留：`response_content_sha256` 仅在报告 `model_calls`、不在 ledger —— 协同改写 verdict + digest 后能否仍过 attestation + live ledger matching

## Method

1. 锁定 HEAD SHA 与脏树文件面。
2. 静态阅读 `app/agent/deepseek_budget.py`、`evals/canonical_pricing.py`、`evals/calibration_attestation.py`（HEAD + dirty）、`evals/evidence{,_schema}.py`、`evals/nonformal_paid_contract.py`、付费 CLI / diagnostic gate、隐私脱敏与 TDD。
3. 对照 dirty diff：响应摘要绑定、review 重放、失败校准落盘、capability 封印。
4. 离线探针：确认全仓仅 `calibration_attestation` 调用 `read_existing_evidence_snapshot`；`model_call_evidence_record` 清空 `provider_request_id` 但保留 `response_id`；协同改写 digest 可自洽通过报告内校验。
5. 不跑 DeepSeek；不打开真实 `artifacts/private/deepseek-budget.sqlite3`。

## Findings

### P0

（无）

未发现可在**真实付费调用路径**上绕过 SQLite 预留上限、双花同一 `(run_id, logical_call_id, attempt_number)`、或重放已存在 `run_id` 的运行时闸门漏洞。`UNIQUE` 约束 + `start_run` 冲突拒绝 + uncertain/reserved 计入 committed（含超预留下的 `MAX(reserved, settled)`）在代码层成立。

---

### P1

#### P1-1 校准 `response_content_sha256` 仅报告内自洽；协同改写 verdict+digest 仍过 live ledger matching

**适用面：both**（HEAD 更弱；dirty 未真正关闭）

| 状态 | 行为 |
|---|---|
| **HEAD** | `_validate_result` **不**要求、不校验 `response_content_sha256`。真账本 settled 49 次后，可只改 `results[].verdict` / pass 标志为夹具一致 JSON，attestation 仍过（语义轨已记为 P1-1）。 |
| **dirty** | 写入 `semantic_verdict_content_sha256(verdict)`，attestation 要求 `call.response_content_sha256 == digest(report.verdict)`。 |

**为何 dirty 仍不够：**

- Ledger `_attempt_evidence` / `read_existing_evidence_snapshot` **从不**存储响应正文或 `response_content_sha256`；匹配维度仅为 `logical_call_sha256`、settlement、usage 派生成本、时间窗。
- 攻击者把 **verdict 与 `response_content_sha256` 一起**改成夹具可过的伪造内容时：
  1. 报告内 digest 自洽 → dirty 新检查通过；
  2. fixture / grounding / gate 重算通过（伪造目标就是夹具一致）；
  3. live ledger 仍匹配原 49 次付费 settled → `_require_trusted_ledger_matches_report` 通过。
- dirty 的 review 机器重放（`_replay_reviewed_calibration_item`）同样只对照 **报告内 verdict ↔ 磁盘 fixture**，不能否定「真调用 + 假正文」合谋。

**结论：** 对「协同改写 BOTH verdict 与 digest」这一问题的直接回答是：**可以，仍能过 attestation + live ledger matching**（dirty 树上）。Digest 是报告字段间一致性锁，不是账本锚定的响应完整性。dirty TDD Boundaries 也承认 “Ledger-side response-body storage remains optional residual hardening”——在本轨威胁模型下这不是可选残留，而是 P1 未关。

**影响：** holdout eligibility 的「裁判真实 49/49」可在未改私有 ledger 的前提下被事后伪造。

---

#### P1-2 非校准付费证据（diagnostic / `dev_repeat` / 正式 bundle 校验）信任自报 usage + 自报 attempt buckets，不回读 live ledger

**适用面：both**（脏树未改此信任边界）

全仓 `read_existing_evidence_snapshot` 的生产调用点 **只有** `evals/calibration_attestation.py`。  
`evals/evidence_schema.py` 的 `_require_completed_paid_bundle_records` / `BudgetSummary` / `verify_eval_bundle` / `holdout_lock` **均不**打开固定私有 SQLite。

**攻击：**

1. 构造内部一致的付费 bundle：canonical price、reservation、`model_calls[].usage`、attempt buckets 与 `run`/`cumulative` 金额互相可重算。
2. 离线 schema / public verifier 接受；**无需**真实 ledger 行存在，也**不会**与 `artifacts/private/deepseek-budget.sqlite3` 对账。
3. 亦可在真实跑完后**下调**报告内 usage/settled，使产物声称的 run 成本低于 ledger 真相。

**对照：** 校准 attestation 已专门修过「合成报告无账本自证」；同项目付费证据链在 diagnostic / `dev_repeat` / 正式包校验上仍停留在「自报多集一致性」。命中攻击目标「evidence schemas trusting self-reported costs」与「paid eval evidence lie」。

**非 P0 原因：** 真实 HTTP 付费路径仍经 `DeepSeekBudgetGuard` 预留；本 finding 是**证据说谎**，不是运行时花超 ¥20。

---

#### P1-3 Provider 关联标识 `response_id` 进入 case / 校准报告；与项目自身红线冲突

**适用面：both**

- `model_call_evidence_record` 强制 `provider_request_id = None`（符合 `provider-request-id-redaction.tdd.md`）。
- 同路径 **原样保留** `response_id`（来自 provider JSON `id`，见 `openai_compatible.py`）。
- 严格 schema `ModelCallRecord.response_id: str | None` 接受非空；校准 fixture / 成功路径测试断言保留 `response_id`。
- 校准 attestation **不**要求 `response_id is None`。

**边界原文：** 匿名 `logical_call_sha256` 应是导出证据中**唯一**的 provider-attempt 关联标识。`response_id` 是第二类 provider 侧 correlator，进入 Eval case record 与 semantic calibration report，违反该合同。

产物默认在 gitignore 的 `artifacts/` 下，但合同写的是「不得进入 case / 校准报告」，不是「仅不得进 git」。公开/演示误打包或日志外泄时，可关联真实 provider 调用。

---

### P2

#### P2-1 `DeepSeekBudgetGuard(now=...)` 在 a10facb 起冻结整段运行时钟

**适用面：both（引入自 HEAD a10facb；dirty 未改）**

a10facb 将 `now=` 从「仅影响构造时一次 `require_current`」变为「`_now_provider = lambda: frozen_now`」，并 `bind_now_provider` 到 ledger 全部时间戳。之后价格窗检查与 settled_at 都停在冻结点。

- 生产 `build_deepseek_budget_guard` **不**传 `now` / `now_provider` → CLI 默认路径安全。
- 库 API / 程序化构造若误传 `now=datetime.now(UTC)`，会在墙钟越过 `valid_until` 后仍认为价格有效，并写出失真 ledger 时间线。

属脚枪 + 测试辅助面扩大；非默认付费 CLI 的直接旁路。

#### P2-2 路径脱敏仅覆盖 `/Users/...`，不覆盖 `/home/...` 等

**适用面：both**

`sanitize_for_evidence` 用 `_MACOS_USER_PATH_PATTERN`。Linux 宿主绝对路径、部分私有路径形态可残留在字符串字段中。密钥类键与 Bearer/email 仍有处理；`.env` / `artifacts/` 在 `.gitignore` / `.dockerignore` 中。

#### P2-3 HEAD 校准失败仍落盘可 attestation 的 schema-v2 完整报告

**适用面：HEAD-only**（dirty 已缓解）

- **HEAD：** `run_semantic_judge_calibration.py` 在 attestation 前无条件写完整报告；失败跑留下可编辑 JSON，降低 P1-1 操作成本。
- **dirty：** `gate_passed=false` 时写 `2.0-untrusted` 且 `results_omitted`；attestation 失败则尝试 `unlink` 正式报告。缓解有效，但不消除 P1-1 对**已成功落盘**报告的事后改写。

---

### note

- **Canonical pricing（both）：** `EXPECTED_CANONICAL_PRICE_FILE_SHA256` + 同字节 reparse + `require_canonical_paid_budget` / reservation 重算——付费正式证据价格身份扎实；未见用自报 snapshot 替换 canonical 文件哈希的路径。
- **Reserve / settle / uncertain / 双花 / run_id（both）：** 预留幂等返回、settled 重复须同成本、uncertain 不降 committed、`run_id` 主键冲突——运行时预算会计对抗面良好。
- **diagnostic / `dev_repeat` allowlist（both）：** `require_nonformal_paid_case_set` 要求目录 `resolve(strict=True)` 等于仓库规范路径 + 固定 case_id 序 + `case_set_sha256`。未发现通过外部 case 目录或改名绕过预预算门的路径；脏树测试在加强 formal capability，不削弱该门。
- **dirty 加固（dirty-only，本轨旁证）：** formal capability 封印 live `httpx.Client` / ledger / price 对象 id + `live_transport_mode()`；source-tree fingerprint 拒绝目录 symlink 跟随；付费 endpoint 拒绝 `/v1` path。这些主要服务 runtime 轨，降低「换 transport 逃预算可见性」风险，**不**修复 P1-1/P1-2。
- **`.env` / 账本文件进 git（both）：** `.gitignore` / `.dockerignore` 排除 `.env*` 与 `artifacts/`；`git ls-files` 未见账本或 `.env`。未发现本轨 diff 把密钥写入跟踪文件。

## Verdict Gate

**NO-GO**

运行时 SQLite 预算闸门与 canonical price 身份在「真花 DeepSeek 钱」维度可接受；但付费**证据真实性**在两处被击穿：

1. 校准成绩可在 live ledger 仍匹配时被协同改写（P1-1，dirty 未关）；
2. 非校准付费 bundle 校验信任自报成本、不回读账本（P1-2）。

另加 `response_id` 违反项目自订 provider 关联红线（P1-3）。在修复前，不得声称「validator + live ledger + review」已保证校准/付费结果不可谎报，也不得声称导出证据已去掉全部 provider 关联标识。

## Residual risks / required fixes

1. **必须（关 P1-1）：** 将规范化响应正文 / verdict 字节摘要写入 **ledger attempt 元数据**（或等价的不可由报告单方改写的信任锚），attestation 校验 `digest(report.verdict) == ledger.response_content_sha256`。仅报告内字段对打不够。
2. **必须（关 P1-2）：** 对宣称 `persistent_sqlite` 的正式 / `dev_repeat`（及任何用于门禁的付费包）校验路径，在验证时 `read_existing_evidence_snapshot` 并对齐 run_id、attempt buckets、settled 成本；拒绝「无账本自洽包」。
3. **必须（关 P1-3）：** 与 `provider_request_id` 相同，序列化与 schema 强制 `response_id: null`；校准 attestation 拒绝非空。
4. **建议（P2-1）：** 文档/API 禁止生产路径传冻结 `now=`；或恢复「`now` 只影响单次检查、后续用墙钟 / 显式 `now_provider`」。
5. **建议（P2-2）：** 扩展路径脱敏（至少 `/home/<user>/`）。
6. **保留 dirty 已做项：** 失败校准 untrusted 落盘、review 机器重放、formal client/ledger 封印——继续合入，但**不能**单独把 Verdict 从 NO-GO 拉到 GO。

**停止条件：** 本报告为预算/隐私轨对抗结论；不实现修复、不调用 DeepSeek、不提交。
