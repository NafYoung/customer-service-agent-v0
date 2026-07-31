# DeepSeek 只读 Agent v1

## 1. 目标与边界

本阶段把自然语言理解接到现有确定性后端，但不开放任何业务写入。模型只
能使用：

```text
get_customer_orders
get_order
get_shipment
get_inventory
search_policy
check_action_eligibility
```

`prepare_*`、`create_handoff_ticket`、认证、展示、确认、执行和调试接口都
不在运行时工具列表中。未知工具名会在调用业务代码前失败关闭。

## 2. DeepSeek 兼容配置

`.env.example` 提供非秘密配置：

```text
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_TIMEOUT_SECONDS=30
DEEPSEEK_MAX_TOKENS=1024
DEEPSEEK_MAX_RETRIES=2
AGENT_MAX_TOOL_ROUNDS=4
AGENT_MAX_TOOL_CALLS=12
```

Key 只由模型客户端从宿主设置取得，不进入 messages、工具 Schema、
Agent 轨迹或错误文本。缺少 Key 不影响确定性后端和离线测试，只会阻止
构造真实 DeepSeek 客户端。DeepSeek 工厂只接受 HTTPS Base URL，避免
Bearer Key 经明文链路发送。

当前适配器调用：

```text
POST https://api.deepseek.com/chat/completions
Authorization: Bearer <local secret>
stream: false
thinking.type: disabled
tool_choice: auto
```

使用项目已依赖的 `httpx` 直接发送兼容请求，不额外安装 OpenAI SDK。
模型请求遇到网络错误、429 或 5xx 时最多重试 2 次；401、402、400 和
422 不重试。**付费路径例外：** 一旦挂上预算闸门，`httpx.RequestError`
（传输层失败、尚无 HTTP 响应）在首次预留并记为 `uncertain` 后立即失败
关闭，不再按 `DEEPSEEK_MAX_RETRIES` 追加预留——避免单次 logical call 把
最坏情况预留永久锁住最多 3 次。无预算的离线/未付费客户端仍可重试传输
错误。响应被截断、内容过滤、资源不足或出现未知结束原因时不得
作为成功回答。

## 3. 工具循环

每轮执行顺序固定：

```text
模型返回结构化 tool_calls
→ 整批检查数量、调用 ID 和精确工具名白名单
→ 整批 JSON 解析与 Pydantic extra=forbid 校验
→ 宿主注入认证上下文
→ 确定性只读工具
→ 脱敏、最小化结果
→ tool 消息回传模型
```

只执行 `message.tool_calls`。普通 `content` 中即使出现 JSON、XML、
DSML 或看似工具调用的文本，也不会被解析成执行指令。一个批次只要有
未知工具、重复 ID 或参数不合规，就不会执行该批次中的其他调用。退货
资格必须显式提供商品、状态和问题类型；换货还必须提供目标尺码，Agent
不会用乐观默认值补造这些事实。整次运行最多 4 个工具轮次、12 次工具
调用。

## 4. Eval

真实模型评测入口：

```bash
set -a
source .env
set +a
python evals/run_readonly_agent_evals.py
```

每条案例新建独立内存数据库。模型只看到自然语言 `user_message` 与 6 个
工具；`expected` 仅由评分器读取。评分检查：

- 必须/禁止工具；
- 资格工具的确定性结构化结果；
- 跨客户订单统一隐藏；
- 明显写操作成功误报；
- Approval、ConfirmationEvent、ActionExecution 和 SupportTicket 总写入为 0。

当前 10 条是开发集，不是隐藏 holdout，也不代表生产安全认证。没有
实际 DeepSeek Key 的离线环境只能验证适配器、工具循环和评分器，不能
宣称真实模型通过率。

## 5. 当前官方与风险依据

截至 2026-07-29，DeepSeek 官方文档列出的 V4 API 模型为
`deepseek-v4-flash` 和 `deepseek-v4-pro`；旧
`deepseek-chat` / `deepseek-reasoner` 已停止使用。V4 thinking 默认
开启，而工具调用轮需要回传 `reasoning_content`，所以 v1 显式关闭
thinking，后续如要开启应作为独立兼容实验。

官方资料：

- [DeepSeek API 快速开始](https://api-docs.deepseek.com/)
- [Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)
- [Tool Calls](https://api-docs.deepseek.com/guides/tool_calls/)
- [Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)
- [错误码](https://api-docs.deepseek.com/quick_start/error_codes/)
- [模型更新记录](https://api-docs.deepseek.com/updates)

官方 GitHub 与 Hugging Face 上存在尚未被维护者确认的用户报告：某些
流式或自托管场景可能把工具调用退化为普通文本或返回空内容。这些报告
不是已确认缺陷，但支持本阶段采用非流式、小工具集、只执行结构化调用
和失败关闭的设计。
