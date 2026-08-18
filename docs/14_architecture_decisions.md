# 架构决策记录（ADR）：为什么是单 Agent + 确定性后端

> 日期：2026-08-18 · 状态：现役 · 适用范围：v0 求职作品原型
> 每个决策给出：背景 → 决策 → 判据（含外部来源）→ 被拒绝的替代方案 → 后果。
> 本文件不替代 `docs/06_portfolio_completion_plan.md` 的交付门；若未来场景
> 变化需要推翻某项决策，必须在本文件追加新 ADR 而不是静默修改。

## ADR-1：保持有界单 Agent（不引入多 Agent / LangGraph / MCP）

- **背景**：售后客服闭环需要 6 个只读/资格工具 + 3 个 prepare 工具，最多
  4 轮工具调用（`max_tool_rounds=4`、`max_tool_calls=12`）。
- **决策**：保持一个 Preparation Agent（继承只读 Agent 的 6 工具 + 3 prepare），
  不拆分查询 Agent 与交易 Agent，不引入编排框架。
- **判据**：Anthropic 官方指南——单 Agent 能处理绝大多数企业工作流；多 Agent
  只在①任务可大量并行②超出单个上下文窗口③需要专业化分工（不同模型/工具集）
  时才划算（[原文](https://claude.com/blog/building-multi-agent-systems-when-and-how-to-use-them)）。
  本项目 9 工具无并行需求、上下文规模小、单一模型即可覆盖，多 Agent 只会放大
  延迟、成本与错误传播。
- **被拒绝的方案**：LangGraph 编排；「查询 Agent + 交易 Agent」拆分；MCP
  服务器（工具已在仓库内以 Pydantic 契约提供，无需进程外工具协议）。
- **后果**：面试口径固定为「有界单 Agent」；未来若出现大量并行或专业分工
  需求，需按判据重新评审并追加 ADR。

## ADR-2：模型无写权限——prepare → 宿主展示 → 可信确认 → execute

- **背景**：退款/取消/换货是高风险写操作，行业教训是「模型输出会被视为公司
  承诺」（Moffatt v. Air Canada，2024-02 加拿大仲裁裁定航空公司须履行其
  聊天机器人编造的政策，
  [案情分析](https://www.dentonsdata.com/airline-ordered-to-compensate-a-b-c-man-because-its-chatbot-provided-inaccurate-information/)）。
- **决策**：模型只能查询、资格判断与 prepare（非变更预览）；`present` /
  `confirm` / `execute` 只属于宿主，需要宿主确认令牌；确认卡从数据库
  canonical preview 渲染，模型自述「已确认」或伪造按钮 payload 不产生任何
  执行权限。
- **判据**：OWASP LLM Top 10（2025）LLM06 过度代理；human-in-the-loop 的
  运行时权威原则——审批状态由系统状态机强制而非 agent 自律。与 Intercom
  Fin Procedures（[人工审批文档](https://www.intercom.com/help/en/articles/14468561-human-in-the-loop-approvals-for-fin-procedures)）、
  Cloudflare Agents HITL（[文档](https://developers.cloudflare.com/agents/concepts/agentic-patterns/human-in-the-loop/index.md)）
  同构。
- **被拒绝的方案**：模型直接调用退款工具；把模型输出的「用户已确认」文本
  当作执行依据；由模型生成按钮 JSON。
- **后果**：每次写操作多一次人工确认；换取「模型无法执行」的硬保证。演示
  体验因此多一步点击，是刻意取舍。

## ADR-3：版本化结构化政策，不做向量 RAG

- **背景**：退换货窗口、final sale 排除、品相要求等是高频确定性规则。
- **决策**：政策以版本化文档（`policies/` + `policies/index.json`）配合代码
  判定；`search_policy` 只返回政策文本，且文本始终是「数据」而非「指令」，
  不能覆盖系统规则。
- **判据**：高频确定性规则适合结构化判定——新鲜度可版本管理、结论可溯源、
  变更可触发回归；向量 RAG 在政策矛盾时会「自信地选错」
  （[结构化执行优先于 RAG 的讨论](https://www.usefini.com/blog/rag-vs-structured-execution-ai-customer-support)，
  [政策新鲜度要求](https://theneuralbase.com/ai-for-customer-support/learn/advanced/freshness-of-docs/)）。
  检索文本按数据处理还可防间接提示注入（OWASP LLM01）。
- **被拒绝的方案**：向量检索政策问答；把政策文本注入系统提示作为权威指令。
- **后果**：v0 不承诺长尾开放问答；扩展时仍先结构化，必要时再做受控检索并
  保持「数据不越权」。

## ADR-4：语义裁判用原子命题 + 校准，不用整体打分

- **背景**：LLM-as-judge 存在位置偏差、亲善偏差、自偏好与不一致
  （[Judging the Judges](https://aclanthology.org/2025.gem-1.33/)，
  [TrustJudge](https://huggingface.co/papers/2509.21117)）。
- **决策**：回答冻结后，以无工具 JSON 模式逐条判定原子命题；结构错误、歧义、
  证据未对齐全部失败关闭；正式门运行前必须通过 49 条固定公开夹具的校准。
  确定性代码仍负责工具、权限、状态与写入的硬判，裁判不是安全授权器。
- **被拒绝的方案**：整体 1–5 分打分；单一裁判一句话结论。裁判与被测模型同源
  带来的错误相关性是保留限制，正式报告如实披露。
- **后果**：评分更严格、命题维护成本更高；新增命题需重绑校准。

## ADR-5：自研轻量 Eval 协议，不引入完整 Eval 框架

- **背景**：需要可独立复验的证据包（轨迹、用量、状态哈希、回执链）。
- **决策**：借鉴 Inspect AI EvalLog 与 tau-bench pass^k 的数据结构，自研
  runner（`evals/`），保留 manifest/轨迹/summary/integrity 四类 artifact。
- **判据**：作品级原型需要可解释、可复验的 artifact 链；完整框架引入依赖、
  黑盒与维护成本；AGENTS.md 明确禁止「为展示引入完整 Eval 框架」。
- **被拒绝的方案**：直接使用 Inspect AI / LangSmith / 完整 Eval 平台。
- **后果**：与外部基准不可直接互通；README 如实说明自研协议与借鉴来源。

## ADR-6：持久预算闸门——attempt 级预扣

- **背景**：真实 DeepSeek 调用产生费用，行业治理现状差（Amla Labs 报告
  79% 组织对 AI agent 无护栏，
  [报告](https://amlalabs.com/blog/akto-agentic-security-report/)）。
- **决策**：每次付费 HTTP attempt（含重试）前经持久 SQLite 账本
  `BEGIN IMMEDIATE` 原子预扣最坏费用；总硬上限 ¥20、自动执行上限 ¥18；
  价格快照过期、模型不匹配、重复 run 或预扣越界都在请求前失败关闭。
- **判据**：OWASP LLM10 无界消耗；行业普遍「事后看账单」，预扣是前置失败关闭。
- **被拒绝的方案**：运行后统计成本；固定请求次数上限（无法覆盖费用口径）。
- **后果**：价格/账单口径偏差时宁可失败关闭也不超支——作品原型可接受；
  公开演示因此不部署项目 Key，走 scripted/离线模式。

## 附：与行业定价语言的关系

海外头部产品转向按「自动化解决」计费（Fin/Zendesk per-resolution、
Salesforce Agentforce Flex Credits，[官方定价](https://www.salesforce.com/news/press-releases/2025/05/15/agentforce-flexible-pricing-news/)）。
本项目的预算闸门是**成本治理**而非商业计费：已结算开发集 40 任务成本
¥0.04357292（约 ¥0.0011/任务）只用于成本口径对照，README 中已注明。
