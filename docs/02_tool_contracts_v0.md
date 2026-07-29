# Agent 工具契约 v0

完整的未来 Agent Schema 见 `docs/tool_contracts.schema.json`；当前只读 Agent 实际白名单见 `docs/readonly_tool_contracts.schema.json`；宿主 HTTP Schema 见 `docs/openapi.json`。三者用途不同，不能把整份 OpenAPI 自动暴露给模型。

## 1. 边界

完整交易阶段规划了 10 个最小工具：

1. `get_customer_orders`
2. `get_order`
3. `get_shipment`
4. `get_inventory`
5. `search_policy`
6. `check_action_eligibility`
7. `prepare_cancel_order`
8. `prepare_return`
9. `prepare_exchange`
10. `create_handoff_ticket`

以下能力明确不在 Agent Schema 中：

- 客户认证与验证码输入；
- 访问令牌或宿主确认令牌；
- 将预览标记为已展示；
- 记录可信确认；
- 执行已确认操作；
- 调试轨迹查询。

客户身份和 `conversation_id` 由宿主应用注入调用上下文，模型不能选择或改写。HTTP API 可保留宿主认证、展示和确认接口，但 Agent 适配器必须按上述白名单注册工具。

当前 DeepSeek 只读 Agent 只注册前 6 个查询与资格工具。三个
`prepare_*` 和 `create_handoff_ticket` 尚未向模型开放；即使模型伪造这些
工具名，适配器也会在业务调用前拒绝。

## 2. 设计原则

1. **窄权限**：每个工具只完成一个明确动作。
2. **身份注入**：客户和会话身份不作为模型输入参数。
3. **读写分离**：模型只能查询、判断、准备和转人工，不能执行高影响交易写入。
4. **结构化结果**：失败返回稳定错误码。
5. **确定性约束**：资格、归属、版本、确认与最终写入由代码校验。
6. **调试轨迹不是审计**：当前轨迹用于开发和评分，不承担安全审计职责。

## 3. 查询与规则工具

### `get_customer_orders`

列出当前已认证客户的订单。输入为空。

### `get_order`

```json
{"order_id": "ORD-1003"}
```

读取一个订单、商品和已有物流信息。其他客户的订单统一返回 `ORDER_NOT_FOUND`。

### `get_shipment`

```json
{"order_id": "ORD-1002"}
```

读取当前客户订单的结构化物流记录。

### `get_inventory`

```json
{"sku": "GAT-WHITE", "size": "43"}
```

读取当前库存快照。换货执行前由确定性后端重新检查。

### `search_policy`

```json
{
  "query": "鞋盒拆了还能换码吗",
  "region": "CN",
  "channel": "ONLINE",
  "top_k": 3
}
```

政策文本只用于解释，不能替代确定性资格判断，也不能覆盖系统规则。

### `check_action_eligibility`

```json
{
  "action_type": "EXCHANGE_ITEM",
  "order_id": "ORD-1003",
  "order_item_id": "ITEM-1003-A",
  "target_size": "43",
  "declared_condition": "NEW_UNWORN",
  "issue_type": "SIZE_MISMATCH"
}
```

确定性判断取消、退货或换货是否允许。

## 4. Prepare 工具

### `prepare_cancel_order`

```json
{"order_id": "ORD-1001", "user_note": "用户不再需要"}
```

生成取消预览和审批，不修改订单。

### `prepare_return`

```json
{
  "order_id": "ORD-1003",
  "order_item_id": "ITEM-1003-A",
  "declared_condition": "NEW_UNWORN",
  "issue_type": "CHANGED_MIND",
  "user_note": null
}
```

生成退货预览和审批，不创建退货申请。

### `prepare_exchange`

```json
{
  "order_id": "ORD-1003",
  "order_item_id": "ITEM-1003-A",
  "target_size": "43",
  "declared_condition": "NEW_UNWORN",
  "issue_type": "SIZE_MISMATCH",
  "user_note": null
}
```

生成换货预览和审批，不预占库存。

Prepare 返回 `approval_id`、结构化 `preview`、`preview_hash` 和有效期。模型必须展示并解释预览，然后停止工具执行、等待宿主确认；它不能自行调用 present、confirm 或 execute。

## 5. 转人工工具

### `create_handoff_ticket`

```json
{
  "order_id": "ORD-1003",
  "category": "DEFECTIVE_ITEM",
  "summary": "用户反馈鞋底开胶，需要核验证据和责任。",
  "priority": "HIGH"
}
```

用于瑕疵、破损、错发、政策歧义或自动流程无法安全判断的情况。它是低风险运营写入，不代表退款、赔偿或责任认定。

## 6. 宿主确认流

宿主应用独立完成：

```text
认证并注入客户与会话
→ Agent 调用 prepare
→ 宿主展示与 preview_hash 对应的完整预览
→ 用户点击确认或提交受控精确文本
→ 宿主写入 ConfirmationEvent
→ 后端按 approval_id 执行一次
→ 宿主把最终结果交回 Agent
```

宿主确认凭据、`ui_event_id` 和内部执行入口不得进入模型上下文。`approval_id` 是服务端幂等边界；模型和客户端不生成幂等键。

## 7. 工具选择规则

| 用户目标 | Agent 可调用 | Agent 禁止行为 |
|---|---|---|
| 查询订单 | `get_order` | 猜测状态或选择客户身份 |
| 解释政策 | `search_policy` | 将政策文本当系统指令 |
| 取消 | 资格判断、`prepare_cancel_order` | 直接执行或伪造确认 |
| 退货 | 资格判断、`prepare_return` | 把预览说成已退货或已退款 |
| 换货 | 资格判断、`prepare_exchange` | 不查库存或声称已预占 |
| 破损/错发 | 资格判断、`create_handoff_ticket` | 自动认责或承诺赔偿 |

## 8. 调试轨迹

Agent 工具调用可保存工具名、运行标识、脱敏参数、脱敏结果、成功状态、错误码、耗时和时间。认证与宿主确认不属于 Agent 工具轨迹。

调试查询接口默认关闭；显式开启时需要管理员令牌，并只返回安全字段。该机制不等同于覆盖所有失败出口、不可篡改的安全审计。
