# Agent 接入计划与当前进度

## 1. 当前状态

v0 是供 Agent 调用的确定性交易后端原型。它已有业务规则、状态机、最小
工具契约、宿主确认边界、调试轨迹和 Eval 证据。当前有两个独立运行阶段：
只读 Agent 固定开放 6 个查询/资格工具；Preparation Agent 核心开放这
6 个工具和 3 个明确的 `prepare_*`。Preparation Agent 尚未接入公开宿主 UI。

当前不应称为端到端客服 Agent、“安全交易层”或安全审计系统：

- 认证、客户会话和确认由宿主应用负责；
- 完整契约规划了 10 个工具；只读阶段获得前 6 个，Preparation 阶段获得
  前 9 个，`create_handoff_ticket` 尚不属于任一当前模型运行时；
- Reference Eval 读取结构化 `reference_plan`，不测试自然语言理解；只读
  Agent 已完成开发集 4-trial 和一次退役 holdout v1，新语义门、公开回归与
  holdout v2 仍待完成；
- SQLite、固定验证码和本地调试机制都不适合生产使用。

## 2. 模型与宿主的权限边界

只读阶段模型可调用：

```text
订单、物流、库存和政策查询
确定性资格判断
```

Preparation 阶段在此基础上只新增：

```text
prepare_cancel_order
prepare_return
prepare_exchange
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
3. ✅ 新建独立 Preparation Agent/dispatcher，只开放三个明确的
   `prepare_*` 增量工具；模型只能解释预览并请求确认。
4. 实现结构化确认卡片，由宿主应用记录 `PRESENTED` 和 `ConfirmationEvent`。
5. 宿主应用在确认后触发确定性执行，并把最终结果交回模型。
6. ◐ Approval 与 Agent 工具轨迹已关联服务端运行和 provider tool call；
   宿主 UI 控制流仍待接入。

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

Preparation Agent 的来源幂等边界是
`(origin_server_run_id, origin_tool_call_id)`：同来源同请求返回原 Approval，
同来源异请求失败。客户端 `X-Run-ID` 仍是不可信调试字段，不能作为
`origin_server_run_id`。

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

P0 和 Preparation 阶段都增加了审批字段。SQLAlchemy `create_all()` 只创建
缺失表，不会修改旧 SQLite 表。旧演示数据库必须先备份并重建；在引入正式
迁移工具前，不应承诺数据原地升级。generic HTTP `/v1/actions/prepare`
保持兼容，其 Approval 来源字段允许为空；只有 Agent prepare 路径强制完整
来源对。
