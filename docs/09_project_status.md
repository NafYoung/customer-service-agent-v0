# 项目现役状态

最后核对：2026-07-31（离线修复提交；与本文件同 Git 提交）

本地分支：`main`

最近已提交检查点：与本文件同 Git 提交（离线语义裁判 prompt / 传输×预算
修复；付费校准仍因 live 账本头寸不足而阻塞）

Preparation Agent 检查点：`1b034cd`

本文是项目恢复工作的现役入口。阶段验收标准仍以
`docs/06_portfolio_completion_plan.md` 为准；历史结果保留在对应
`docs/testing/` 报告中。

## 当前结论

| 事实面 | 状态 | 证据 |
|---|---|---|
| 确定性后端与只读 Agent | verified-current | 完整离线门通过；Reference Eval 8/8 |
| Eval 证据与预算闸门 | verified-current | 开发集 40/40；账本仍在 |
| holdout v1 | verified-current / retired | 唯一正式结果 46/80、`pass^4=0.35`；禁止重跑 |
| Preparation Agent | changed-and-verified | 提交 `1b034cd`；独立审查 Gate GO |
| 原子命题语义门 | changed-and-verified | `0077b1f` 三路 fresh reaudit ALL GO；本轮离线 prompt v2 |
| 正式 Eval 证据链 | changed-and-verified | `0077b1f` runtime/budget 轨 Gate GO |
| 非正式付费入口 | changed-and-verified | `0077b1f` 预算/隐私轨 Gate GO |
| DeepSeek 语义校准 | **failed / blocked** | `e2cd096`/`c6791c8` 付费跑 16/49；¥18 自动执行头寸耗尽；离线修复已落地，**不得**再付费重跑直至头寸策略经明示批准 |
| 公开回归与 holdout v2 | blocked | 校准未过；付费头寸不足 |
| 宿主确认、并发、UI、GitHub、公开演示 | pending | 尚未实现或发布 |
| 生产运行态 | not-applicable | 没有远端、部署或公开 URL |
| Agent 记忆 | generated-read-only | 本次未获授权写入，也未修改 |

## 付费语义校准失败（2026-07-31）

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

按夹具 `kind`（不展开正文）归类，失败高度集中：

- `contradiction`：**0/7**（全部失败）
- `unsafe_prompt_injection`：**0/7**（全部失败）
- `safe_prompt_injection`：仅 2/7 通过（假阴性偏多）
- `generic` / `negation_flip`：多数失败；`safe_canonical` / `safe_paraphrase` 部分失败

结论：**校准门未过，主因是语义裁判准确率不足**，不是单纯「没跑完」。

### 协议 / 预算副作用（次因，但锁死头寸）

同一次 run 账本摘要（live SQLite，非密钥）：

| 范围 | attempt | settled_exact | uncertain | committed_cny | settled_cny | remaining_execution |
|---|---|---|---|---|---|---|
| run | 57 | 40 | **17** | ≈17.0667 | ≈0.0319 | ≈0.8059 |
| cumulative | 310 | 293 | **17** | ≈17.1941 | ≈0.1592 | ≈0.8059 |

- 17 笔 `uncertain` 的 `error_code` **全部**为 `MODEL_TRANSPORT_ERROR`；`known_cost` 为空，按预留
  `reservation_cny_per_attempt=1.002048` 计入 committed。
- 约 6 个 logical call × 规范 `DEEPSEEK_MAX_RETRIES=2`（最多 3 次尝试）放大为 17 笔
  uncertain，单次传输故障即可永久锁住约 ¥1–3 预留。
- 代码路径：`uncertain` **不能**再 `settle`；只有 `reserved → settled_*` 或
  `reserved → uncertain`。uncertain / reserved 计入 committed（见
  `DeepSeekBudgetLedger._committed_units`）。
- 当前 `remaining_execution_cny ≈ 0.81 < 1.002048`，**连下一次预留都开不出**。
  在 ¥18 自动执行上限下，本机账本等效耗尽。
- 硬上限 ¥20 理论余量 ≈2.81，仍不够覆盖「再来一轮 + 最坏 uncertain 预留」；且自动执行合同仍是 ¥18。
- **不得**为腾预算手工删改 / 二次 restore live 账本后立刻付费重跑；上一次
  `failed-calib-20260730` 已证明 restore 只会把同一传输风险再锁死一遍。

累计已结算费用约 ¥0.159（历史 ~¥0.127 + 本跑 settled ≈¥0.032）。真正锁死自动执行头寸的是
≈¥17.03 的 uncertain 预留，不是 settled 小额。

### 与 `failed-calib-20260730` 的关系

路径：`artifacts/private/phase2-reaudit/failed-calib-20260730/`（另有
`deepseek-budget.pre-restore-20260730T193513Z.sqlite3` 与
`68468a1-three-track-goa.md`）。

- 旧跑 SHA `68468a1`；`run_id=eval-20260730t193216z-…`；**49/49**
  `SEMANTIC_JUDGE_MODEL_ERROR`，passed=0——纯协议/模型错误，未测到裁判质量。
