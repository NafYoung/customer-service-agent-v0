# 项目现役状态

最后核对：2026-07-30 19:48 UTC

本地分支：`main`

最近已提交检查点：与本文件同 Git 提交（DeepSeek 价格快照刷新与预算时钟绑定）

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
| 原子命题语义门 | changed-and-verified-offline / pending fresh same-commit audit | 49 条固定夹具已有逐 claim 人工证据区域和矛盾双侧标注；校准 validator 必须从固定私有账本逐调用核对 49 个唯一哈希、用量、费用、模式和时间，不能再由合成调用摘要自证 |
| 正式 Eval 证据链 | changed-and-verified-offline / pending fresh same-commit audit | 一次性 formal capability 绑定 Settings、模型、裁判、守卫、冻结快照和完整 harness；调用前重冻并拒绝对象/方法替换；7×4 与 holdout 前置校验从原始回答、轨迹、状态、写入和 verdict 确定性重算，不信任自报分数 |
| 非正式付费入口 | changed-and-verified-offline / pending fresh same-commit audit | `diagnostic` 固定 10×1，`dev_repeat` 固定 7×4；付费调用证据按哈希、attempt 数、错误阶段、时间和费用与只读账本逐一闭环；所有公开 artifact 将 provider request ID 固定为 `null` |
| DeepSeek 语义校准 | pending | 尚未调用，新增费用为 0 |
| 公开回归与 holdout v2 | pending | 必须等待语义校准和独立审查通过 |
| 宿主确认、并发、UI、GitHub、公开演示 | pending | 尚未实现或发布 |
| 生产运行态 | not-applicable | 没有远端、部署或公开 URL |
| Agent 记忆 | generated-read-only | 本次未获授权写入，也未修改 |

## 最近验证

2026-07-30 对本文件所在提交执行完整离线门与 Reference Eval：

```text
ruff: passed
mypy: 53 source files passed
schema freshness: passed
pytest: 586 passed
branch coverage: 83.39%
pip-audit: no known vulnerabilities
Reference Eval: 8/8
```

定价快照已按 DeepSeek 官方文档重新核对并刷新（费率未变：缓存命中 ¥0.02/M、
输入 ¥1/M、输出 ¥2/M）；canonical 文件为
`pricing/deepseek-v4-flash-2026-07-30.json`，`valid_until`
为 `2026-08-06T17:20:00Z`。预算账本与 budget guard 共用注入时钟，使
`run identity` 的 `started_at` 在离线付费证据测试中保持一致。

本轮未调用 DeepSeek，新增费用为 0。测试仍有一条非阻断警告：FastAPI/Starlette
的旧 `TestClient` 兼容入口提示未来迁移到 `httpx2`；当前测试行为未受影响。

## 当前唯一执行顺序

1. 对本提交同一完整 SHA 完成三路全新
   `phase2_fresh_adversarial_reaudit`：预算/结果/隐私、runtime/capability/
   source/harness、语义校准/原始证据重算。
2. 修复复审发现的所有 P0/P1 和影响交付合同的 P2，再运行完整离线门。
3. 当前 canonical 价格快照有效至 `2026-08-06T17:20:00Z`。仅在执行时仍
   有效、预算账本无未知预留且三路审查全部 Gate GO 后，才安全加载
   `.env` 并运行公开语义校准。不得打印环境变量。
   若价格快照已过期，必须先从当前官方来源刷新并形成新的干净提交，再重做
   受价格身份影响的同提交审查。
4. 校准门为固定 49 条夹具 `49/49`，并要求严格 validator 重算通过、预算
   完全结清以及按确定性分层规则抽取 5 条的程序性独立 GO 复核回执。通过后
   再运行七条公开回归 `7 cases × 4 trials`。
5. 公开回归达到 28/28、`pass^4=1.00`、全部安全断言通过且状态变化为 0 后，
   才由独立评测方封存全新 holdout v2，并只正式运行一次。
6. Phase 2 完成后继续宿主确认、确定性执行与并发、零密钥 UI、GitHub 和
   匿名公开演示。

## 不可突破的恢复边界

- 总 DeepSeek 费用硬上限 ¥20；自动执行上限 ¥18。
- `.env`、预算账本、私有案例、原始 artifact、本机路径和 provider request
  ID 不进入 Git 或公开构建产物。
- 公开演示只使用合成数据和离线已验证轨迹，不部署项目 DeepSeek Key。
- 语义裁判不能覆盖工具、权限、写入、状态或确认的确定性失败。
- 最终完成前必须再由一个全新、未参与实现的智能体做完整平行审查。

## 当前工作区说明

本提交是 Phase 2 恢复在 `17f098a` 之上补齐 P1 与价格刷新后的干净候选，
可直接作为三路全新 `phase2_fresh_adversarial_reaudit` 的审查基线。改动包括：
真实账本校准证明、正式 runtime capability 对象绑定、原始证据确定性重评分、
逐调用 attempt/错误阶段/预算结果闭环、清除持久化与公开 artifact 中的 provider
request ID，以及 DeepSeek 价格快照续期与 guard/ledger 共享时钟。完整离线门与
Reference Eval 已通过，但尚未取得同提交三路审查 GO，不能视为 Phase 2 验收完成。

此前 `40289d9` 关闭了 formal context 与 28/28 回归前置门；`18d31bb` 关闭固定
输出根 symlink、严格回执、私有目录链和公开校验器问题，独立聚焦复审为 GO；
`0c55845` 又把批准的 Eval profile 固定为 `30 / 1024 / 2 / 4 / 12`，扩展运行
依赖身份，并要求 source-tree 三次快照一致；`aee4a3d` 进一步约束价格时间线，
并使正式完成/失败路径都独立重算完整 runtime identity。此前聚焦复核不计入本轮
fresh Gate。复核现场保留，未删除缓存、数据库或私有 artifact。
