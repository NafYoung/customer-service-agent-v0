# 项目现役状态

最后核对：2026-07-30 23:45 UTC

本地分支：`main`

最近已提交检查点：与本文件同 Git 提交（Phase 2 P1 闭合：send-path 封印 +
formal failure 账本绑定 + 校准 `provider_request_id` 脱敏）

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
| 原子命题语义门 | changed-and-verified-offline / pending fresh same-commit audit | 校准 `_call_evidence` 强制 `provider_request_id`/`response_id` null；响应摘要账本锚定仍在 |
| 正式 Eval 证据链 | changed-and-verified-offline / pending fresh same-commit audit | client/transport/mounts + send/post/request 封印；formal failure 与 formal v1 付费包回读 live ledger |
| 非正式付费入口 | changed-and-verified-offline / pending fresh same-commit audit | `persistent_sqlite` 包校验回读固定私有账本；导出证据 correlator 强制 null |
| DeepSeek 语义校准 | pending | 尚未调用，新增费用为 0 |
| 公开回归与 holdout v2 | pending | 必须等待语义校准和独立审查通过 |
| 宿主确认、并发、UI、GitHub、公开演示 | pending | 尚未实现或发布 |
| 生产运行态 | not-applicable | 没有远端、部署或公开 URL |
| Agent 记忆 | generated-read-only | 本次未获授权写入，也未修改 |

## 最近验证

2026-07-30 对本文件所在提交执行完整离线门与 Reference Eval：

```text
ruff: passed
mypy: 54 source files passed
schema freshness: passed
pytest: 607 passed
branch coverage: 82.62%
pip-audit: no known vulnerabilities
Reference Eval: 8/8
```

对基线 `ac6ccd8` 的三路全新 `phase2_fresh_adversarial_reaudit` 结论为：

| 轨 | 结论 | 报告 |
|---|---|---|
| 预算 / 结果 / 隐私 | **NO-GO** | `docs/testing/phase2-fresh-reaudit-budget-privacy-ac6ccd8.md` |
| runtime / capability / harness | **NO-GO** | `docs/testing/phase2-fresh-reaudit-runtime-harness-ac6ccd8.md` |
| 语义校准 / 原始证据 | **GO** | `docs/testing/phase2-fresh-reaudit-semantic-evidence-ac6ccd8.md` |

本提交闭合 ac6ccd8 两路 NO-GO 的 P1（send-path 封印、formal failure/v1 账本绑定、校准 correlator 写盘脱敏）。**必须**在本提交新 SHA 上重做同结构三路 fresh reaudit；不得继承 ac6ccd8 的 GO/NO-GO。

本轮未调用 DeepSeek，新增费用为 0。

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

本提交在 `ac6ccd8` 之上闭合 Phase 2 复审残留 P1，主要包括：

- **Runtime / capability：** 除 `sealed_httpx_client_id` 外，封印
  `sealed_httpx_transport_id` 与 `sealed_httpx_mounts`；拒绝实例级
  `Client.send` / `.post` / `.request` 遮蔽；对抗覆盖兄弟 `HTTPTransport`
  替换与 `_mounts` MockTransport 注入。类级 `HTTPTransport.handle_request`
  补丁仍为文档化残余。
- **预算：** formal failure 与退役 formal v1 的 `persistent_sqlite` 包校验回读
  live ledger（允许 `active` / `completed` 可绑定状态）。
- **隐私：** 校准 `_call_evidence` 成功路径不再回填 `provider_request_id`。

完整离线门通过后仍须取得**本提交**三路审查 GO，不能视为 Phase 2 验收完成；
付费 DeepSeek 校准仍 pending。
