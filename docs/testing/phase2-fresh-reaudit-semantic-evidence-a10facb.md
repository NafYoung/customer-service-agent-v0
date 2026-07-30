# Phase 2 Fresh Re-audit — 语义校准 / 原始证据重算

独立对抗复审（未参与实现）。只读审查；未调用 DeepSeek；未读取 `.env` / 私有账本 / 私有 Eval artifact。

## Reviewed SHA

- 要求：`a10facb87019249b4fc48adc20d8b254a1131bc0`
- 实测：`git rev-parse HEAD` 一致
- 工作树：审查开始时 `git status --porcelain` 为空（clean）

## Scope

本轨仅覆盖：

1. 语义裁判是否能覆盖/洗白工具、权限、写入、业务状态等硬失败
2. 校准协议：49 fixtures、evidence regions、矛盾双侧引用、validator 是否从 live ledger 重算
3. 正式回归是否从原始 answer / trajectory / state / writes / verdicts 确定性重算，拒绝自报 pass
4. 校准 attestation → review receipt → holdout manifest 绑定链
5. fixture 提示注入 / 不安全裁判指令；schema 歧义是否失败关闭

主要阅读面：`evals/semantic_judge.py`、`evals/semantic_calibration.py`、`evals/calibration_attestation.py`、`evals/run_semantic_judge_calibration.py`、`evals/readonly_eval.py`（`rescore_readonly_case_evidence`）、`evals/holdout_lock.py` / `evals/evidence_schema.py` 中的正式回归重算路径、相关 tests 与 TDD。

## Method

- 核验 HEAD 与 clean tree
- 静态阅读评分、接地、校准、attestation、CLI、holdout 绑定
- 离线加载并校验 49 条规范语料（kind 计数、gate 标签、注入夹具）
- 对抗探针：写入/禁止工具 + 完美语义 verdict 的 raw rescore；空答案 + verdict；null verdict fail-closed；`logical_call_sha256` 语义；review 是否重开 fixture/verdict；attestation 是否绑定模型响应正文

## Findings

### P0

（无）

未发现「语义 pass 直接覆盖工具 / 写入 / 状态硬失败」的可利用路径。`rescore_readonly_case_evidence` 将全部 check 累积为 failures，`passed = not failures`；语义成功不能清除 security/tool/write 失败。探针：

- `business_write_count=1` + 接地通过的安全语义 verdict → overall fail（写入计数）
- `execute_prepared_action` 出现在 tool_trace + 安全语义 verdict → overall fail（read-only / forbidden tool）
- `semantic_contract` 存在且 `semantic_verdict=None` → semantic categories fail-closed

正式回归门在 schema 层对 `dev_repeat` 调用 `_require_canonical_record_rescore`，并在 `validate_regression_gate` 中再次对 28 条 raw evidence 独立 `rescore_readonly_case_evidence`，要求 `raw_passed_trials == 28`；自报 scores/status 与 raw 不一致会被拒绝（已有 `test_formal_regression_gate_rejects_raw_write_evidence_hidden_by_forged_pass`）。

### P1

#### P1-1 校准 attestation 不绑定裁判模型响应正文；可用「真账本 + 伪造 verdict」冒充 49/49

**位置：** `app/agent/deepseek_budget.py`（`logical_call_sha256` = `sha256(logical_call_id)`，`logical_call_id` 为随机 UUID）；`evals/calibration_attestation.py` 的 `_validate_result` / `validate_calibration_attestation`；`evals/run_semantic_judge_calibration.py` 写报告路径。

**攻击：**

1. 真实跑完 holdout-eligible 校准（49 次付费 settled 进入固定私有 ledger）。
2. 若裁判未 49/49：保留报告中的 `model_calls`（含 `logical_call_sha256`、usage）与 budget 身份。
3. 将每条 `results[].verdict` / `observed_relations` / pass 标志改写成与 fixture 期望关系、人工 evidence region、矛盾双侧完全一致的 JSON。
4. 对篡改后的报告再次调用 `validate_calibration_attestation`。

**为何能过：** validator 重算的是「报告内 verdict ↔ 磁盘 fixture/合同」，并要求 49 个 call digest 与 live ledger settled attempt 对齐；**从不**校验 `turn.content`、响应正文哈希或「该 logical call 实际返回的 JSON」。`docs/testing/calibration-live-ledger-attestation.tdd.md` Boundaries 亦写明 response-content hash 不在范围——但在「校准成绩不可自证」威胁模型下，这恰好留下完整冒充面。

**影响：** holdout eligibility 的「DeepSeek 裁判已在 49 夹具上校准通过」可在未改 ledger 的前提下被事后伪造。这直接命中攻击目标 2/3（校准真实性 / 拒绝自报 pass）在语义成绩维度上的缺口。

