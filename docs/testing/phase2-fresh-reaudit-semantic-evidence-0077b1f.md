# Phase 2 Fresh Re-audit — 语义校准 / 原始证据重算

独立对抗复审（未参与实现）。只读审查；未调用 DeepSeek；未读取 `.env` / 私有账本 / 私有 Eval artifact。

## Reviewed SHA

- 要求：`0077b1f57ef2d5eb7155a92683041ed9e76fb38e`
- 实测：`git rev-parse HEAD` 一致
- 工作树：审查**开始时** `git status --porcelain` 为空（clean，`VERIFY_OK`）
- 相对先验 GO 点 `ac6ccd8`：本轨核心文件
  `calibration_attestation.py`、`semantic_calibration.py`、`run_semantic_judge_calibration.py`、`readonly_eval.py`、`holdout_lock.py`、`deepseek_budget.py`、`test_calibration_attestation.py` **零差分**
- 本轨相关唯一代码差分：`evals/semantic_judge.py`（校准 `_call_evidence` 强制 `provider_request_id=None`）+ `tests/test_semantic_calibration.py`（序列化/ correlator 断言）+ TDD 一句说明
- `0077b1f` 的 send-path / formal-failure / ledger-binding 改动落在
  `run_readonly_agent_evals.py`、`formal_failure_evidence.py`、`paid_ledger_binding.py`、`evidence_schema.py`——**不修改** `rescore_readonly_case_evidence` / `validate_regression_gate` / attestation 消化路径

## Scope

相对先验报告
`docs/testing/phase2-fresh-reaudit-semantic-evidence-ac6ccd8.md` 的回归核验：

1. Attestation 是否仍将 `digest(report.verdict)` 绑定到 **ledger 内** `response_content_sha256`
2. 独立 review 是否仍机器重放抽样 fixture（而非仅 `Literal[True]` 自证）
3. 失败校准是否仍只留 untrusted、不可 attestation 的产物
4. 语义 pass 是否仍不能洗白工具/写入/状态硬失败；正式回归 raw rescore 是否完整
5. **附加：** 校准成功路径是否永不写入 `provider_request_id`（含 `asdict` 落盘前）

## Method

- 起始核验 HEAD + clean tree；全程未联网调 DeepSeek
- 静态对比 `ac6ccd8..0077b1f` 本轨与旁路 diff；精读
  `semantic_judge._call_evidence` / `evaluate_semantic_contract`、
  `calibration_attestation`（`_validate_result`、`_require_trusted_ledger_matches_report`、`_replay_reviewed_calibration_item`）、
  `run_semantic_judge_calibration` 失败分支、`readonly_eval.rescore_readonly_case_evidence`、
  `holdout_lock.validate_regression_gate`
- 离线语料：49 fixtures；kind 7×7；gate 21/28；unsafe_prompt_injection×7 均为 gate fail 且 `evaluator_manipulation_relation=entailed`
- 对抗探针：写入计数 / 禁止工具 + 接地语义 pass；null verdict fail-closed；`2.0-untrusted` schema 拒绝；`both_or_ambiguous` 不能过 `score_semantic_verdict`；泄漏 correlator turn → `_call_evidence` 仍 null
- 聚焦测试：P1-1 单改/双改 digest、P1-2 review 重放、correlator 单测、
  `test_formal_regression_gate_rejects_raw_write_evidence_hidden_by_forged_pass`——全部通过

## Prior GO claim status (ac6ccd8 → 0077b1f)

| 先验项 | 本 commit 状态 | 证据摘要 |
| --- | --- | --- |
| P1-1 attestation 绑 ledger digest | **仍关闭** | 核心 attestation/ledger 代码相对 `ac6ccd8` 零差分。`evaluate_semantic_contract` 仍 `bind_response_content_sha256`；`_validate_result` 要求 `digest(verdict)==call.digest`；`_require_trusted_ledger_matches_report` 经 live `read_existing_evidence_snapshot` 与含 digest 的 attempt bucket identity 对账。单改 verdict 或双改报告侧 digest+attempt_evidence 仍失败（测试覆盖） |
| P1-2 review 机器重放 | **仍关闭** | `validate_calibration_review` → `_replay_reviewed_calibration_item`（关系、contradiction、grounding、gate）。`Literal[True]` 勾选仍不足单独过门；错 verdict + GO 回执仍拒 |
| P2-1 失败校准不可 attestation | **仍关闭** | `gate_passed=False` 只写 `*.untrusted.json`（`2.0-untrusted`，`results_omitted=True`）；`CalibrationReport` 仅 `Literal["2.0"]`；探针拒绝 untrusted schema；attestation 失败仍 `unlink` schema-v2 报告 |
| 语义洗白硬失败 / 正式 raw rescore | **仍成立** | 探针：接地语义 pass + `business_write_count=1` → overall fail（写入/状态硬失败保留，语义 claim 失败数可为 0）；`execute_prepared_action` → read-only 失败；`semantic_verdict=None` → fail-closed。`validate_regression_gate` 仍对 28 条独立 `rescore_readonly_case_evidence` 且 `raw_passed_trials != 28` 拒绝；伪造 pass 藏写入证据测试通过 |
| 校准成功路径 `provider_request_id` | **已确认关闭** | `_call_evidence` 成功与错误路径均强制 `provider_request_id=None`（不再回填 `turn`/`error`）；`response_id` 同为 null。单测覆盖 `asdict` 序列化。attestation 仍拒绝非空 correlator（纵深） |

## Findings

### P0

（无）

### P1

（无）

### P2

（无新开项）

### note

- **相对 ac6ccd8 的净变化：** 隐私旁路（校准成功写盘可带 `provider_request_id`）在本 SHA 已从源路径消除；不削弱 digest/ledger/review/rescore 门。
- **send-path / formal-failure：** 加强 httpx 封印与失败包 ledger binding；不触及语义评分累积或回归 raw rescore 合同。
- **Review schema 残留：** `CalibrationReviewItem` 仍为 `Literal[True]`；机器重放仍是真实闸门（同 ac6ccd8 note）。
- **Digest 语义：** `response_content_sha256` = 规范化 verdict JSON，非原始 `turn.content` 字节（同先验）。
- **CLI 失败路径测试缺口：** 仍未见专门断言 `*.untrusted.json` / attestation `unlink` 的 CLI 测试（功能关闭仍成立，同 ac6ccd8）。
- **Ledger 文件系统内部人篡改：** 信任锚仍依赖私有 SQLite 完整性；未升格为本轨 P0/P1。

## Verdict Gate

**GO**

`ac6ccd8` 四项关闭条件在 `0077b1f` 上全部回归成立；校准成功路径不再写入 `provider_request_id`。本轨可继续受其他轨（预算/运行时等）门禁约束进入后续 holdout 资格链。

## Residual risks

1. 私有 ledger 被直接篡改时响应 digest 信任锚失效（超出本轨报告伪造模型）。
2. Review 勾选字段仍为装饰性 `Literal[True]`；勿误读为独立证明。
3. 建议补 CLI 测试：失败校准只写 untrusted、attestation 失败删除 schema-v2 报告。
