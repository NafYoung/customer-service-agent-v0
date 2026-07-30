# 项目现役状态

最后核对：2026-07-30 23:48 UTC

本地分支：`main`

最近已提交检查点：与本文件同 Git 提交（记录 `0077b1f` 三路 fresh
reaudit ALL GO，下一步付费语义校准）

Preparation Agent 检查点：`1b034cd`

本文是项目恢复工作的现役入口。阶段验收标准仍以
`docs/06_portfolio_completion_plan.md` 为准；历史结果保留在对应
`docs/testing/` 报告中。

## 当前结论

| 事实面 | 状态 | 证据 |
|---|---|---|
| 确定性后端与只读 Agent | verified-current | 完整离线门通过；Reference Eval 8/8 |
| Eval 证据与预算闸门 | verified-current | 开发集 40/40；累计已结算费用 ¥0.12738404 |
| holdout v1 | verified-current / retired | 唯一正式结果 46/80、`pass^4=0.35`；禁止重跑 |
| Preparation Agent | changed-and-verified | 提交 `1b034cd`；独立审查 Gate GO |
| 原子命题语义门 | changed-and-verified | `0077b1f` 三路 fresh reaudit ALL GO |
| 正式 Eval 证据链 | changed-and-verified | `0077b1f` runtime/budget 轨 Gate GO |
| 非正式付费入口 | changed-and-verified | `0077b1f` 预算/隐私轨 Gate GO |
| DeepSeek 语义校准 | pending | 三路 GO 已齐；下一步付费校准（新增费用仍为 0） |
| 公开回归与 holdout v2 | pending | 必须等待语义校准和独立审查通过 |
| 宿主确认、并发、UI、GitHub、公开演示 | pending | 尚未实现或发布 |
| 生产运行态 | not-applicable | 没有远端、部署或公开 URL |
| Agent 记忆 | generated-read-only | 本次未获授权写入，也未修改 |

## 最近验证

对基线 `0077b1f57ef2d5eb7155a92683041ed9e76fb38e`（`fix: seal formal send
path and bind failure ledger evidence`）的三路全新
`phase2_fresh_adversarial_reaudit` 结论为：

| 轨 | 结论 | 报告 |
|---|---|---|
| 预算 / 结果 / 隐私 | **GO** | `docs/testing/phase2-fresh-reaudit-budget-privacy-0077b1f.md` |
| runtime / capability / harness | **GO** | `docs/testing/phase2-fresh-reaudit-runtime-harness-0077b1f.md` |
| 语义校准 / 原始证据 | **GO** | `docs/testing/phase2-fresh-reaudit-semantic-evidence-0077b1f.md` |

`ac6ccd8` 的 P1（send-path 封印、formal failure/v1 账本绑定、校准 correlator
写盘脱敏）在本 SHA 上已关闭；三路均 **Gate GO**。不得继承更早 SHA 的结论。

本轮文档提交未调用 DeepSeek；新增费用为 0。预检（校准前）：

- canonical 价格 `valid_until=2026-08-06T17:20:00Z`，仍有效
- `.env` 存在且含 `DEEPSEEK_API_KEY`（仅核验键名）
- 账本：`budget_runs` completed=2；`budget_attempts` settled_exact=253；无
  reserved/unknown 阻塞
- 累计已结算约 ¥0.12738404（远低于 ¥18 自动 / ¥20 硬上限）

## 当前唯一执行顺序

1. ~~对本提交同一完整 SHA 完成三路全新 `phase2_fresh_adversarial_reaudit`。~~
   **已完成：`0077b1f` 三路 ALL GO。**
2. 当前 canonical 价格快照有效至 `2026-08-06T17:20:00Z`。在仍有效、预算账本
   无未知预留的前提下，安全加载 `.env` 并运行公开语义校准。不得打印环境变量。
   若价格快照已过期，必须先从当前官方来源刷新并形成新的干净提交，再重做
   受价格身份影响的同提交审查。
3. 校准门为固定 49 条夹具 `49/49`，并要求严格 validator 重算通过、预算
   完全结清以及按确定性分层规则抽取 5 条的程序性独立 GO 复核回执。通过后
   再运行七条公开回归 `7 cases × 4 trials`。
4. 公开回归达到 28/28、`pass^4=1.00`、全部安全断言通过且状态变化为 0 后，
   才由独立评测方封存全新 holdout v2，并只正式运行一次。
5. Phase 2 完成后继续宿主确认、确定性执行与并发、零密钥 UI、GitHub 和
   匿名公开演示。

## 不可突破的恢复边界

- 总 DeepSeek 费用硬上限 ¥20；自动执行上限 ¥18。
- `.env`、预算账本、私有案例、原始 artifact、本机路径和 provider request
  ID 不进入 Git 或公开构建产物。
- 公开演示只使用合成数据和离线已验证轨迹，不部署项目 DeepSeek Key。
- 语义裁判不能覆盖工具、权限、写入、状态或确认的确定性失败。
- 最终完成前必须再由一个全新、未参与实现的智能体做完整平行审查。

## 当前工作区说明

代码基线仍为 `0077b1f`。本文件与三路 GO 报告一并记录 fresh reaudit 结果；
下一步为付费 DeepSeek 语义校准，不得跳过独立复核回执。
