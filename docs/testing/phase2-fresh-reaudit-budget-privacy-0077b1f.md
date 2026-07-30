# Phase 2 Fresh Re-audit — 预算 / 结果 / 隐私

独立对抗复审（未参与实现）。只读审查；未调用 DeepSeek；未读取 `.env`、真实私有账本或私有 Eval artifact 内容；未打印任何密钥。

## Reviewed SHA

| 项 | 值 |
|---|---|
| 要求 SHA | `0077b1f57ef2d5eb7155a92683041ed9e76fb38e` |
| 实测 `git rev-parse HEAD` | `0077b1f57ef2d5eb7155a92683041ed9e76fb38e`（一致） |
| HEAD 主题 | `fix: seal formal send path and bind failure ledger evidence` |
| 工作树（审查开始时） | **CLEAN**（`git status --porcelain` 空） |
| 写报告前 | 仅出现并行未跟踪产物 `docs/testing/phase2-fresh-reaudit-runtime-harness-0077b1f.md`；**无**已跟踪文件相对 HEAD 改动。审查面仍为该 commit 树。 |

相对 `ac6ccd8` 本 SHA 相关面：`formal_failure_evidence` 账本绑定、退役 formal v1 校验绑账本、校准 `_call_evidence` 强制 correlator null、既有 `paid_ledger_binding` / digest 锚定 / diagnostic·dev_repeat·formal completed 回读。

## Scope

本轨仅覆盖：

1. 先验 `ac6ccd8` NO-GO 声称关闭点是否真正关闭，并猎取残留旁路：
   - Formal failure 包 + 退役 formal v1 须回读 live ledger
   - 校准不得写入 `provider_request_id` / `response_id`
   - `response_content_sha256` 账本锚定；协同改写 verdict + digest 须失败
   - diagnostic / `dev_repeat` / formal completed 付费包须回读 SQLite
2. 运行时预算闸门、canonical pricing、allowlist、隐私泄漏旁路
3. 不覆盖 runtime harness 封印 / 语义裁判语义正确性主线（仅在其触及预算·结果·隐私时旁证）

## Method

1. 锁定 HEAD = `0077b1f`；对照 prior `phase2-fresh-reaudit-budget-privacy-ac6ccd8.md` / `a10facb.md`（威胁模型，不橡皮图章）。
2. 静态阅读：`paid_ledger_binding.py`、`formal_failure_evidence.py`、`evidence_schema.py`、`diagnostic_evidence.py`、`readonly_reporting.py`、`semantic_judge.py`、`calibration_attestation.py`、`deepseek_budget.py`、`canonical_pricing.py`、`nonformal_paid_contract.py`、`evidence.py` 脱敏、相关 TDD。
3. 离线探针：`_call_evidence` + `asdict` correlator 强制 null；`ModelCallRecord` 拒非空；binding 调用面；attempt identity 含 digest；canonical price 文件哈希；git 跟踪面无 `.env`/账本。
4. 聚焦 pytest（均通过）：formal failure 无账本/匹配临时账本/篡改 settled；校准 correlator；双改写拒绝；dev_repeat live ledger；diagnostic / response_id / `read_existing_evidence` 相关子集。
5. 不跑 DeepSeek；不打开真实 `artifacts/private/deepseek-budget.sqlite3` 内容。

## Findings

### Prior NO-GO 关闭核验

| 先验项（ac6ccd8） | 本 SHA 结论 | 证据摘要 |
|---|---|---|
| Formal failure 付费预算不回读 ledger | **已关** | `FormalFailureEvidenceBundle` 在 `summary.budget is not None` 时调用 `require_persistent_budget_matches_trusted_ledger`；captured 预算强制 `persistent_sqlite`；对抗测：缺账本拒、匹配临时账本过、篡改 settled 拒 |
| 退役 formal v1 校验早退无账本 | **已关** | `ReadonlyEvidenceBundle.cross_validate`：`holdout_formal` + `schema_version=="1.0"` + `persistent_sqlite` 调同一 binding（label `formal v1`）；manifest 侧 v1 本就要求 `persistent_sqlite` |
| 校准成功写盘可带 `provider_request_id` | **已关** | `_call_evidence` 成功/失败路径均 `provider_request_id=None` 且 `response_id=None`；`asdict` 探针与聚焦测确认；schema / attestation 仍拒非空 |
| 账本锚定 digest + 双改写 | **仍关**（本 SHA 未回退） | ledger 列 + `bind_response_content_sha256`；attestation 要求 digest(verdict)==call==ledger bucket；`test_calibration_attestation_rejects_dual_rewrite_of_verdict_and_digest` 通过 |
| diagnostic / `dev_repeat` / formal v2 completed 回读 | **仍关** | `_require_completed_paid_bundle_records` / diagnostic / readonly_reporting 均调 `paid_ledger_binding`；dev_repeat 缺账本/篡改测通过 |

