# 项目现役状态

最后核对：2026-07-29 20:00 UTC

本地分支：`main`

当前实现检查点：`1305f94`（Phase 2 fresh-audit repair）

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
| 原子命题语义门 | changed-and-verified / pending fresh re-audit | 49 条固定夹具已有逐 claim 人工证据区域和矛盾双侧标注；单字、子串、删否定、跨 claim 和单侧证据失败关闭 |
| 正式 Eval 证据链 | changed-and-verified / pending fresh re-audit | `1305f94` 绑定 canonical 价格、record 重算 summary、owner-only 完整回执链及 persistent failed-attempt 预算；失败结果不能冒充成功 |
| DeepSeek 语义校准 | pending | 尚未调用，新增费用为 0 |
| 公开回归与 holdout v2 | pending | 必须等待语义校准和独立审查通过 |
| 宿主确认、并发、UI、GitHub、公开演示 | pending | 尚未实现或发布 |
| 生产运行态 | not-applicable | 没有远端、部署或公开 URL |
| Agent 记忆 | generated-read-only | 本次未获授权写入，也未修改 |

## 最近验证

2026-07-29 在 `1305f94` 实现上执行：

```text
ruff: passed
mypy: 50 source files passed
schema freshness: passed
pytest: 308 passed
branch coverage: 82.36%
pip-audit: no known runtime vulnerabilities
Reference Eval: 8/8
```

完整 `make verify PYTHON=.venv/bin/python` 已通过。测试仍有一条非阻断
警告：FastAPI/Starlette 的旧
`TestClient` 兼容入口提示未来迁移到 `httpx2`；当前测试行为未受影响。
本轮未调用 DeepSeek，新增费用为 0。

## 回家后唯一恢复顺序

1. 先查看 `git status` 和检查点 `1305f94`，不要重跑 holdout v1。
2. 对 `1305f94` 完成全新 `phase2_fresh_adversarial_reaudit`：语义绕过、
   canonical 价格单次冻结、校准标签、干净源码门、逐 attempt 费用、summary
   重算、失败预算、唯一运行锁、完整回执链和正式输出隐私。
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

Preparation Agent 与 Phase 2 两轮对抗修复都已保存为本地检查点，但 Phase 2
还没有 fresh re-audit GO、真实校准或公开回归结果，不能视为验收完成。
对 `18433e6` 的三组全新只读审查再次返回 NO-GO，发现 artifact 自报定价、
旧 80/80 summary、failed-attempt 低报预算、receipt/权限/路径披露和语义
子串绕过；集成复查随后又复现价格冻结与预算守卫二次读取间的 P0 换包窗口。
`1305f94` 已逐项关闭并新增 RED/GREEN 回归。下一步必须由未参与这些修改的
全新审查者重新签发 Gate。复核现场保留，未删除缓存、数据库或私有 artifact。