#### P1-2 独立 review 回执为 Literal 自证，不重放 fixture/verdict/模型输出

**位置：** `CalibrationReviewItem`（`relations_match` / `grounding_valid` / `contradiction_label_matches` 均为 `Literal[True]`）；`validate_calibration_review`。

**攻击：** 只要 `calibration_report_sha256` 绑定、抽样覆盖 `required_review_fixture_ids`（分层 ≥10%）、时间序与 `conclusion=GO`，回执即可通过。函数源码不打开 fixture、不读报告 verdict、不要求模型 transcript。

**与 P1-1 合击：** 伪造到「与标签一致」的 verdict 后，人工抽查若只对照 fixture 期望关系，也会「看起来正确」；回执无法密码学地否定该伪造。链上的 report→review→holdout declaration 哈希绑定仍在，但绑定的是**可被替换的语义成绩叙述**，不是裁判原始输出。

### P2

#### P2-1 失败校准仍落盘完整报告，降低篡改成本

`run_semantic_judge_calibration.py` 在 attestation 之前无条件 `_write_private_report`。未过门的 run 也会留下可编辑的私有 JSON；配合 P1-1 形成「跑完 → 改 verdict → 重验证」操作路径。应考虑仅在 `gate_passed` 且 attestation 成功后落盘，或落盘失败报告时剥离/封印 verdict 并标记 `untrusted`。

#### P2-2 非校准路径的矛盾 grounding 只要求「双侧非重叠原文 span」

`validate_semantic_verdict_grounding` 不验证两侧语义对立，只验证落地与非重叠。生产评分仍依赖裁判诚实标注 `material_self_contradiction`；短语黑名单与工具/写入硬检查可部分补偿。校准夹具对 contradiction / ambiguous 有更强 region+side 约束，但正式 case 重算不走 calibration region 绑定（设计如此）。残留：裁判漏标矛盾时，若短语层也未覆盖，语义层可能假阴性——属裁判准确率问题，不是硬失败覆盖。

### note

- **规范语料：** 49 fixtures；kind 计数 7×7；`expected_gate_pass` 21/28；unsafe_prompt_injection 七条均为 gate fail 且 `evaluator_manipulation_relation=entailed`；safe 注入夹具显式否认操纵。`both_or_ambiguous` 对 required/forbidden 均不能使 `score_semantic_verdict` 通过（歧义失败关闭）。
- **Live ledger：** 缺失/权限错误/合成无账本报告被拒绝；49 唯一 `logical_call_sha256` ↔ settled attempt 有测试覆盖。该项对「无调用自证」有效，不关闭 P1-1。
- **Holdout 绑定链：** `ValidatedFormalRunContext` 要求 attestation/report/review/declaration 的 report_sha256、review_sha256、run_id、fixture/contract/harness、reviewer 字段一致——链存在；信任锚受 P1-1/P1-2 削弱。
- **同模型相关误差、temperature 0≠数学确定、JSON schema≠语义正确：** TDD 已声明；正式校准依赖 49/49 + 抽查，但抽查机制见 P1-2。
- **本轨未把 `holdout_formal` 终包是否逐条 raw rescore 升为 P0**（`evidence_schema` 对 `dev_repeat` 有 `_require_canonical_record_rescore`，对 `holdout_formal` 未见同等循环）；正式**公开回归门**本身有独立 raw rescore。若需扩轨可另开。

## Verdict Gate

**NO-GO**

硬失败隔离与正式回归 raw 重算在本轨攻击目标 1/3（工具写入侧）上可接受；但校准成绩真实性（目标 2/4）在自证/私有产物篡改模型下被 P1-1 与 P1-2 击穿——不能声称「validator + review + ledger」已保证裁判真实 49/49。

## Residual risks / required fixes

1. **必须：** 将裁判响应正文（或规范化 verdict 字节）纳入证据绑定——例如对 `complete_json` 返回内容做 `response_content_sha256`，写入 model_call evidence 与/或 ledger attempt 元数据；attestation 校验报告 verdict 的 canonical JSON digest 与该哈希一致（或要求 ledger 存 response digest）。
2. **必须：** 强化 review：至少由 validator 重放抽样 fixture 的报告 verdict 接地与期望关系（机器可做部分），并要求回执引用不可伪造的响应摘要；禁止仅靠 `Literal[True]` 自证。
3. **建议：** CLI 对未过门 run 不写可 attestation 的 schema-v2 报告，或写入后密封为 `gate_passed=false` 且拒绝 attestation。
4. **残留：** 同模型 actor/judge 相关错误、无响应绑定时的内部人篡改面，在修复 1–2 前不得进入 holdout formal。
