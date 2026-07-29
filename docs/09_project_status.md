# 项目现役状态

最后核对：2026-07-29 21:19 UTC

本地分支：`main`

最近实现检查点：`40289d9`（formal context and regression gate）

下一轮审查基线：本文件所在的干净 Git 提交；以审查报告记录的完整
`git rev-parse HEAD` 为准。

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
| 原子命题语义门 | changed-and-verified / pending final fresh re-audit | 49 条固定夹具已有逐 claim 人工证据区域和矛盾双侧标注；单字、子串、删否定、跨 claim 和单侧证据失败关闭 |
| 正式 Eval 证据链 | changed-and-verified / pending final fresh re-audit | formal 程序化入口必须消费排他 start receipt 后签发的一次性 context；v2 声明、start、完成/失败 evidence、terminal 与 public verifier 全链绑定当前 28/28 回归 bundle |
| 非正式付费入口 | changed-and-verified / pending final fresh re-audit | `diagnostic` 固定 10×1，`dev_repeat` 固定 7×4；程序化入口在模型调用前校验，完成证据逐 trial 对齐模型、usage、retry/error 和预算桶 |
| DeepSeek 语义校准 | pending | 尚未调用，新增费用为 0 |
| 公开回归与 holdout v2 | pending | 必须等待语义校准和独立审查通过 |
| 宿主确认、并发、UI、GitHub、公开演示 | pending | 尚未实现或发布 |
| 生产运行态 | not-applicable | 没有远端、部署或公开 URL |
| Agent 记忆 | generated-read-only | 本次未获授权写入，也未修改 |

## 最近验证

2026-07-29 在 `9918925` 实现上执行：

```text
ruff: passed
mypy: 52 source files passed
schema freshness: passed
pytest: 395 passed
branch coverage: 82.56%
pip-audit: no known runtime vulnerabilities
Reference Eval: 8/8
```

完整 `make verify PYTHON=.venv/bin/python` 已通过。测试仍有一条非阻断
警告：FastAPI/Starlette 的旧
`TestClient` 兼容入口提示未来迁移到 `httpx2`；当前测试行为未受影响。
本轮未调用 DeepSeek，新增费用为 0。

## 回家后唯一恢复顺序

1. 先查看 `git status` 和本文件所在提交，不要重跑 holdout v1。
2. 对同一个干净提交完成三路全新 `phase2_fresh_adversarial_reaudit`：语义绕过、
   canonical 价格单次冻结、校准标签、干净源码门、逐 attempt 费用、summary
   重算、diagnostic retry/error 预算、失败预算、一次性 formal context、公开
   回归前置门、唯一运行锁、完整回执链和正式输出隐私。
3. 修复复审发现的所有 P0/P1 和影响交付合同的 P2，再运行完整离线门。
4. 仅在价格快照仍有效、预算账本无未知预留且审查 Gate GO 后，安全加载
   `.env` 并运行公开语义校准。不得打印环境变量。
5. 校准门为固定 49 条夹具 `49/49`，并要求严格 validator 重算通过、预算
   完全结清以及按确定性分层规则抽取 5 条的程序性独立 GO 复核回执。通过后
   再运行七条公开回归 `7 cases × 4 trials`。
6. 公开回归达到 28/28、`pass^4=1.00`、全部安全断言通过且状态变化为 0 后，
   才由独立评测方封存全新 holdout v2，并只正式运行一次。
7. Phase 2 完成后继续宿主确认、确定性执行与并发、零密钥 UI、GitHub 和
   匿名公开演示。

## 不可突破的恢复边界

- 总 DeepSeek 费用硬上限 ¥20；自动执行上限 ¥18。
- `.env`、预算账本、私有案例、原始 artifact、本机路径和 provider request
  ID 不进入 Git 或公开构建产物。
- 公开演示只使用合成数据和离线已验证轨迹，不部署项目 DeepSeek Key。
- 语义裁判不能覆盖工具、权限、写入、状态或确认的确定性失败。
- 最终完成前必须再由一个全新、未参与实现的智能体做完整平行审查。

## 当前工作区说明

Preparation Agent 与 Phase 2 对抗修复都已保存为本地检查点，但 Phase 2
还没有同一提交上的三路 fresh re-audit GO、真实校准或公开回归结果，不能
视为验收完成。此前审查除价格冻结、summary、failed-attempt、receipt、
权限、路径和语义绕过外，又复现了最坏费用预留不足、完成态 attempt bucket
可伪造、非正式案例 allowlist 可被程序化绕过、模型调用可跨 trial 搬移，
diagnostic retry/error 费用未绑定、formal 可编程入口锁前调用模型，以及 v2
未绑定 28/28 回归 evidence。`40289d9` 及其前置 RED/GREEN 检查点已逐项关闭
这些问题；下一步必须由未参与实现的全新审查者在同一干净提交上重新签发
Gate。复核现场保留，未删除缓存、数据库或私有 artifact。
