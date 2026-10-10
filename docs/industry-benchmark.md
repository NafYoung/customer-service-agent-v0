# 行业对标（2026-08 网络调研）

> 从 README 移出，原文未改。

> 以下对照来自公开网络调研；外部产品数据为厂商自报口径，本项目数据以本地评测 artifact 为准。

### 为什么写操作必须「宿主确认 + 确定性执行」

行业把这类模式称为 human-in-the-loop approval：Intercom 的 Fin Procedures
对关键操作提供人工审批（[文档](https://www.intercom.com/help/en/articles/14468561-human-in-the-loop-approvals-for-fin-procedures)），
Cloudflare Agents 将「执行前暂停并请求人确认」列为标准 agentic 模式
（[文档](https://developers.cloudflare.com/agents/concepts/agentic-patterns/human-in-the-loop/index.md)）。
反面判例 Moffatt v. Air Canada（2024-02，加拿大仲裁）裁定航空公司须履行其
聊天机器人编造的政策：模型输出一旦被视为公司承诺，错误成本由公司承担
（[案情分析](https://www.dentonsdata.com/airline-ordered-to-compensate-a-b-c-man-because-its-chatbot-provided-inaccurate-information/)）。
因此本项目退款/改单类写操作只能由宿主在可信确认后确定性执行；模型自述
「已确认」或伪造按钮 payload 不产生任何执行权限。

### 为什么是「单 Agent + 版本化结构化政策」

Anthropic 官方指南：单 Agent 能处理绝大多数企业工作流，多 Agent 只在可大量并行、
需要独立上下文窗口或专业化分工时才划算（[原文](https://claude.com/blog/building-multi-agent-systems-when-and-how-to-use-them)）。
本项目 6 只读 + 3 prepare 工具无并行需求，保持单 Agent。高频售后规则由
版本化结构化政策 + 代码判定，不做向量检索；检索到的政策文本是数据而非指令，
对齐「结构化执行优先于 RAG」的行业实践。

### 成本口径：per-resolution 对照

海外头部产品转向按「自动化解决」计费：Fin 与 Zendesk 采用 per-resolution /
automated resolutions 定价，Salesforce Agentforce 按会话 + Flex Credits
（[官方定价](https://www.salesforce.com/news/press-releases/2025/05/15/agentforce-flexible-pricing-news/)）。
本项目用持久预算闸门管理真实模型调用：总硬上限 ¥20、自动执行上限 ¥18、
attempt 级预扣。已结算的开发集 40 任务总成本 ¥0.04357292，折合每任务约
¥0.0011——这是成本口径对照，不是商业定价。

### 时代背景：平台取消「仅退款」之后

2025-04 起淘宝、京东、拼多多、抖音、快手全面取消「仅退款」，售后改由商家
自主处理（[报道](https://www.bbtnews.com.cn/2025/0422/554521.shtml)）。商家需要
自己的售后决策与风控能力，「确定性规则 + 人工确认 + 审计」正是对这一局面的回应。
行业以独立解决率/转人工率为主叙事（头部自报 80–91%，口径不一）；本项目以
pass^1/pass^4 报告任务成功率，两者口径不同，不直接混用。