---

### P0

（无）

未发现可在真实付费 HTTP 路径上绕过 SQLite 预留上限、双花同一 `(run_id, logical_call_id, attempt_number)`、或重放已存在 `run_id` 的运行时漏洞。`UNIQUE` + `start_run` 冲突拒绝 + uncertain/reserved 计入 committed 仍成立。默认 CLI `build_deepseek_budget_guard` 不冻结墙钟。

---

### P1

（无）

先验 ac6ccd8 两项 P1（formal failure / v1 不回读；校准写盘 correlator）在本威胁模型下已关闭；本轮未猎到新的门禁付费包「无账本自洽」或导出 correlator 旁路。

说明：`budget_capture_status=unavailable`（`budget is None`）的 formal failure 包**不**触发账本绑定——与 TDD「whenever a captured budget is present」一致；unavailable 不得声称派生预算金额，故不是「下调自报成本」面。

---

### P2

#### P2-1 Agent / 非校准付费调用不封印 `response_content_sha256`

与 ac6ccd8 相同：agent `ObservedChatModel` 不 bind 响应正文摘要；paid binding 对**双侧均为 null** 的 digest 视为匹配。事后改写 case 轨迹助手正文、且不改 ledger 成本/attempt 身份时，成本说谎面已由 live ledger 约束，正文完整性仍不在账本锚上。校准轨已关。

#### P2-2 `DeepSeekBudgetGuard(now=...)` 仍冻结整段时钟

误传 `now=datetime.now(UTC)` 会冻结价格窗与 ledger 时间戳。生产 builder 默认不传 → 非默认旁路。

#### P2-3 路径脱敏仍仅 `/Users/...`

`sanitize_for_evidence` 的 `_MACOS_USER_PATH_PATTERN` 未覆盖 `/home/<user>/` 等。密钥键与 Bearer/email、`provider_request_id`/`response_id` null-only 仍有处理。

#### P2-4 paid binding 不比对 cumulative 金额

比对 run identity、**run** 金额与 attempt buckets（含 digest）；故意忽略 `remaining_execution_cny`，也不要求 report.cumulative 与 ledger cumulative 一致。单 run 成本说谎已堵；累计花费叙事仍可偏短。

#### P2-5 退役 formal v1 仍跳过完整 completed paid 记录重算

v1 现已绑 live ledger（关先验 P1），但**不**走 `_require_completed_paid_bundle_records` 的 case/usage 全量重算。holdout v1 已退役且禁止重跑；残留是退役包校验完整度，不是无账本成本说谎。

---

### note（通过项）

- **Canonical pricing：** `EXPECTED_CANONICAL_PRICE_FILE_SHA256` + freeze/reparse 一致（离线核验通过）。
- **Allowlist：** `require_nonformal_paid_case_set` 仍绑 `resolve(strict=True)` 规范目录 + 固定 case 序 + `case_set_sha256`。
- **Evidence snapshot：** attempt bucket 导出含 `response_content_sha256`，**不含** `provider_request_id`（私有 SQLite 列可保留；导出面可接受）。
- **Git 跟踪面：** `git ls-files` 未见真实 `.env` / sqlite 账本。
- **校准序列化：** 不再依赖「attestation 失败再 unlink」作为唯一 correlator 屏障；写入前即 null。

## Verdict Gate

**GO**

本轨威胁模型下：门禁可用的 `persistent_sqlite` 付费证据（diagnostic / `dev_repeat` / formal v2 completed / formal failure captured / 退役 formal v1）均回读固定私有账本；校准响应 digest 仍账本锚定且双改写失败；校准与公开 model-call 导出强制 `provider_request_id`/`response_id` 为 null；运行时 ¥20 闸门与 canonical price / allowlist 未见新的 P0/P1 旁路。

残留均为 P2（agent 正文未锚定、冻结 `now=`、路径脱敏、cumulative 叙事、退役 v1 校验完整度），不阻挡本轨 Verdict GO。

## Residual risks / optional hardening

1. **建议：** agent 付费路径若需「结果」不可事后改写正文，将响应摘要 bind 进 ledger（或明确不在项目承诺内）。
2. **建议：** 扩展路径脱敏；文档禁止生产冻结 `now=`；按需比对 cumulative。
3. **可选：** 退役 formal v1 校验改为直接拒绝，或补齐与 v2 同级的 completed 记录重算（非关 GO 条件）。

**停止条件：** 本报告为预算/隐私轨对抗结论；不实现修复、不调用 DeepSeek、不提交。审查开始时工作树干净；本文件为预算/隐私轨预期新增产物（并行 runtime harness 报告不在本轨范围）。
