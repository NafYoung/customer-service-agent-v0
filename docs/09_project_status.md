# 项目现役状态

最后核对：2026-07-31（用户明示批准新本地预算账本；付费语义校准进行中或见下文结果）

本地分支：`main`

最近已提交检查点：与本文件同 Git 提交

Preparation Agent 检查点：`1b034cd`

离线修复基线：`56c5c6f`（语义裁判 prompt v2 + 付费路径传输失败快速失败关闭）

本文是项目恢复工作的现役入口。阶段验收标准仍以
`docs/06_portfolio_completion_plan.md` 为准；历史结果保留在对应
`docs/testing/` 报告中。

## 当前结论

| 事实面 | 状态 | 证据 |
|---|---|---|
| 确定性后端与只读 Agent | verified-current | 完整离线门通过；Reference Eval 8/8 |
| Eval 证据与预算闸门 | verified-current | 开发集 40/40；见账本策略 |
| holdout v1 | verified-current / retired | 唯一正式结果 46/80、`pass^4=0.35`；禁止重跑 |
| Preparation Agent | changed-and-verified | 提交 `1b034cd`；独立审查 Gate GO |
| 原子命题语义门 | changed-and-verified | `0077b1f` 三路 fresh reaudit ALL GO；离线 prompt v2 于 `56c5c6f` |
| 正式 Eval 证据链 | changed-and-verified | `0077b1f` runtime/budget 轨 Gate GO |
| 非正式付费入口 | changed-and-verified | `0077b1f` 预算/隐私轨 Gate GO |
| DeepSeek 语义校准 | **in progress / see results** | 用户 2026-07-31 明示批准新 ledger 后重跑；见下文 |
| 公开回归与 holdout v2 | pending gate | 校准 + 独立 GO 复核通过后才跑七条回归；holdout v2 需独立评测智能体封存，禁止本会话自封 |
| 宿主确认、并发、UI、GitHub、公开演示 | pending | 尚未实现或发布 |
| 生产运行态 | not-applicable | 没有远端、部署或公开 URL |
| Agent 记忆 | generated-read-only | 本次未获授权写入，也未修改 |

## 预算账本策略（2026-07-31 用户明示批准）

用户明示批准：归档已耗尽的本地 live 账本，换用全新本地账本，并继续付费语义校准（校准通过后跑公开回归）。开发不再被已耗尽本地闸门阻塞；DeepSeek 账户侧配额充足。合同上限不变。

| 项 | 值 |
|---|---|
| 归档只读路径 | `artifacts/private/deepseek-budget.exhausted-20260731T053125Z.sqlite3`（mode 0444；**未删除**） |
| 规范 live 路径 | `artifacts/private/deepseek-budget.sqlite3`（attestation 绑定固定路径；由下一次付费 run 新建） |
| 正式硬上限 | ¥20（`FORMAL_HARD_LIMIT_CNY`；证据 schema 钉死，未上调） |
| 自动执行上限 | ¥18（`FORMAL_EXECUTION_LIMIT_CNY`；未上调；新账本恢复满额头寸） |
| 传输×预算 | `56c5c6f`：付费路径 `httpx.RequestError` 首次预留并 `uncertain` 后立即失败关闭，不再按 `max_retries` 连环 uncertain |

旧归档账本中的 uncertain 预留仍保留为审计证据，但**不再**计入新 live 账本的 committed。不得把旧 uncertain 当作「未发生」；只是换了新执行身份的本地闸门。若新账本在校准中再次被不公平耗尽，停止付费并报告，**不得**静默上调 `FORMAL_*`。

## 历史：付费语义校准失败（2026-07-31 早先，旧 live 账本）

在三路 GO 基线 `0077b1f` 之上、文档检查点 `e2cd096` 上执行了公开语义校准。
私有失败产物（`results_omitted=true`，不可 attestation）：

