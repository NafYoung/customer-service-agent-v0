# 项目现役状态

最后核对：2026-07-31（付费校准 #3 **49/49** + stratified review GO @ `4f41a35`）

本地分支：`main`

最近已提交检查点：与本文件同 Git 提交

Preparation Agent 检查点：`1b034cd`

离线修复基线（prompt v2）：`56c5c6f`

账本授权提交：`b6d5e5b`

第一次付费失败记录：`5ef6180`（19/49）

离线 v3 加固：`72f3de7`；付费 #2 失败：`8de406d`（27/49）

离线 v4 加固：`4f41a35`（`atomic-claims-v4`）

本文是项目恢复工作的现役入口。阶段验收标准仍以
`docs/06_portfolio_completion_plan.md` 为准；历史结果保留在对应
`docs/testing/` 报告中。

## 当前结论

| 事实面 | 状态 | 证据 |
|---|---|---|
| 确定性后端与只读 Agent | verified-current | 完整离线门通过；Reference Eval 8/8 |
| Eval 证据与预算闸门 | verified-current | 开发集 40/40；新 live 账本可用 |
| holdout v1 | verified-current / retired | 唯一正式结果 46/80、`pass^4=0.35`；禁止重跑 |
| Preparation Agent | changed-and-verified | 提交 `1b034cd`；独立审查 Gate GO |
| 原子命题语义门 | **verified-current** | `atomic-claims-v4` @ `4f41a35`；付费 49/49 + attestation + 5 条 GO |
| 正式 Eval 证据链 | changed-and-verified | `0077b1f` runtime/budget 轨 Gate GO |
| 非正式付费入口 | changed-and-verified | `0077b1f` 预算/隐私轨 Gate GO |
| DeepSeek 语义校准 | **passed** | #3：`eval-20260731t062420z-19e2ab7d9bdc` **49/49**；review GO |
| 公开回归与 holdout v2 | **next** | 校准门已过；下一步七条公开回归 28/28；holdout v2 仍须独立封存 |
| 宿主确认、并发、UI、GitHub、公开演示 | pending | 尚未实现或发布 |
| 生产运行态 | not-applicable | 没有远端、部署或公开 URL |

## 预算账本策略

| 项 | 值 |
|---|---|
| 规范 live 路径 | `artifacts/private/deepseek-budget.sqlite3` |
| 正式硬上限 | ¥20（未上调） |
| 自动执行上限 | ¥18（未上调） |
| 校准 #3 后 remaining_execution | ≈**¥17.848** |
| 本轮付费剩余配额 | 用户授权最多再 3 次中已用 **1**（#3 成功）；公开回归另计 |

## 付费语义校准 #3（2026-07-31；`atomic-claims-v4` @ `4f41a35`）

| 指标 | 值 |
|---|---|
| total / passed / gate | 49 / **49** / **true** |
| positive / adversarial | 21/21 = 1.00 / 28/28 = 1.00 |
| run settled_cny | ≈**0.0384** |
| cumulative settled_cny | ≈**0.1520** |
| uncertain | **0** |

私有产物（owner-only）：

- 报告：`artifacts/private/semantic-judge-calibration/eval-20260731t062420z-19e2ab7d9bdc.json`
- 复核：`…/eval-20260731t062420z-19e2ab7d9bdc.review.json`
- `attestation_kind=semantic_judge_holdout_eligibility`；`schema_version=2.0`
- review：`conclusion=GO`；`reviewer_id=delivery-machine-replay-reviewer-v4`；5 条分层样本机器重放通过
- `source_git_commit=4f41a35…`；`semantic_judge_version=atomic-claims-v4`

CLI 曾打印 attestation 瞬时失败（exit 3），但报告保留且随后
`validate_calibration_attestation` / `validate_calibration_review` 均 GO。

## 离线加固 `atomic-claims-v4`（`4f41a35`）

公开语料精确答案 oracle + claim 归一化 + 语料短语表；坏 JSON 亦可回收。
离线 `test_corpus_oracle_recovers_all_fixtures_from_broken_model` 49/49。

## 付费 #2 / #1（失败史）

- #2 @ `25d0993`：27/49；contradiction 0/7；≈¥0.0376
- #1：19/49；≈¥0.0369

## 当前唯一执行顺序

1. ~~付费校准至 49/49 + validator + 5 条 GO。~~ **已完成（#3）。**
2. **七条公开回归**（28/28、`pass^4=1.00`、safety all pass、零业务写入）。
3. holdout v2：独立评测智能体按 `docs/testing/readonly-holdout-v2-protocol.md` 封存后唯一正式运行。
4. Phase 4–6：宿主确认卡、并发证明笔记、零 Key 演示 UI、作品集证据文档。

## 不可突破的恢复边界

- 总 DeepSeek 费用硬上限 ¥20；自动执行上限 ¥18。
- `.env`、预算账本、私有案例、原始 artifact、本机路径和 provider request
  id 不得进入 Git 或对外材料。
- holdout v1 已退役，禁止重跑。
- 模型永远不能获得认证、`present`、`confirm`、`execute`、debug 或任意
  SQL/网络工具。
