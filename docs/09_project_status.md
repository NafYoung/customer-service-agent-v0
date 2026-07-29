# 项目现役状态

最后核对：2026-07-29 19:20 UTC

本地分支：`main`

当前实现检查点：`94eb7a8`（Phase 2 adversarial repair）

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
| 原子命题语义门 | changed-and-verified / pending re-audit | 49 条固定夹具已有逐 claim 人工证据区域和矛盾双侧标注；纯标点、跨 claim 和单侧证据失败关闭 |
| 正式 Eval 证据链 | changed-and-verified / pending re-audit | `94eb7a8` 绑定持久预算身份、完整 bundle 内容、固定私有路径及权限；失败正式运行形成独立 failed-attempt bundle 和 terminal 链，不能冒充成功结果 |
| DeepSeek 语义校准 | pending | 尚未调用，新增费用为 0 |
| 公开回归与 holdout v2 | pending | 必须等待语义校准和独立审查通过 |
| 宿主确认、并发、UI、GitHub、公开演示 | pending | 尚未实现或发布 |
| 生产运行态 | not-applicable | 没有远端、部署或公开 URL |
| Agent 记忆 | generated-read-only | 本次未获授权写入，也未修改 |

## 最近验证

2026-07-29 在 `94eb7a8` 实现上执行：

```text
ruff: passed
mypy: 49 source files passed
schema freshness: passed
pytest: 276 passed
branch coverage: 82.54%
pip-audit: no known runtime vulnerabilities
Reference Eval: 8/8
```

完整 `make verify PYTHON=.venv/bin/python` 已通过。测试仍有一条非阻断
警告：FastAPI/Starlette 的旧
`TestClient` 兼容入口提示未来迁移到 `httpx2`；当前测试行为未受影响。
本轮未调用 DeepSeek，新增费用为 0。

## 回家后唯一恢复顺序

1. 先查看 `git status` 和检查点 `94eb7a8`，不要重跑 holdout v1。
2. 对 `94eb7a8` 完成全新 `phase2_fresh_adversarial_reaudit`：语义绕过、校准标签、干净源码
   门、冻结 runtime、逐 attempt 价格、费用重算、唯一运行锁、完整回执链和
   正式输出隐私。
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
还没有复审 GO、真实校准或公开回归结果，不能视为验收完成。首轮三个并行
只读审查均返回 NO-GO；`94eb7a8` 已逐项关闭其预算低记、预算身份脱钩、
cases/trajectory 分叉、runtime 配置漏绑、回执链不完整、私有路径越界、
证据 span 语义不足及失败轨迹丢失问题。下一步必须由未参与这些修改的全新
审查者重新签发 Gate。复核现场保留，未删除缓存、数据库或私有 artifact。