- 当时 17 笔 uncertain 锁死预算后，运维用 pre-restore 备份恢复了 live 账本
  （旧 run 已不在 live 中）。
- **本轮 live 上的 17 笔 uncertain 是 `e42de5ec` 新产生的**，不是旧跑残留。

## 离线修复（本提交；未调用 DeepSeek）

已落地、可用离线证据验证：

1. **语义裁判合同清晰化（`atomic-claims-v2`）**
   - 更新 `evals/semantic_judge_prompt.md`：固定评估顺序；强化
     evaluator-manipulation（发出指令=`entailed`，明确拒绝=`contradicted`，
     不得因后半段正确答复改成 `not_mentioned`）；强化
     `both_or_ambiguous` + `material_self_contradiction` 决策规则。
   - 全局操纵命题补充「仅否认不等于发出指令」。
   - 离线：49 条 label oracle 可复现 21/28 gate；contradiction /
     prompt-injection 夹具表面与标签一致；prompt 合同单测覆盖硬 kind。
   - **不**声称 DeepSeek 实跑准确率已恢复；付费 49/49 仍未重跑。

2. **传输×预算：付费路径传输失败快速失败关闭**
   - `OpenAICompatibleChatClient`：存在 `budget_guard` 时，
     `httpx.RequestError` 在首次预留并 `uncertain` 后立即抛出，不再按
     `max_retries` 追加预留。
   - 无预算客户端仍可重试传输错误；HTTP 429/5xx 付费重试行为不变。
   - 单测：付费路径 1 次 ConnectError → `attempt_count=1`、
     `committed=1.002048`；未付费路径仍可 3 次尝试后成功。
   - **不**回滚或改写 live 账本中已有 17 笔 uncertain；历史锁定仍在。

### 付费重跑仍阻塞的原因

- live `remaining_execution_cny ≈ 0.81 < reservation_cny_per_attempt`；
- ¥18 自动执行合同下无法再 `reserve`；
- uncertain 按设计永久计入 committed；不得静默 wipe / restore live 账本
  「造」头寸。

### 用户可明示批准的选项（任选其一；默认不做）

1. **新 ledger 路径**（仅当项目合同允许、且审计上明确这是新执行身份，
   不是覆盖旧 uncertain）：指向独立 SQLite，保留旧账本只读归档。
2. **显式预算重置**：用户书面批准后，用受控运维步骤归档旧 live 并换新
   空账本；仍受 ¥20 / ¥18 约束；不得把旧 uncertain 当作「未发生」。
3. **等待 / 缩小范围**：在现有头寸下不做 49 条付费校准；离线门与
   Reference Eval 可继续。

在未获上述明示批准前：**禁止**再次付费校准或任何 DeepSeek 出口。

## 最近验证

对基线 `0077b1f57ef2d5eb7155a92683041ed9e76fb38e` 的三路全新
`phase2_fresh_adversarial_reaudit` 仍为 ALL GO（见既有
`docs/testing/phase2-fresh-reaudit-*-0077b1f.md`）。三路 GO **不**因本次校准失败而撤销。

本轮离线修复未调用 DeepSeek；新增费用为 0。完整离线门见同提交
`make verify` 证据。

## 当前唯一执行顺序

1. ~~三路全新 `phase2_fresh_adversarial_reaudit`。~~ **已完成：`0077b1f` ALL GO。**
2. ~~付费公开语义校准 49/49。~~ **已尝试，失败（16/49）。禁止在未修复前重跑。**
3. ~~离线裁判修复与传输×预算降耗。~~ **本提交已完成离线部分；付费头寸仍阻塞。**
4. 仅当同时满足下列条件才可考虑再次付费校准：
   - 离线修复有具体证据（本提交）；
   - 本机 execution 头寸能覆盖「串行 49 次预留」且对 uncertain 最坏情况有余量
     （当前 **不满足**，除非用户明示批准新 ledger / 重置）；
   - 不得静默 raise execution limit 或 restore 账本来「造」头寸。
5. 校准 49/49 + validator 重算 + 预算结清 + 5 条程序性独立 GO 复核后，才跑七条公开回归。
6. 公开回归 28/28 后才封存 holdout v2（只跑一次）。

## 不可突破的恢复边界

- 总 DeepSeek 费用硬上限 ¥20；自动执行上限 ¥18。
- `.env`、预算账本、私有案例、原始 artifact、本机路径和 provider request
  ID 不进入 Git 或公开构建产物。
- 公开演示只使用合成数据和离线已验证轨迹，不部署项目 DeepSeek Key。
- 语义裁判不能覆盖工具、权限、写入、状态或确认的确定性失败。
- 最终完成前必须再由一个全新、未参与实现的智能体做完整平行审查。
- **uncertain 预留按设计永久计入 committed；不得为重跑而篡改 live 账本。**

## 当前工作区说明

代码基线含本提交离线修复；付费语义校准仍失败关闭且头寸不足。下一步是
**用户对账本策略的明示决定**，不是再次静默付费。
