# Phase 2 Fresh Re-audit — 预算 / 结果 / 隐私

独立对抗复审（未参与实现）。只读审查；未调用 DeepSeek；未读取 `.env`、真实私有账本或私有 Eval artifact 内容；未打印任何密钥。

## Reviewed SHA

| 项 | 值 |
|---|---|
| 要求 SHA | `ac6ccd84533f827b93287b4aece601a5f1aee0e2` |
| 实测 `git rev-parse HEAD` | `ac6ccd84533f827b93287b4aece601a5f1aee0e2`（一致） |
| HEAD 主题 | `fix: seal Phase 2 runtime and bind paid evidence digests` |
| 工作树（审查开始时） | **CLEAN**（`git status --porcelain` 空） |

相对 `a10facb` 的相关面：`deepseek_budget` 账本 digest 列与 `bind_response_content_sha256`、`evals/paid_ledger_binding.py`、校准 attestation、evidence schema / diagnostic / readonly 付费校验、`response_id` 脱敏与测试。

## Scope

本轨仅覆盖：

1. 先验 a10facb NO-GO 三项声称关闭点是否真正关闭，并猎取残留旁路：
   - `response_content_sha256` 须账本锚定；协同改写 verdict + 报告 digest 须失败
   - `persistent_sqlite` 付费包须回读 live SQLite，不得只信自报成本
   - 导出证据中 `response_id` 须为 null（同 `provider_request_id`）
2. 运行时预算闸门、canonical pricing 身份、隐私泄漏、diagnostic / `dev_repeat` allowlist
3. 不覆盖 runtime harness 封印 / 语义裁判语义正确性主线（仅在其触及预算·结果·隐私时旁证）

## Method

1. 锁定 HEAD = `ac6ccd8` 且工作树干净；对照 prior 报告 `phase2-fresh-reaudit-budget-privacy-a10facb.md`（仅作威胁模型，不橡皮图章）。
2. 静态阅读：`app/agent/deepseek_budget.py`、`openai_compatible.py`、`evals/paid_ledger_binding.py`、`calibration_attestation.py`、`evidence{,_schema}.py`、`diagnostic_evidence.py`、`readonly_reporting.py`、`formal_failure_evidence.py`、`semantic_judge.py`、`run_semantic_judge_calibration.py`、相关 TDD。
3. 离线探针：`ModelCallRecord` 拒绝非空 `response_id` / `provider_request_id`；`model_call_evidence_record` 双字段强制 null；`require_persistent_budget_matches_trusted_ledger` 调用面；校准 `_call_evidence` + `asdict` 是否仍带出 correlator；attempt bucket identity 是否含 digest。
4. 聚焦 pytest：双改写拒绝、合成报告无账本拒绝、dev_repeat live ledger 匹配/缺失/篡改成本、`response_id` schema、semantic_judge / readonly_reporting 相关集。
5. `git ls-files` 未见真实 `.env` / 账本；仅见 `.env.example`。

## Findings

### Prior P1 关闭核验

| 先验项 | 本 SHA 结论 | 证据摘要 |
|---|---|---|
| P1-1 协同改写 verdict+digest | **已关** | 账本 `budget_attempts.response_content_sha256`；`bind_response_content_sha256` 一次封印；attestation 要求 `digest(verdict)==call.digest==ledger bucket.digest`；双改写报告字段仍与 trusted snapshot 冲突 |
| P1-2 付费包不回读 ledger | **部分关闭** | diagnostic / `dev_repeat` / formal v2 completed `ReadonlyEvidenceBundle` 经 `paid_ledger_binding` 回读固定私有 SQLite；**formal failure（失败 holdout 门禁包）仍不回读** |
| P1-3 `response_id` 导出 | **已关（就该字段）** | `model_call_evidence_record` / schema `response_id: None` / 校准 `_call_evidence` 写死 null / attestation 拒非空；sanitize null-only 键含 `response_id` |

---

### P0

（无）

未发现可在真实付费 HTTP 路径上绕过 SQLite 预留上限、双花同一 `(run_id, logical_call_id, attempt_number)`、或重放已存在 `run_id` 的运行时漏洞。`UNIQUE` + `start_run` 冲突拒绝 + uncertain/reserved 计入 committed 仍成立。默认 CLI `build_deepseek_budget_guard` 不冻结墙钟。

---

### P1

#### P1-1（残留）`formal_failure` 付费预算仍只信自报，不回读 live ledger

**状态：先验 P1-2 在「任何用于门禁的付费包」口径下未关完**

`evals/paid_ledger_binding.require_persistent_budget_matches_trusted_ledger` 已接到：

- `evals/evidence_schema._require_completed_paid_bundle_records`（`dev_repeat` / formal v2 completed）
- `evals/diagnostic_evidence._require_paid_diagnostic`
- `evals/readonly_reporting._require_completed_paid_evidence`

但 `evals/formal_failure_evidence.py` **零**引用 `read_existing_evidence_snapshot` / `paid_ledger_binding`。`holdout_lock.verify_failed_holdout_receipt_chain` 以 `validate_formal_failure_bundle` 验收失败正式包——该路径仍可对内部自洽的 `persistent_sqlite` budget / attempt buckets 放行，**无需**固定私有账本行存在，也可在真跑后下调报告成本而不与 live ledger 对账。

另：`holdout_formal` + `schema_version=="1.0"`（已退役 v1）在 manifest 校验后早退，**不**进入 `_require_completed_paid_bundle_records`，同属无账本自洽面（退役但仍可被 schema 接受）。

**非 P0 原因：** 真花钱仍经 `DeepSeekBudgetGuard`；本项是门禁**证据说谎**面，不是超 ¥20 运行时旁路。

---

