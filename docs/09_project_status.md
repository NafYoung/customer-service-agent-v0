# 项目现役状态

最后核对：2026-09-03

本地分支：`codex/commerce-agent-fencing`

已验证实现检查点：`b23ec77`

当前候选检查点：本文件所在的干净 Git 提交；以实际
`git rev-parse HEAD` 为准。

本文是项目恢复和完成度判断的现役入口。阶段验收合同仍以
`docs/06_portfolio_completion_plan.md` 为准；历史结果保留在对应
`docs/testing/` 报告中。

## 当前结论

| 事实面 | 状态 | 证据与边界 |
|---|---|---|
| 确定性后端与只读 Agent | verified-current | 完整离线门和 Reference Eval 要求保留 |
| 模型可见工具数据边界 | changed-and-verified-offline | 第三方文本固定 fencing、服务端来源信封、原始 trace 与模型副本分离；完整离线门与对抗测试已通过 |
| Preparation Agent | verified-current | 单 Agent，精确 9 工具白名单，最多生成一个 Approval，不获得认证、展示、确认或执行权限 |
| 受信宿主流 | changed-and-verified-offline | `/v1/host/messages` 串联 Agent；服务端 canonical card、空 body 按钮确认和确定性幂等执行已有端到端测试 |
| 人工接管 | changed-and-verified-offline | manual mode 持久化；接管会取消同会话未完成 Approval，之后的消息、prepare、present、confirm 和 execute 全部失败关闭 |
| 可选 DeepSeek live 运行时 | changed-and-verified-offline | 默认关闭；显式开启时固定官方端点/模型、审查过的 v2 价格文件和共享持久预算账本 |
| 预算闸门 | changed-and-verified-offline | Host 与 Eval 默认使用同一绝对路径账本；环境变量不能另开 Host 额度；下一次最坏预留超限、价格过期或 SQLite 异常都在 HTTP 前失败关闭 |
| 当前付费状态 | not-observed-this-turn | 本轮未读 `.env` 或真实私有账本，未调用 DeepSeek；因此不宣称当前累计费用或真实 provider 连通性 |
| 业务 UI、真实集成与高并发 | pending | 尚无 Web 聊天界面、真实客户/订单/ERP/物流/支付数据，SQLite 不证明 PostgreSQL 并发安全 |
| 生产运行态 | not-applicable | 没有远程部署或公开 URL，不宣称生产就绪 |

## 本轮新增能力

1. 模型可见的服务端工具结果使用固定 `<rivet_tool_data>` 边界与来源信封；
   第三方文本中的伪造角色、工具、特殊令牌和 fence 逃逸标记在回传模型前清洗，
   原始脱敏 trace 仍供离线取证。
2. 受信宿主端点只从认证会话和宿主 header 注入客户、会话与确认权限，
   不信任模型或浏览器提交的 preview/hash/event id。
3. 取消、退货和换货的低风险合成路径可以走完
   `message -> prepare -> canonical present -> trusted confirm -> execute`。
4. 破损、瑕疵、错发和客户主动要求人工都进入持久 manual mode；模型自由文本
   不能创建转交工单。
5. live 入口固定 `deepseek-v4-flash`、官方 HTTPS 端点、温度 0 和当前审查过的
   v2 价格文件。其预算按官方峰时 USD 费率与 `10 CNY/USD` 保守换算计算；
   这是内部上界，不是供应商最终账单。
6. 当前价格政策有效至 `2026-09-07T17:40:23Z`；过期后必须重新从官方来源
   核对、冻结新文件并重跑受影响的审查，不得只延长日期。

## 最近验证

当前工作树已执行完整离线门和 Reference Eval：

```text
ruff: passed
mypy: 58 source files passed
schema freshness: passed
pytest: 633 passed
branch coverage: 83.46%
pip-audit: no known vulnerabilities
Reference Eval: 8/8
```

本轮 fencing 聚焦门覆盖 34 个单元、Agent 循环和对抗测试，全部通过；完整门
同时覆盖既有 Host、价格窗口、状态机和评测合同。验证仅使用离线 scripted
model 和 `httpx.MockTransport`，未发起真实模型请求。

当前仅有一条已知非阻断警告：Starlette `TestClient` 的旧 `httpx` 兼容入口提示
未来迁移到 `httpx2`。

## 独立复核与剩余风险

两路未参与主实现的最终复核分别检查了 live 预算/人工接管生命周期，
以及文档与能力声明。已发现的 P1 “账本分叉”、“转人工后旧卡仍可执行”
和“新运行时文件未跟踪”已修复；最终安全复核没有剩余 P0/P1。当前不得隐藏的 P2：

- v2 Host 预算 snapshot 尚无对应的公开 Eval `BudgetSummary` v2 schema；Host 当前不调用
  该公开导出，但后续复用证据通道前必须补齐。
- manual mode 能阻止接管提交之后的新入口，但 SQLite 首版没有承诺人工按钮能
  抢占一个已越过 guard 的并发执行请求。
- 还没有真实 DeepSeek smoke test、真实业务影子流量或人工工时指标；不能由离线
  测试推导“大多数时间不需人工”。

## 当前唯一执行顺序

1. 保持已验证实现提交为内测基线；任何生产代码变更都必须重跑完整门和受影响的
   独立复核。
2. 只有当价格政策仍有效、共享账本无未知预留，且操作者明确开启 live 开关后，
   才能做一次最小真实 smoke test；不打印环境变量或账本私有内容。
3. 先跑合成员工内测，再跑只读影子流量，最后才对小比例低风险真实请求开放
   prepare/confirm。任一安全写入违规都立即回退到 manual/read-only。
4. 用可观测 KPI 判断是否扩容：无人为介入完成率、误执行/越权写入数、人工接管率、
   重复咨询率、P50/P95 处理时长和人工复核工时。
5. 不重跑 retired holdout v1。语义校准、公开回归和全新 holdout v2 仍按
   `docs/06_portfolio_completion_plan.md` 的一次性协议执行。

## 不可突破的恢复边界

- `.env`、API Key、宿主令牌、私有账本、私有 Eval artifact 和 provider request ID
  不进入 Git、日志或回复。
- 本项目共享持久账本的内部人民币硬上限为 `¥20`，自动运行准入上限为
  `¥18`；它不是供应商账单保证，也无法限制其他程序使用同一 Key。
- 公开演示不携带 DeepSeek Key，不产生模型网络出口。
- 语义裁判不能覆盖工具、权限、写入、状态、确认或人工接管的确定性失败。
- 当前可宣称的上限是“可供合成和受控内测的候选版”，不是生产系统或已证明能
  代替人工的自治客服。
