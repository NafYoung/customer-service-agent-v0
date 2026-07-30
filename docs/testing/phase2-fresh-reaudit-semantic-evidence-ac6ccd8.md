# Phase 2 Fresh Re-audit — 语义校准 / 原始证据重算

独立对抗复审（未参与实现）。只读审查；未调用 DeepSeek；未读取 `.env` / 私有账本 / 私有 Eval artifact。

## Reviewed SHA

- 要求：`ac6ccd84533f827b93287b4aece601a5f1aee0e2`
- 实测：`git rev-parse HEAD` 一致
- 工作树：审查**开始时** `git status --porcelain` 为空（clean，`VERIFY_OK`）
- 中途：并行改动使 worktree 变脏；本轨关键文件相对 HEAD 除 `evals/semantic_judge.py` 有一处与 provider request-id 红action 相关的未提交差分外，其余（`calibration_attestation.py`、`run_semantic_judge_calibration.py`、`semantic_calibration.py`、`readonly_eval.py`、`holdout_lock.py`、`deepseek_budget.py`、`test_calibration_attestation.py`）与 `ac6ccd8` 一致。对脏文件以 `git show ac6ccd8:…` 核对 digest 封印路径；结论锚定该 commit，不以并行脏改动为证据。

## Scope

本轨仅覆盖先验报告
`docs/testing/phase2-fresh-reaudit-semantic-evidence-a10facb.md` 的关闭核验：

1. Attestation 是否将 `digest(report.verdict)` 绑定到 **ledger 内** `response_content_sha256`（而非仅报告自洽）
2. 独立 review 是否机器重放抽样 fixture（而非仅 `Literal[True]` 自证）
3. 失败校准是否仍留下可 attestation 的 schema-v2 报告
4. 语义 pass 是否仍不能洗白工具/写入/状态硬失败；正式回归 raw rescore 是否完整

## Method

- 起始核验 HEAD + clean tree；中途改以 commit blob 锚定本轨
- 静态阅读：`evals/semantic_judge.py`、`evals/calibration_attestation.py`、`evals/run_semantic_judge_calibration.py`、`evals/readonly_eval.py`（`rescore_readonly_case_evidence`）、`evals/holdout_lock.py`（`validate_regression_gate`）、`app/agent/deepseek_budget.py`（`bind_response_content_sha256`）、相关 TDD
- 离线语料：49 fixtures；kind 7×7；gate 21/28
- 对抗探针：写入计数 / 禁止工具 + 接地通过语义 verdict；null verdict fail-closed；`2.0-untrusted` schema 拒绝；review `relations_match=False` 拒绝
- 聚焦测试：`test_calibration_attestation`（含 P1-1 双写篡改、P1-2 review 重放）、`test_semantic_calibration_cli`、`test_formal_regression_gate_rejects_raw_write_evidence_hidden_by_forged_pass` — 全部通过

## Prior NO-GO claim status

| 先验项 | 本 commit 状态 | 证据摘要 |
| --- | --- | --- |
| P1-1 attestation 不绑响应/仅报告自洽 | **已关闭** | `evaluate_semantic_contract` 写入 `response_content_sha256=semantic_verdict_content_sha256(verdict)` 并 `bind_*` 封印 settled attempt；`_validate_result` 要求 digest(report.verdict)==call digest；`_require_trusted_ledger_matches_report` 要求 live ledger bucket 同 digest。仅改 verdict、或同时改报告侧 digest+attempt_evidence，在 ledger 仍持原 digest 时均失败（测试覆盖） |
| P1-2 review 仅 Literal[True] | **已关闭（攻击面）** | `validate_calibration_review` 重开绑定报告，对每条抽样调用 `_replay_reviewed_calibration_item`（关系、contradiction、grounding、calibration region、gate）。`Literal[True]` 仍在 schema 上，但不足以单独过门 |
| P2-1 失败校准落盘可 attestation 报告 | **已关闭** | `gate_passed=False` 只写 `*.untrusted.json`（`schema_version=2.0-untrusted`，`results_omitted=True`，无 verdict）；`CalibrationReport` 仅接受 `Literal["2.0"]` + `semantic_judge_holdout_eligibility`；attestation 失败会 `unlink` 已写 schema-v2 报告 |
| 语义洗白硬失败 / 正式回归 raw rescore | **仍成立（可接受）** | 探针：`business_write_count=1` + 接地语义 pass → overall fail（写入）；`execute_prepared_action` + 语义 pass → overall fail（read-only）；`semantic_verdict=None` → semantic fail-closed。`validate_regression_gate` 对 28 条独立 `rescore_readonly_case_evidence`；伪造 pass 藏写入证据仍被拒 |

## Findings

### P0

（无）

### P1

（无）

先验 P1-1 / P1-2 在本 SHA 上不可再以「真账本 + 伪造报告 verdict」或「GO 回执 + Literal 勾选」单独冒充 49/49。

### P2

（无新开项）

### note

- **Review schema 残留：** `CalibrationReviewItem` 字段仍为 `Literal[True]`（人为勾选不能表达否决）。机器重放是真实闸门；勾选字段本身不再构成独立攻击面。
- **Digest 语义：** `response_content_sha256` 是**规范化 verdict JSON**（`stable_sha256(verdict.model_dump(...))`），不是原始 `turn.content` 字节。这与先验要求的「canonical digest(report.verdict) ↔ ledger」一致；不等于对未解析原文的字节级绑定。
- **Ledger 信任锚：** API 层 `bind_response_content_sha256` 对已封印 digest 拒绝改写；威胁模型若扩展到「可任意改写私有 SQLite 文件的内部人」，则需账本完整性根——本轨未升格为 P0/P1。
- **CLI 失败路径测试缺口：** 源码清晰实现 untrusted 落盘与 attestation 失败删报告，但未见专门断言 `*.untrusted.json` / unlink 的 CLI 测试（功能关闭仍成立）。
- **语料：** 49；kind 7×7；`expected_gate_pass` 21/28；unsafe_prompt_injection×7 均为 gate fail 且 `evaluator_manipulation_relation=entailed`；`both_or_ambiguous` 不能使 `score_semantic_verdict` 通过。
- **同模型相关误差 / temperature 0≠数学确定：** TDD 已声明，非本轨关闭条件。
- **holdout_formal 终包是否逐条 raw rescore：** 与先验一致，本轨仍以公开回归门独立 raw rescore 为验收；未把 holdout 终包升为 P0。

## Verdict Gate

**GO**

先验 a10facb NO-GO 的四项关闭条件在 `ac6ccd8` 上均已兑现：ledger 绑定 digest(report.verdict)、review 机器重放、失败 run 不留可 attestation schema-v2、语义不能洗白硬失败且正式回归 raw rescore 完整。本轨可进入后续 holdout 资格链（仍受预算/运行时等其他轨门禁约束）。

## Residual risks

1. 私有 ledger 文件系统被直接篡改时，响应 digest 信任锚失效（需账本完整性/只追加策略，超出本轨报告伪造模型）。
2. Review 回执勾选字段仍为装饰性 `Literal[True]`；依赖 validator 重放——勿把人工勾选误读为独立密码学证明。
3. 建议补 CLI 测试：失败校准只写 untrusted、attestation 失败删除 schema-v2 报告。
