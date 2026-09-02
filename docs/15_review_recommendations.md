# 修改意见（2026-09-02）

读者：项目作者。本文是现役收口意见，不替代
`docs/06_portfolio_completion_plan.md` 的验收合同，也不改写已退役 holdout
成绩。核对日与 `docs/09_project_status.md` 对齐。

## 判断

架构方向正确，不要改。卖点是「模型只能理解 / 查询 / prepare，写操作必须
`prepare → 宿主展示 → 按钮确认 → 确定性 execute`」，不是聊天覆盖率。

当前短板不在再加工具，而在三件事：

1. 对外文档互相打架，削弱「诚实证据」叙事（本轮已对齐现役入口）。
2. Phase 5 合同仍要求 PostgreSQL 并发证明，作品集却已按 Phase 6 公开发布。
3. 面试入口过重：`docs/` 约 50+ Markdown，面试官不会读完。

按 `docs/06` §8，项目**还不能标完成**：Postgres 并发证明未闭合；未参与实现的
新智能体平行审查未做。

## 已核对事实（不要回退）

| 项 | 事实 |
|---|---|
| 公开仓 | https://github.com/NafYoung/customer-service-agent-v0 |
| 公开 Demo | https://rivet-public-demo.onrender.com/ ，`preparation_scripted`，无 DeepSeek Key |
| 宿主 UI | 已接 Preparation Agent（scripted；live 仅本地） |
| holdout v1 / v2 | 已退役；禁止同题重跑。v2 为 44/80、`pass^4=0.40` |
| 公开回归 | 加固后 28/28，`pass^4=1.00`，业务写入 0 |
| 价格快照 | `pricing/deepseek-v4-flash-2026-08-19.json` 的 `valid_until` 为 2026-08-26；墙钟已过期。未授权新快照前，live DeepSeek 必须失败关闭 |
| 市场调研 P0/P1 | 对标章节、ADR、转人工、审计快照、引用门、shadow 回放已落地 |

## 本轮已改

对齐现役入口，不改业务代码、不延长价格窗口、不重跑评测：

- `docs/README_DETAILED.md`：宿主 UI 已接入；GitHub / Demo 已公开。
- `docs/12_phase6_publish_checklist.md`：Demo URL 已部署；默认模式与
  `render.yaml` 一致为 `preparation_scripted`。
- `docs/09_project_status.md` / `docs/10_public_demo_status.md`：核对日与
  Phase 6 状态。
- `tests/test_public_status_docs.py`：禁止现役入口再写「UI 未接入 / Demo 未部署 /
  仍待选平台」。

## 待你拍板（二选一，不要悬着）

Phase 5 现在是合同漏洞：SQLite 能证幂等重放和竞争 confirm 不双执行，**不能**证
行锁、最后一件库存、故障半提交回滚。`docs/11_phase5_concurrency_plan.md` 已写清。

| 选项 | 做什么 | 何时选 |
|---|---|---|
| A. 窄证明 | Compose 加 Postgres；只加「两审批抢最后一件库存」+「杀进程后同审批重试回到首次 `ActionExecution`」 | 面试会追问并发，且你愿意扩测试面 |
| B. 正式降级 | 在 `docs/06` / `docs/09` 写明 v0 以 SQLite 串行写为边界，PG 不在作品集完成范围内 | 默认。作品级原型够用 |

未拍板前，不要一边说 Phase 6 已发布，一边让 Phase 5 无限「仍待」。

## 不要做

- 多 Agent / LangGraph / MCP / 完整 Eval 框架 / 政策向量 RAG。
- 同题重跑 holdout v1 / v2；在离线路由门变绿前开 holdout v3。
- 把开发集 `40/40` 或公开回归 `28/28` 说成 holdout。
- 公开 Demo 部署 live DeepSeek 或项目 Key。
- 编造或擅自延长价格快照；把 `verify_return_evidence` 讲成真实视觉验货。
- 再往 README 塞行业调研。对标章节已经够用。

## 能力缺口（零付费优先）

holdout v2 失败聚类是工具路由，不是安全写入：

- A 库存乱走订单列表，未收敛 `get_inventory`
- B 退换未打 `check_action_eligibility`
- C 注入未先 `search_policy`
- D/E 查询语义不稳

公开 7×4 只证明不回退。要用 `evals/run_shadow_offline.py` 和 scripted 用例先把
A/B/C 做成离线回归。新 holdout 需要新 `case_set`、重绑 49/49 校准、剩余预算，
且只能跑一次。

## 面试口径（3 分钟）

1. 打开 Demo →「取消订单 ORD-1001」→ 确认卡来自 DB canonical preview → 确认执行或拒绝落工单。
2. 模型没有 `execute`：聊天机器人编造政策会被当成公司承诺（Air Canada）。
3. holdout `44/80` 不是改写成 PASS，而是 FAIL → 聚类 → Prompt/回归加固 → 公开 7×4 的 `28/28`。
4. 公开 Demo 是 `preparation_scripted`（零外网），不是 live DeepSeek。Render Free 闲置会休眠，简历同时放本地无 Key 命令。

被追问时主动划清：

- 只读 6 工具 + Preparation 9 工具是分阶段同一职责面，不是多 Agent 编排（ADR-1）。
- 本机智能体封存是流程隔离，不是第三方盲测。
- 语义裁判与被测模型同源；安全硬门靠代码，不靠裁判。

对外入口保持三件套：`README.md`、`docs/09_project_status.md`、
`docs/14_architecture_decisions.md`。其余 `docs/testing/` 与调研报告标成档案，
不要和现役状态并列。

## 建议下一刀（需另授权）

1. Phase 5：选 A 或 B，改合同并改测试/文档。
2. 可选：一页「面试 3 分钟」脚本（可附确认卡截图，抗 Render 冷启动）。
3. 未拍板前不要动 Agent、工具面或付费路径。
