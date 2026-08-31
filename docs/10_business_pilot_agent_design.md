# 受控业务内测 Agent 设计

最后核对：2026-08-31

## 1. 读者与目标

本文面向项目作者、代码评审者和后续接手的智能体。目标是在不改变 RIVET
既有安全边界的前提下，把已经验证的 Preparation Agent 和确定性交易后端串成
一条可在本地合成业务环境中内测的完整链路：

```text
用户消息
  -> 单 Agent 查询、判断或生成一个待确认提议
  -> 宿主从数据库读取 canonical card
  -> 用户点击结构化按钮
  -> 宿主生成确认事件并调用确定性执行
  -> 明确不适合自动处理的情况进入人工接管
```

本阶段不是生产部署，也不使用真实客户数据。完成本阶段后仍不能宣称已经能
无人值守替代真人客服；可以宣称的是：在已定义的低风险合成流程中，自动处理
链路可被端到端复现，高风险、歧义和异常流程能确定性停止或转人工。

## 2. 外部调研结论

本轮只把外部设计转译为本项目自己的合成代码和测试，不引入外部 Agent
运行时或数据集依赖。

| 来源 | 可复用设计 | RIVET 的取舍 |
|---|---|---|
| [tau2-bench evaluation](https://github.com/sierra-research/tau2-bench/blob/main/docs/evaluation.md) | 默认把最终数据库状态与必要沟通相乘评分；参考动作不一定是唯一正确路径 | 同时硬判 expected state delta、用户沟通、阶段、工具、权限、确认和幂等，不能只比较 DB hash |
| [OpenAI Agents SDK HITL](https://github.com/openai/openai-agents-python/blob/main/docs/human_in_the_loop.md) | 敏感调用形成 interruption，暂停后批准或拒绝，再从持久状态恢复；畸形审批参数失败关闭 | Approval 是一次性待决提议，绑定客户、会话、运行、tool call、预览哈希和订单版本；不使用粘性工具授权 |
| [Parlant human handoff](https://github.com/emcie-co/parlant/blob/develop/docs/production/human-handoff.md) | 转人工后会话进入 manual mode，后续消息标记人工来源 | 首版采用保守 manual mode：一旦结构化接管，同一会话不再调用模型；恢复自动化留给后续受信员工流程 |
| [AgentDojo](https://huggingface.co/papers/2406.13352) | 工具和检索结果属于不可信数据，间接提示注入必须按动作安全评测 | 政策正文、工具结果和工单摘要均不能产生认证、确认或执行权限；新增工具结果注入回归 |
| [BFCL](https://huggingface.co/datasets/gorilla-llm/Berkeley-Function-Calling-Leaderboard) | 缺参数、无相关工具、多轮和不可执行调用要分开测 | 保留现有 schema 失败关闭，并新增“不相关但允许的读工具”与缺失工具场景 |
| [CRMArena](https://huggingface.co/datasets/Salesforce/CRMArena) | CRM 场景覆盖订单、case、知识和多轮交互 | 只借鉴覆盖维度；其 CC-BY-NC 数据不进入本项目训练集、公开测试集或商业内测数据 |

X/Twitter 的一手信号用于风险校准，不作为效果证明：tau-bench 作者强调
“达成目标”不能覆盖未获用户授权的中间操作；Klarna 等企业公开的自动化率和
处理时长属于厂商自报，内测只能把它们转译为待观测指标，不能预先写成 RIVET
的能力数字。

## 3. 首个实现切片

### 3.1 宿主消息入口

- 受信宿主生成 `server_run_id`，注入认证与会话；模型看不到这些字段。
- 每次请求构造一个 Preparation Agent，模型仍只有精确 9 工具白名单。
- Agent 运行前后都检查会话是否已转人工；接管后的请求不再调用模型。
- Agent 最多生成一个 `PREPARED` Approval，不执行任何交易。
- HTTP 响应不暴露原始模型轨迹、内部 token 或 preview hash；始终给出宿主
  生成的权威安全提示，明确本轮没有执行交易或仍待确认。

### 3.2 Canonical card 与空请求体确认

- 浏览器/BFF 不能提交 preview、preview hash、确认来源或 UI event id。
- `present` 从数据库读取归属当前客户和会话的 Approval，以其中的 canonical
  hash 调用既有状态机，并返回可显示的 canonical preview。
- `confirm` 只接受空 body；服务端以 Approval ID 派生稳定 UI event id，记录
  `BUTTON` 确认，再调用确定性执行。HTTP 重试返回同一执行结果。
- 旧 `/v1/actions/*` 继续作为受信内部兼容接口，但不是浏览器集成合同。

### 3.3 确定性人工接管

- `SupportTicket` 新增可空的 conversation、origin run 和 transfer reason
  字段；可空是为了保留旧 Reference Eval/兼容 API，新的 manual handoff
  服务强制这些字段存在。
- 同一客户和会话只允许一条 manual handoff。相同载荷重试返回原工单；不同
  载荷返回冲突，不覆盖原始摘要。
- `HUMAN_REVIEW_REQUIRED` 只能从成功的确定性资格工具结果触发自动转交；
  模型自由文本不能创建工单。
- 客户主动转人工走独立宿主端点，原因是封闭枚举，客户、会话和运行来源由
  宿主注入。
- 首版不自动从 manual 恢复 agent-owned；工单即使被标记 resolved，也需后续
  受信员工流程显式恢复。

## 4. 用户旅程与验收

1. 合成客户说“取消 ORD-1001”，Agent 只生成一条 Approval；展示 canonical
   card 后点击确认，订单恰好取消一次，重复确认返回首次结果。
2. 合成客户申请符合规则的退货或换货时，遵循相同的 prepare、present、
   confirm、execute 链路；执行前仍重新检查订单版本、资格和库存。
3. 瑕疵、错发或运输破损经确定性资格结果创建一条人工工单；同一会话后续
   消息不再调用模型。
4. 客户主动要求人工时，宿主创建结构化接管；重试不重复建单，篡改原因、
   摘要、订单或内部身份字段都不能覆盖原记录。
5. 模型即使声称“已经取消”，没有 Approval/Confirmation/Execution 就没有
   业务变化，宿主响应仍显示“本轮未执行”。
6. 跨客户、跨会话、未展示、过期、被替代、陈旧订单和重复事件全部失败关闭。
7. 离线 scripted model 测试不构造 HTTP client，也不产生模型网络出口。

## 5. 明确不进入本切片

- 多 Agent、LangGraph、MCP、Rasa 或 Parlant 运行时；
- 向量库、RAG、语音、多语言、全渠道和完整工单运营台；
- 真实身份、真实客户数据、ERP、物流、支付或退款接入；
- 在 SQLite 上宣称并发库存安全；
- 重跑 retired holdout v1，或绕过价格、预算、语义校准和独立审查门。

## 6. 当前付费模型阻断

DeepSeek 官方在 2026-08-16 起把 `deepseek-v4-flash` 调整为 USD 计价并按 UTC
峰谷分档。当前仓库只支持单一 CNY 三费率快照，因此不能只延长旧 JSON 的
`valid_until`。在预算模型支持“官方 USD 峰值费率 + 明示的保守换算上界”并
完成同提交审查前，真实模型入口继续失败关闭；本切片只使用离线 scripted
model 验证宿主安全合同。
