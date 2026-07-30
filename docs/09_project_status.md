# 项目现役状态

最后核对：2026-07-30 01:09 UTC

本地分支：`main`

最近已提交检查点：`17f098a`（本轮 P1 修复前基线）

当前候选检查点：本文件所在的下一次干净 Git 提交；提交后以审查报告记录的
完整 `git rev-parse HEAD` 为准。

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

2026-07-30 对本文件所在候选工作树执行完整离线门：

```text
ruff: passed
mypy: 53 source files passed
schema freshness: passed
pytest: 586 passed
branch coverage: 83.37%
Reference Eval: 8/8
```

本轮未联网，因此执行了 lint、mypy、Schema freshness、完整 pytest/branch
coverage 和 Reference Eval；依赖声明未变化，最近一次 `pip-audit` 仍为无已知
运行时漏洞，但尚未在当前候选提交上联网刷新。测试仍有一条非阻断
警告：FastAPI/Starlette 的旧
`TestClient` 兼容入口提示未来迁移到 `httpx2`；当前测试行为未受影响。
本轮未调用 DeepSeek，新增费用为 0。

## 当前唯一执行顺序

1. 把当前候选落为干净提交，然后对同一个完整 SHA 完成三路全新
   `phase2_fresh_adversarial_reaudit`：预算/结果/隐私、runtime/capability/
   source/harness、语义校准/原始证据重算。
2. 修复复审发现的所有 P0/P1 和影响交付合同的 P2，再运行完整离线门。
3. 当前 canonical 价格快照有效至 `2026-07-30T08:58:58Z`。仅在执行时仍
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

Preparation Agent 已保存为本地检查点。本轮在 `17f098a` 之上补齐了四组
P1：真实账本校准证明、正式 runtime capability 对象绑定、原始证据确定性
重评分，以及逐调用 attempt/错误阶段/预算结果闭环；并把 provider request
ID 从所有持久化和公开 artifact 中清除。完整离线门通过，但这些改动尚未在
一个干净提交上取得三路全新同提交审查 GO，因此不能视为 Phase 2 验收完成。

此前 `40289d9` 关闭了 formal context 与 28/28 回归前置门；
`18d31bb` 关闭固定输出根 symlink、严格回执、私有目录链和公开校验器问题，
独立聚焦复审为 GO；`0c55845` 又把批准的 Eval profile 固定为
`30 / 1024 / 2 / 4 / 12`，扩展运行依赖身份，并要求 source-tree 三次快照
一致；`aee4a3d` 进一步约束价格时间线，并使正式完成/失败路径都独立重算
完整 runtime identity。此前聚焦复核不计入本轮 fresh Gate。下一步必须由
未参与实现的三路全新审查者在本文件所在同一干净提交上重新签发 Gate。
复核现场保留，未删除缓存、数据库或私有 artifact。
