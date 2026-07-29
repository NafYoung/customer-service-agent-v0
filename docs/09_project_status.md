# 项目现役状态

最后核对：2026-07-29 11:38 UTC

本地分支：`main`

当前实现检查点：`ec71b53`（fail-closed semantic Eval controls）

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
| 原子命题语义门 | changed-and-verified / pending audit | 本地实现与 37 条校准夹具已通过离线测试并提交为 `ec71b53`；独立审查被本次收尾中断 |
| DeepSeek 语义校准 | pending | 尚未调用，新增费用为 0 |
| 公开回归与 holdout v2 | pending | 必须等待语义校准和独立审查通过 |
| 宿主确认、并发、UI、GitHub、公开演示 | pending | 尚未实现或发布 |
| 生产运行态 | not-applicable | 没有远端、部署或公开 URL |
| Agent 记忆 | generated-read-only | 本次未获授权写入，也未修改 |

## 最近验证

2026-07-29 在当前工作树执行：

```text
ruff: passed
mypy: 45 source files passed
generated contracts: fresh
pytest: 187 passed
branch coverage: 80.11% (gate 80%)
pip-audit: no known runtime vulnerabilities
Reference Eval: 8/8
```

测试仍有一条非阻断警告：FastAPI/Starlette 的旧 `TestClient` 兼容入口提示未来
迁移到 `httpx2`；当前测试行为未受影响。

## 回家后唯一恢复顺序

1. 先查看 `git status` 和检查点 `ec71b53`，不要重跑 holdout v1。
2. 新建一个未参与实现的审查智能体，完成
   `phase2_fresh_adversarial_audit`：语义绕过、校准标签、holdout split、
   唯一运行锁、runtime 指纹和正式输出隐私。
3. 修复所有 P0/P1 和影响交付合同的 P2，再运行完整离线门。
4. 仅在价格快照仍有效、预算账本无未知预留且审查 Gate GO 后，安全加载
   `.env` 并运行公开语义校准。不得打印环境变量。
5. 校准门为：所有预期失败夹具 100%，预期通过夹具至少 95%。通过后再运行
   七条公开回归 `7 cases × 4 trials`。
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

Preparation Agent 与 Phase 2 语义门都已保存为本地检查点，但 Phase 2 还没有
独立 Gate、真实校准或公开回归结果，不能视为验收完成。三个并行只读审查在
`/neat` 收尾时被主动停止，未产生可采用的最终 Gate。复核现场保留，未删除
缓存、数据库、私有 artifact 或任何工作树。