- `artifacts/private/semantic-judge-calibration/eval-20260731t010742z-e42de5ec196b.untrusted.json`
- `attestation_kind=semantic_judge_calibration_failed`；`schema_version=2.0-untrusted`
- `source_git_commit=e2cd096…`；`run_id=eval-20260731t010742z-e42de5ec196b`

### 门禁结果（裁判质量为主因）

| 指标 | 值 |
|---|---|
| total / passed / gate | 49 / 16 / **false** |
| positive（期望 gate pass） | 11/21 ≈ 0.52 |
| adversarial（期望 gate fail） | 5/28 ≈ 0.18 |
| failed_fixture_ids | 33 |

结论：**该次校准门未过，主因是语义裁判准确率不足**；随后 `56c5c6f` 做了离线 prompt / 传输修复。

### 协议 / 预算副作用（已归档账本）

同一次 run 在旧 live 上产生 17 笔 `MODEL_TRANSPORT_ERROR` uncertain，把
`remaining_execution_cny` 锁到 ≈0.81，¥18 自动执行合同下无法再 `reserve`。
该账本已按用户批准归档为
`deepseek-budget.exhausted-20260731T053125Z.sqlite3`。

## 离线修复（`56c5c6f`；未调用 DeepSeek）

1. **语义裁判合同清晰化（`atomic-claims-v2`）**
   - 更新 `evals/semantic_judge_prompt.md`：固定评估顺序；强化
     evaluator-manipulation 与 `both_or_ambiguous` /
     `material_self_contradiction` 决策规则。
   - 离线：49 条 label oracle 可复现 21/28 gate。
   - **不**单独声称 DeepSeek 实跑准确率已恢复。

2. **传输×预算：付费路径传输失败快速失败关闭**
   - 存在 `budget_guard` 时，`httpx.RequestError` 在首次预留并
     `uncertain` 后立即抛出，不再按 `max_retries` 追加预留。

## 最近验证

对基线 `0077b1f57ef2d5eb7155a92683041ed9e76fb38e` 的三路全新
`phase2_fresh_adversarial_reaudit` 仍为 ALL GO。三路 GO **不**因早先校准失败而撤销。

价格快照：`pricing/deepseek-v4-flash-2026-07-30.json`，
`valid_until=2026-08-06T17:20:00+00:00`（本轮启动时仍有效）。

## 当前唯一执行顺序

1. ~~三路全新 `phase2_fresh_adversarial_reaudit`。~~ **已完成：`0077b1f` ALL GO。**
2. ~~离线裁判修复与传输×预算降耗。~~ **`56c5c6f`。**
3. ~~用户明示批准新 ledger。~~ **2026-07-31 已批准；旧账本已归档。**
4. 付费公开语义校准 49/49（本轮）；失败则停止继续付费重试，改离线分析。
5. 校准 49/49 + validator 重算 + 预算结清 + 5 条程序性独立 GO 复核后，才跑七条公开回归。
6. 公开回归 28/28、`pass^4=1.00` 后，holdout v2 **仅**在独立评测智能体按
   `docs/testing/readonly-holdout-v2-protocol.md` 封存时运行；本会话实现/校准路径
   **不得自封** holdout。

## 不可突破的恢复边界

- 总 DeepSeek 费用硬上限 ¥20；自动执行上限 ¥18。
- `.env`、预算账本、私有案例、原始 artifact、本机路径和 provider request
  ID 不进入 Git 或公开构建产物。
- 公开演示只使用合成数据和离线已验证轨迹，不部署项目 DeepSeek Key。
- 语义裁判不能覆盖工具、权限、写入、状态或确认的确定性失败。
- 最终完成前必须再由一个全新、未参与实现的智能体做完整平行审查。
- **uncertain 预留按设计永久计入 committed；不得为重跑而篡改 live 账本。**
  换新账本仅在用户明示批准、旧账本只读归档的前提下进行。

## 当前工作区说明

代码基线 `56c5c6f`；live 账本已按用户批准重置为空规范路径。下一步是付费校准
49/49；结果与费用写入本文件同提交。
