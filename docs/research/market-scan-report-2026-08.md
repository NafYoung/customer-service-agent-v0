# RIVET 客服 Agent 品类调研与改进建议报告

> **调研时间**：2026-08（本会话）
> **调研方式**：三路并行网络调研子代理，共 59+ 次 web_search（海外企业级 20+ 次 / 国内市场 18 次 / 工程与评测实践 21 次），中英文关键词结合。
> **数据口径**：厂商自报指标已逐一标注；无法从搜索摘要核实的数字标注「未确认」。完整来源链接分别见三份原始调研报告（国内报告文件：`智能客服与电商售后Agent市场调研报告_2025-2026.md`，另两份在本会话消息中）。
> **对标对象**：`NafYoung/customer-service-agent-v0`（RIVET，本地路径 `customer-service-agent-v0/`）。

---

## 一、品类现状速览（三路交叉印证）

### 1.1 海外：从「客服机器人」到「按解决计费的 Agent 经济」

| 信号 | 事实 | 时间 |
|---|---|---|
| 行业整合 | Salesforce 以 **36 亿美元**收购 Fin（原 Intercom）；Zendesk 完成收购 Forethought | 2026-06 / 2026-03 |
| 资本热度 | Sierra 融资 $9.5 亿、估值 $158 亿；Decagon C 轮 $1.31 亿、估值 $15 亿 | 2025–2026 |
| 定价范式 | **per-resolution（按自动化解决计费）**成为主流：Fin 与 Zendesk「automated resolutions」；Salesforce Agentforce 走按会话 + Flex Credits 信用点 | 2025 |
| 标杆数据 | Klarna AI 助手上线首月处理 **2/3 客服会话**（230 万次对话 ≈ 700 名坐席工作量，利润改善约 $40M，自报） | 2024-02 |
| 行业预测 | Gartner：到 **2029 年 agentic AI 将自主解决 80% 常见客服问题**；但 **2027 年 40% 的 agentic 项目将失败** | 2025-03 |
| 反方数据 | Salesforce 研究称 LLM agent 在 **65% 的 CX 任务上失败**；Amla Labs：**79% 的组织对 AI agent 没有护栏**；ISG：近一半「agentic」生产系统不具备真正自主性 | 2025 |
| 失败案例 | **Air Canada 聊天机器人案**（2024-02 仲裁：公司须履行其 chatbot 编造的政策）；McDonald's 终止 IBM AI 点餐测试 | 2024 |

