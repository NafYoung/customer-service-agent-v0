# Agent 接入计划与当前进度

## 1. 当前状态

v0 是供 Agent 调用的确定性交易后端原型。它已有业务规则、状态机、最小工具契约、宿主确认边界、调试轨迹和 Reference Eval。当前新增了 provider-neutral 单 Agent 循环与 DeepSeek OpenAI-compatible 适配，但只开放 6 个只读/资格工具；Prompt A/B 从严格 7/10 提升到 10/10，尚需重复运行和隐藏 holdout 验证。

当前不应称为端到端客服 Agent、“安全交易层”或安全审计系统：

- 认证、客户会话和确认由宿主应用负责；
- 完整契约规划了 10 个工具，当前模型运行时只获得前 6 个只读工具；
- Reference Eval 读取结构化 `reference_plan`，不测试自然语言理解；只读 Agent Eval 已有一次真实基线但还不是隐藏 holdout；
- SQLite、固定验证码和本地调试机制都不适合生产使用。

## 2. 模型与宿主的权限边界

当前只读模型可调用：

```text
订单、物流、库存和政策查询
确定性资格判断
```

模型不可调用或接触：

```text
认证与验证码
access token
HOST_CONFIRMATION_TOKEN
present / confirm / execute
客户端幂等键
debug tool events
```

宿主应用必须采用明确的工具白名单，不能把整份 HTTP OpenAPI 自动注册给模型。

## 3. 接入顺序

1. ✅ 增加模型适配器，只开放查询、政策和资格判断工具。
2. ✅ 建立自然语言案例并完成真实 DeepSeek Prompt A/B；严格 7/10 → 10/10，工具调用 25 → 12。
3. 在只读自然语言 Eval 达到基线后，开放三个 `prepare_*` 工具；模型只能解释预览并请求确认。
4. 实现结构化确认卡片，由宿主应用记录 `PRESENTED` 和 `ConfirmationEvent`。
5. 宿主应用在确认后触发确定性执行，并把最终结果交回模型。
6. 将模型输入输出、Agent 工具轨迹和宿主控制流关联到服务端生成的运行标识。

`execute_prepared_action` 不应在任何阶段开放为模型工具。

## 4. 宿主状态

宿主应用至少保存：

```text
server_run_id
conversation_id
auth_session
current_intent
resolved_order_id
resolved_order_item_id
pending_approval_id
approval_preview
preview_hash
confirmation_state
last_tool_result
```

`pending_approval_id` 只能对应一个当前操作。相同客户和会话生成新审批时，旧审批必须进入 `SUPERSEDED`；用户切换订单、商品或目标尺码时必须重新准备。

## 5. 可信确认

允许执行必须同时满足：

- 客户与会话来自宿主注入；
- `approval_id` 属于该客户和会话；
- 宿主已展示与 `preview_hash` 匹配的完整预览；
- 用户通过结构化按钮或受控精确文本确认；
- `ConfirmationEvent` 绑定审批、客户、会话、预览哈希、UI 事件和确认来源；
- 审批未过期、未被取代，订单版本未变化；
- 执行前确定性资格复核仍然通过。

模型输出的 `true`、自造幂等键或“用户应该已经同意”的判断都不能替代确认事件。宿主确认令牌不得进入模型上下文或 Agent 工具参数。

`approval_id` 是一次审批的服务端幂等边界。读取历史执行结果前，必须先校验客户和会话归属。

## 6. 不建议的实现

- 不要让模型直接生成 SQL。
- 不要把客户、会话或认证令牌暴露为工具参数。
- 不要使用一个 `manage_order(action, payload)` 万能工具。
- 不要把业务不变量只写在 Prompt 中。
- 不要让模型自行判断确认或直接调用宿主确认接口。
- 不要把调试轨迹称为安全审计。
- 不要用多 Agent 增加当前原型复杂度。

## 7. 模型接入后的首要验收线

- 越权访问或跨客户结果泄露：0 次。
- 跨会话重放结果泄露：0 次。
- 缺少匹配确认事件的交易写入：0 次。
- 同一审批产生重复业务写入：0 次。
- 模型上下文中出现验证码、访问令牌或宿主确认令牌：0 次。
- 正常查询与政策解释达到可接受基线后，再优化语言风格。

## 8. 本地数据库兼容

P0 增加了审批上下文与确认事件字段。SQLAlchemy `create_all()` 只创建缺失表，不会修改旧 SQLite 表。旧演示数据库必须先备份并重建；在引入正式迁移工具前，不应承诺数据原地升级。