#### P1-2（新/旁路）校准报告序列化绕过 `model_call_evidence_record`，成功路径可落盘 `provider_request_id`

**状态：`response_id` 已关；同合同 correlator `provider_request_id` 在校准写盘路径未关**

- `evals/semantic_judge._call_evidence`：`response_id=None`（好），但成功时 `provider_request_id=turn.provider_request_id`（来自 `x-request-id` / `request-id`）。
- `run_semantic_judge_calibration` 用 `asdict(result)` 写私有报告，**不**经 `model_call_evidence_record` / `sanitize_for_evidence`。
- 离线探针：带 `provider_request_id='req-LEAK'` 的 turn → asdict 证据仍含泄漏值。
- 公开 schema / attestation **拒绝**非空 `provider_request_id` → 有头时 attestation 失败并 `unlink`（失败关闭），但：
  1. 与 `provider-request-id-redaction.tdd.md`「不得进入校准报告」冲突（写盘即进入）；
  2. write→attest→unlink 窗口与异常中断可残留含 correlator 的文件；
  3. 若真实 DeepSeek 返回 request-id 头，**可 attestation 的成功落盘与隐私红线互相打架**（功能/隐私双压）。

相对先验 P1-3：本 SHA 关掉了 `response_id`，但校准写路径仍暴露第二类（其实是合同已点名的第一类）provider 关联标识。

---

### P2

#### P2-1 Agent / 非校准付费调用不封印 `response_content_sha256`

agent `ObservedChatModel` 不写、不 bind 响应正文摘要；paid binding 对 **双侧均为 null** 的 digest 视为匹配。攻击者在 **不改 ledger 成本/attempt 身份** 前提下仍可事后改写 case 轨迹中的助手正文；成本说谎面已由 P1-2 主路径约束，正文完整性不在账本锚上。校准轨已用 verdict digest 关闭；此处为非校准「结果」残留。

#### P2-2 `DeepSeekBudgetGuard(now=...)` 仍冻结整段时钟

与 a10facb 相同：误传 `now=datetime.now(UTC)` 会冻结价格窗与 ledger 时间戳。生产 builder 默认不传 → 非默认旁路。

#### P2-3 路径脱敏仍仅 `/Users/...`

`sanitize_for_evidence` 的 `_MACOS_USER_PATH_PATTERN` 未覆盖 `/home/<user>/` 等。密钥键与 Bearer/email 仍有处理。

#### P2-4 paid binding 不比对 cumulative 金额

`require_persistent_budget_matches_trusted_ledger` 比对 run identity、**run** 金额与 attempt buckets；故意忽略 `remaining_execution_cny`，也**不**要求 report.cumulative 与 ledger cumulative 一致。单 run 成本说谎已堵；累计花费叙事仍可偏短（影响表述，不放行无账本 run）。

---

### note（通过项）

- **P1-1 主攻击（不改私有 ledger，只改报告）：** 关闭。`test_calibration_attestation_rejects_dual_rewrite_of_verdict_and_digest` 等聚焦测通过。
- **diagnostic / dev_repeat / formal v2 completed：** 无账本或篡改 settled 被 `paid_ledger_binding` 拒绝；匹配临时私有账本可通过。
- **Canonical pricing：** `EXPECTED_CANONICAL_PRICE_FILE_SHA256` + reparse + reservation 重算仍扎实。
- **Allowlist：** `require_nonformal_paid_case_set` / payload 仍绑仓库规范目录 + 固定 case 序 + `case_set_sha256`；未见外部目录换皮绕过预预算门。
- **Git 跟踪面：** 未见真实 `.env` / sqlite 账本入仓。
- **Ledger 内 `provider_request_id` 列：** 存于私有 SQLite；evidence snapshot bucket **不**导出该字段（可接受）。

## Verdict Gate

**NO-GO**

三项声称修复中：**校准响应 digest 账本锚定（P1-1）** 与 **导出 `response_id` null（P1-3）** 在本威胁模型下成立；**`persistent_sqlite` 付费证据必须回读 live ledger（P1-2）** 在 diagnostic / `dev_repeat` / formal v2 **completed** 包上成立，但 **formal failure 门禁包（及退役 formal v1 校验早退）仍可无账本自洽**，与先验「任何用于门禁的付费包」关闭条件不符。

另：校准成功写盘仍可落入 `provider_request_id`，与项目自订 provider 关联红线冲突（attestation 失败关闭不能当作「未进入报告」）。

在修好 formal failure（及必要时 v1）账本绑定、并让校准序列化与 `model_call_evidence_record`/null-only 合同一致之前，不得声称本轨预算/结果/隐私证据链已对门禁付费包全面不可谎报、导出已去掉全部 provider 关联标识。

## Residual risks / required fixes

1. **必须（关完 P1-2）：** `validate_formal_failure_bundle` / 失败 holdout 链在 `enforcement_mode==persistent_sqlite` 时调用 `require_persistent_budget_matches_trusted_ledger`（或等价只读 snapshot 对齐）。退役 formal v1 要么拒绝校验、要么同样绑账本。
2. **必须（关 P1-2 隐私旁路）：** 校准结果落盘前对 model_calls 走 `model_call_evidence_record`（或等价强制 `provider_request_id`/`response_id` 为 null）；`_call_evidence` 成功路径不要回填 turn 上的 request id。
3. **建议：** agent 付费路径若需「结果」不可事后改写，将响应摘要 bind 进 ledger（或明确不在本项目承诺内）。
4. **建议：** 扩展路径脱敏；文档禁止生产冻结 `now=`；按需比对 cumulative（若对外声称累计花费）。

**停止条件：** 本报告为预算/隐私轨对抗结论；不实现修复、不调用 DeepSeek、不提交。审查开始时工作树干净；本文件为唯一预期新增产物。
