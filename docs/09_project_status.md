# 项目现役状态

最后核对：2026-07-30 21:00 UTC

本地分支：`main`

最近已提交检查点：与本文件同 Git 提交（Phase 2 P0/P1 合入：runtime 封印 +
校准/付费证据绑定 + `response_id` 脱敏）

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
| 原子命题语义门 | changed-and-verified-offline / pending fresh same-commit audit | 本提交关闭 a10facb 三路复审 NO-GO 中的校准响应摘要账本锚定、review 机器重放、失败校准 untrusted 落盘 |
| 正式 Eval 证据链 | changed-and-verified-offline / pending fresh same-commit audit | live httpx/ledger/price 对象封印；source-tree 拒绝目录 symlink 跟随；付费 endpoint 拒绝 `/v1` |
| 非正式付费入口 | changed-and-verified-offline / pending fresh same-commit audit | `persistent_sqlite` 包校验回读固定私有账本；`response_id` 与 `provider_request_id` 一并强制 null |
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
pytest: 598 passed
branch coverage: 82.77%
pip-audit: no known vulnerabilities
Reference Eval: 8/8
```

对基线 `a10facb` 的三路全新 `phase2_fresh_adversarial_reaudit` 结论均为 **NO-GO**
（报告已入库）：

- `docs/testing/phase2-fresh-reaudit-budget-privacy-a10facb.md`
- `docs/testing/phase2-fresh-reaudit-runtime-harness-a10facb.md`
- `docs/testing/phase2-fresh-reaudit-semantic-evidence-a10facb.md`

本提交合入修复后，**必须**在新 SHA 上重做同结构三路 fresh reaudit；修复本身不能继承 a10facb 审查 GO。

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

本提交在 `a10facb` 之上合入 Phase 2 复审 NO-GO 修复，主要包括：

- **Runtime / capability：** live `httpx.Client` 与 budget ledger/price 对象封印；
  `transport_mode` 由 live transport 推导；source-tree fingerprint 不跟随目录
  symlink；付费 endpoint path 收紧（拒绝 `/v1`）。
- **语义校准：** `response_content_sha256` 写入 model_call 与 settled ledger
  attempt；attestation 对账本摘要而非仅报告内自洽；review 机器重放抽样夹具；
  失败校准 untrusted 落盘。
- **预算 / 隐私：** `persistent_sqlite` 付费包校验回读固定私有账本；导出证据
  强制 `response_id`/`provider_request_id` 为 null。

完整离线门已通过，但尚未取得**本提交**三路审查 GO，不能视为 Phase 2 验收完成；
付费 DeepSeek 校准仍 pending。