来源精选：[RTE：Salesforce 收购 Fin](https://www.rte.ie/news/business/2026/0615/1578570-salesforce-agrees-to-buy-irish-tech-firm-fin-for-36bn/) · [TechCrunch：Zendesk 收购 Forethought](https://techcrunch.com/2026/03/11/zendesk-acquires-agentic-customer-service-startup-forethought/) · [Salesforce Flex Credits 定价](https://www.salesforce.com/news/press-releases/2025/05/15/agentforce-flexible-pricing-news/) · [Klarna 官方数据](https://www.klarna.com/international/press/klarna-ai-assistant-handles-two-thirds-of-customer-service-chats-in-its-first-month/) · [Gartner 80% 预测](https://www.gartner.com/en/newsroom/press-releases/2025-03-05-gartner-predicts-agentic-ai-will-autonomously-resolve-80-percent-of-common-customer-service-issues-without-human-intervention-by-2029) · [Dentons：Air Canada 案分析](https://www.dentonsdata.com/airline-ordered-to-compensate-a-b-c-man-because-its-chatbot-provided-inaccurate-information/)

### 1.2 国内：仅退款退潮 + 大厂免费抢入口 + 独立解决率叙事

| 信号 | 事实 | 时间 |
|---|---|---|
| 政策窗口 | 淘宝/京东/拼多多/抖音/快手**全面取消「仅退款」**，改商家自主处理售后——商家需要自己的售后决策能力，正是售后 Agent 的市场窗口 | 2025-04 起 |
| 头部产品 | 阿里**全新 AI 店小蜜**（电商首个售前+售后办事型客服 Agent）：转人工率 **-45%**、询单转化 **+10%**（自报） | 2026-05 |
| 免费模式 | 京东**京小智 5.0** 对中小商家**免费开放**，2026 年 618 超百万商家接入 | 2025-09 起 |
| 大模型落地 | 腾讯企点客服接 DeepSeek：车企案例**独立解决率 30%→80%**，一汽丰田 84%（厂商案例口径） | 2025-03~04 |
| 风控方向 | 淘宝天猫上线「售后 AI 假图识别模型」打击 P 图骗退款；反羊毛党成独立赛道（Riskified 等） | 2026-04 |
| 指标共识 | **独立解决率**是行业主叙事指标，头部自报 80–91% | 2025–2026 |
| 合规 | 《人工智能生成合成内容标识办法》2025-03 印发（AI 生成内容需标识）；PIPL 最小必要原则适用客服会话数据 | 2025 |

来源精选：[北京商报：告别秒退](https://www.bbtnews.com.cn/2025/0422/554521.shtml) · [亿欧：AI 店小蜜发布](https://www.iyiou.com/news/202605111129570) · [亿欧：京小智免费开放](https://www.iyiou.com/news/202509251110307) · [腾讯云：独立解决率 30%→80%](https://cloud.tencent.cn/developer/article/2677612) · [DoNews：售后 AI 假图识别](https://www.donews.com/news/detail/4/6526287.html) · [最高检转载：《AI 标识办法》](https://www.spp.gov.cn//tt/202503/t20250314_690508.shtml)

### 1.3 工程实践共识（与 RIVET 直接相关）

| 主题 | 共识 | 关键来源 |
|---|---|---|
| 单 Agent 优先 | Anthropic 官方：单 Agent 能处理绝大多数企业工作流；多 Agent 只在可大量并行 / 超 context / 专业化分工时划算，且放大延迟与错误传播 | [claude.com 官方博客](https://claude.com/blog/building-multi-agent-systems-when-and-how-to-use-them) |
| HITL 运行时权威 | 审批状态必须由**系统状态机强制**，不依赖 agent 自律；确认卡展示「做什么、动什么数据、后果、幂等性」，执行由宿主完成 | [Intercom Fin Procedures HITL](https://www.intercom.com/help/en/articles/14468561-human-in-the-loop-approvals-for-fin-procedures) · [Cloudflare HITL](https://developers.cloudflare.com/agents/concepts/agentic-patterns/human-in-the-loop/index.md) |
| 安全护栏 | OWASP LLM Top 10（2025）：LLM01 注入、LLM06 过度代理、LLM10 无界消耗；工具白名单 + per-tool 权限 + 认证放 Agent 外 | [OWASP 解读](https://www.indusface.com/learning/owasp-top-10-llm/) · [Okta 最小权限](https://www.okta.com/es-es/identity-101/how-to-implement-least-privilege-for-ai-agents/) |
| LLM-as-judge 局限 | 位置偏差、亲善偏差、自偏好；缓解：结构化原子命题 + 校准 + 人审抽样 | [Judging the Judges（ACL 2025）](https://aclanthology.org/2025.gem-1.33/) · [TrustJudge](https://huggingface.co/papers/2509.21117) |
| 生产评测 | 线上真实会话沉淀为回归集；轨迹评测；模拟器评测（NICE Cognigy Simulator、Nubank Snowglobe）；shadow/A-B | [LangSmith Evaluation Concepts](https://docs.langchain.com/langsmith/evaluation-concepts) · [Nubank Snowglobe](http://www.zenml.io/llmops-database/accelerating-ai-agent-development-through-simulation-based-evaluation) |
| 电商售后一致性 | transactional outbox + CDC + 幂等消费者；退款走 tokenization（agent 不接触卡数据）；幂等键 + 状态机跃迁检查是退款接口标配 | [Fieldguide：2025 AI PCI 合规指南](https://www.fieldguide.io/resource-articles/ai-pci-compliance-a-complete-guide-for-businesses-in-2025) |
| 政策知识 | 高频确定性规则用**结构化规则判定**，向量 RAG 只用于开放问答；政策需版本化 + 新鲜度 + 引用可溯源 | [RAG vs Structured Execution](https://www.usefini.com/blog/rag-vs-structured-execution-ai-customer-support) · [Neural Base：Freshness](https://theneuralbase.com/ai-for-customer-support/learn/advanced/freshness-of-docs/) |

---

## 二、RIVET 对标分析

> 已逐一对照三路建议核对项目现状，**去掉项目已具备的能力**，避免重复建设。

### 2.1 已被行业验证的设计（保持并强化叙事，不建议改动）

1. **单 Agent 9 工具**——Anthropic 官方判据（无大量并行需求、无 context 超限、无专业分工）恰好背书 RIVET 的边界；「为什么不用多 Agent」应写成文档而不是仅靠直觉。
2. **prepare → 宿主展示 → 确认 → execute + 运行时权威**——与 Intercom Fin Procedures HITL、Cloudflare HITL 是同一模式；确认由宿主完成、模型自述「已确认」无效，正是 Air Canada 案的对策。
3. **认证/授权在 Agent 外 + 跨客户隔离**——对齐行业「agent 只带会话令牌、宿主做认证授权」方向（Skyflow/Decagon 金融实践同思路）。
4. **工具白名单 + Pydantic `extra="forbid"` + 精确契约**——对齐 OWASP LLM06 最小权限与 MCP 安全清单。
5. **attempt 级预算预扣闸门（¥20 硬上限）**——比行业普遍的「事后看账单」更严格；前置失败关闭是 LLM10 无界消耗的正确解法。
6. **版本化政策 + 确定性资格规则**——对齐「结构化执行优先于向量 RAG」的行业趋势；比纯 RAG 客服更抗幻觉。
7. **开发集/回归/holdout 三层 + 原子命题语义裁判 + 校准协议**——对齐 LangSmith regression、Decagon 评测引擎理念；原子命题门优于整体打分（规避亲善偏差）。
8. **服务端幂等重放**——对齐退款接口标配。

### 2.2 与市场实践的差距（建议范围）

| # | 差距 | 项目现状（已核实） |
|---|---|---|
| G1 | **转人工闭环缺失** | `create_handoff_ticket` 契约（`app/tools/contracts.py`）、服务（`app/services/tickets.py`）、路由 `/v1/tickets` 均已存在，但**未接入任何 Agent 路径**；demo 的 reject/预算耗尽/补槽失败不落工单；无法统计转人工率 |
| G2 | **指标叙事单一** | 只有 `pass^k`；缺 resolution / deflection / handoff 口径定义与映射，缺「预算约束下成本-准确率曲线」 |
| G3 | **护栏只有全局一层** | 只有总额硬闸门；缺 per-conversation 软闸门、per-action 成本上限、模型路由（普通查询 vs 退款判定） |
| G4 | **审计粒度不足** | 有 Approval/ConfirmationEvent/ToolEvent，但缺「决策快照」（规则版本、政策版本、资格输入、成本、确认人）与已执行动作的撤销能力 |
| G5 | **无多模态验货接口** | 行业已普及凭证识别/假图识别（淘宝天猫 2026-04 上线），RIVET 无任何 mock 接口占位 |
| G6 | **合规叙事空白** | 无 AI 生成内容标识（《标识办法》）、无 PIPL 最小必要声明、无 PCI 边界声明与数据流图 |
| G7 | **缺 shadow/A-B 评测通道** | 只有正式付费评测；无「离线回放同一批会话 → 全自动成本/风险报告」通道 |
| G8 | **回答不强制引用** | `search_policy` 返回版本化文本，但最终回答不强制引用条款/版本，无法逐句溯源 |

---

## 三、分级改进建议

> 约束：遵守 AGENTS.md（单 Agent、不引入 LangGraph/MCP/多 Agent/完整 Eval 框架；修改行为同步测试/类型/Schema/README；¥20 硬上限；holdout v1/v2 禁止重跑）。所有「重算指标」类建议均基于**已有 run 数据**，不产生新付费调用。

### P0 · 叙事增强（零/低代码，1–2 天，直接提升面试说服力）

| # | 建议 | 落地 | 依据 |
|---|---|---|---|
| P0-1 | README 增「行业对标」章节：① 用 **per-resolution 成本叙事**包装 ¥20 闸门（每次解决成本上限，对照 Fin/Zendesk 计费单位）；② 用 **Air Canada 案**解释为什么退款/改单必须宿主确认 + 确定性执行；③ 用 **仅退款退潮**（2025-04）讲清项目时代背景 | `README.md`、`docs/README_DETAILED.md` | 海外 #1/#5、国内 #1 |
| P0-2 | 新增决策文档《为什么是单 Agent + 确定性后端》（引用 Anthropic「单 Agent 处理绝大多数企业工作流」与「何时不该用多 Agent」判据；说明为什么不做向量 RAG 而用版本化结构化政策） | `docs/`（新文件） | 工程 #4、工程 #12 |
| P0-3 | **指标三件套**：给现有评测数据补 resolution / deflection / handoff 口径定义与映射表（如「holdout pass^4=0.40 ⇒ 预算内决策正确率」，诚实标注口径差异，**不重跑**）；把 ¥0.0436（40/40 开发集）换算为「每任务成本 ≈ ¥0.0011」放 README | `README.md`、`evals/README.md`、`docs/09_project_status.md` | 海外 #2、国内 #8 |
| P0-4 | 合规最小实现：demo 回复尾部加「本回复由 AI 生成」标识（对应《标识办法》）；README 加 PIPL 最小必要声明 + PCI 边界声明 + 「用户/宿主/Agent/支付网关」数据流图（标注敏感数据不进 LLM 上下文） | `app/static/demo/app.js`、`README.md` | 国内 #10、工程 #15 |

### P1 · 工程改进（各 0.5–2 天，均在现有边界内）

| # | 建议 | 落地模块 | 依据 |
|---|---|---|---|
| P1-1 | **转人工闭环接线**：demo 宿主在「用户拒绝 / 预算耗尽 / 补槽失败 / 资格不通过」路径调用已有的 `create_handoff_ticket` 落 `SupportTicket`，前端展示工单号；**Agent 保持精确 9 工具不变**（转人工是宿主决策，不破坏叙事）；eval 层新增 handoff 统计 | `app/demo/host.py`、`app/demo/routes.py`、`app/demo/replay.py` | 国内 #6、工程 #13 |
| P1-2 | **护栏分层**：全局 ¥20 硬上限不变，新增 per-conversation 软闸门（如每次会话 ≤ N 次 attempt/成本）+ 预留模型路由接口（普通查询 vs 敏感判定可配不同模型），软闸门触发即转人工落工单 | `app/agent/deepseek_budget.py`、`app/demo/preparation_runner.py` | 工程 #10、海外 #7 |
| P1-3 | **决策审计快照**：execute 时把「资格输入、规则版本、政策版本、成本、确认人/时间、幂等键」写入审计快照（`DecisionSnapshot` 表），公开侧仅暴露脱敏投影；对应 PIPL 可解释性与 Gartner「40% 项目失败」的治理叙事 | `app/models.py`、`app/services/actions.py` | 国内 #7、海外 #9 |
| P1-4 | **幂等再加两层**：DB 唯一约束（approval_id + 业务键）兜底并发；execute 前加**状态机跃迁矩阵硬校验**（如已退款订单拒绝二次 execute；prepare 后政策版本变更需重新 prepare） | `app/models.py`、`app/domain/state_machine.py`、`app/services/actions.py` | 工程 #7、工程 #13 |
| P1-5 | **凭证校验 mock 工具**：新增 `verify_return_evidence` 契约 + 确定性 mock 实现（凭证类型/一致性判定），**只挂宿主侧、不进 Agent allowlist**，README 标注 TODO——对齐淘宝天猫假图识别方向而不承诺真实 CV | `app/tools/contracts.py`、`app/tools/facade.py`、`app/services/`（mock） | 国内 #9 |
| P1-6 | **shadow 模式评测通道**：复用 scripted 离线路径，把公开回归 7 条会话回放产出「若全自动：节省成本 / 引入风险」报告（零付费），对齐 Nubank Snowglobe 模拟评测理念 | `evals/`（新 runner）+ `app/demo/replay.py` | 海外 #6 |
| P1-7 | **回答强制引用**：Preparation/只读 Prompt 增加「最终回答必须引用订单字段/政策条款与版本，信息不足显式拒答并转人工」；对应回归集补 1–2 条引用缺失用例（离线验证，不付费） | `app/agent/*_system_prompt.md`、`evals/readonly_regression_cases/` | 海外 #10、工程 #9/#12 |

### P2 · 可选（需新授权/预算，或展示性）

| # | 建议 | 依据 |
|---|---|---|
| P2-1 | 新 holdout 题集（新 `case_set_sha256` + 重绑校准，按既有协议）——行业叙事里「一次性盲测」本身已是亮点，但需新题集与新授权 | 项目协议 |
| P2-2 | 工具轨迹可视化回放器（`ToolTrace`/`model_turns` 已落库，做成时间线 UI），对齐 OTel GenAI `gen_ai.tool.*` 命名 | 工程 #3 |
| P2-3 | 政策版本 × 评测联动演示（退换货窗口 v1→v2 后行为按新版本变化、旧版本用例标记过期） | 工程 #5 |

### 不做（守住边界，写进决策文档）

- **多 Agent / LangGraph / MCP / 完整 Eval 框架**：AGENTS.md 边界；Anthropic 判据也不支持为此引入。
- **真实支付 / PCI 落地、真实 ERP/物流接口、PostgreSQL 升级**：v0 边界（Postgres 属 Phase 5 既有计划，非本次建议）。
- **重跑 holdout v1/v2**：禁止。
- **向量 RAG 政策问答**：与「结构化规则 + 版本化政策」卖点冲突；只建议在决策文档中解释为何不用。

---

## 四、一句话总结

> 市场正从「应答式 chatbot」走向「按解决计费的办事型 Agent」；RIVET 的「单 Agent + 宿主确认 + 确定性执行 + 版本化政策 + 预算闸门」几乎踩中了 2025–2026 全部工程共识（HITL 运行时权威、OWASP LLM06/10、结构化政策优先、回归/holdout 评测），**短板不在架构而在收口**：转人工闭环接线、指标语言翻译成行业口径（resolution/deflection/handoff）、合规叙事（AI 标识/PIPL/PCI）、护栏分层与审计快照。按 P0→P1 顺序投入约一周，即可把「工程扎实的原型」升级为「懂市场、懂合规、指标口径专业」的求职作品。
