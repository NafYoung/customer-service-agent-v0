# RIVET · Customer Service Agent v0

鞋服电商售后的**有界单 Agent 作品原型**：语言理解与交易执行分离；写操作走  
`prepare → 宿主展示 → 可信确认 → execute`，模型拿不到执行权限。

> **不是生产系统。** 数据全为合成样例。详细文档见 [`docs/README_DETAILED.md`](docs/README_DETAILED.md)。

## 一句话架构

```text
用户 → Host UI
        ├─ 只读 Agent（精确 6 工具）→ DeepSeek（OpenAI-compatible）
        ├─ Preparation Agent（精确 9 工具，只 prepare，不 execute）
        └─ 宿主确认令牌 → 确定性后端执行（幂等）
```

- FastAPI + SQLAlchemy + SQLite；跨客户隔离  
- 取消 / 退货 / 换货：确定性资格规则 + 版本化政策  
- Eval：开发集 / 公开回归 / holdout（含失败后归因复验）+ 持久预算闸门  
- CI：`ruff` · `mypy` · 覆盖率 · Schema freshness · `pip-audit` · Gitleaks  

## 评测结果（面试可讲）

| 门 | 结果 | 说明 |
|---|---|---|
| 开发集 10×4 | 40/40，`pass^4=1.00` | 参与过 Prompt 优化，不能冒充 holdout |
| 语义校准 #4 | 49/49 | 隔离裁判 |
| 公开回归 7×4 | 28/28，`pass^4=1.00` | 加固后现役 |
| holdout v1 | 46/80，`pass^4=0.35` | 已退役；无真实安全写入违规 |
| holdout v2 | 44/80，`pass^4=0.40` | 已退役；见 [postmortem](docs/testing/holdout-v2-postmortem.md) |
| 业务写入（只读门） | 0 | 工具白名单 + 状态哈希 |

诚实叙事：**holdout 未过门** → 归因 → 修复 → 用公开 7×4 证明不回退。不是把 FAIL 改写成 PASS。

指标口径对照（与行业「独立解决率」不直接混用）：`pass^1` 对应单任务单次
全部通过，语义上接近行业 resolution；`pass^4` 衡量同任务 4 trial 全部通过的
可靠性；安全硬门保证业务写入 0。行业头部自报独立解决率 80–91% 为厂商口径，
题面难度与系统范围均不同，本项目不宣称与行业数字直接可比。

## 行业对标（2026-08 网络调研）

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

## 合规与数据边界（原型声明）

- **AI 生成内容标识**：演示界面回复气泡带「本回复由 AI 生成」标识，对应
  《人工智能生成合成内容标识办法》（2025-03 印发）的显式标识要求。
- **PIPL 最小必要**：只读取处理售后所需的订单/物流/商品/政策字段；演示数据
  全为合成，不含真实个人信息；调试接口默认关闭。
- **PCI 边界**：v0 不接入真实支付，退款/换货只更新本地业务状态；真实落地须走
  支付 tokenization，模型与 Agent 上下文不得接触卡数据。

```text
用户 → 宿主 UI（认证 + 会话）
         ├─ Agent（6 只读 + 3 prepare；无执行权限；凭证不进上下文）
         └─ 确定性后端（规则/状态机/幂等）→ SQLite（合成数据）
API Key · 宿主确认令牌 · 调试令牌 —— 仅存服务端，从不进入模型工具与上下文
```

## 在线演示（Render）

公网托管步骤见 [`docs/13_hosted_demo_render.md`](docs/13_hosted_demo_render.md)
（GitHub 仓库 + Render Free Web Service；**不是** GitHub Pages）。

**Demo：** https://rivet-public-demo.onrender.com/

Free 档闲置约 15 分钟会休眠，首次打开可能需等待约 1 分钟。

## 3 分钟本地演示（无需 API Key）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
test -f .env || cp .env.example .env

APP_MODE=public_demo \
DEMO_AGENT_MODE=preparation_scripted \
DEMO_ALLOWED_ORIGIN=http://127.0.0.1:8000 \
DEMO_COOKIE_SECURE=false \
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

打开 http://127.0.0.1:8000/ →「取消订单 ORD-1001」→ 核对确认卡 →「确认并执行」或「拒绝」。
消息经 **Preparation Agent**（scripted 多轮工具，零外网）写出 pending；确认与执行仍由宿主完成。模糊意图（如「我想退货」）会先补问订单号。

**转人工闭环**：拒绝待确认操作、会话限额（消息/准备/确认次数）或 live 预算
耗尽时，宿主自动落一张 `SupportTicket` 并把工单号返回给用户；转人工是宿主
决策，模型工具面保持精确 9 工具不变。

本地 live DeepSeek（**禁止**公开部署；走预算闸门，累计硬上限见项目规则）：

```bash
APP_MODE=local \
DEMO_AGENT_MODE=preparation_live \
DEMO_ALLOWED_ORIGIN=http://127.0.0.1:8000 \
DEMO_COOKIE_SECURE=false \
DEEPSEEK_API_KEY=... \
HOST_CONFIRMATION_TOKEN=... \
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

live 模式另有**每会话软闸门**（`DEMO_MAX_LIVE_ATTEMPTS_PER_SESSION`，触发后自动转人工落工单）
与**模型路由预留**（`DEMO_LIVE_QUERY_MODEL` / `DEMO_LIVE_ACTION_MODEL`，留空回退
`DEEPSEEK_MODEL`；换模型前必须先提供该模型的价格快照，否则预算闸门失败关闭）。
全局 ¥20/¥18 硬上限不变。

## 开发运行

```bash
# 建议 Python ≥ 3.11
pip install -r requirements-dev.txt
cp .env.example .env   # 设置 HOST_CONFIRMATION_TOKEN；live eval 再加 DEEPSEEK_API_KEY
uvicorn app.main:app --reload --env-file .env
```

- API：http://127.0.0.1:8000/docs  
- 演示账号：`linfan@example.com` / `246810`  
- 测试：`pytest` · `make verify`  

更多（架构图、取消全流程 curl、Eval 协议、文档索引）→ [`docs/README_DETAILED.md`](docs/README_DETAILED.md) · [`docs/09_project_status.md`](docs/09_project_status.md)
