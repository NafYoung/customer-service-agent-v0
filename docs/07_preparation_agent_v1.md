# Preparation Agent v1

## 1. 目标与边界

本阶段新增独立的 Preparation Agent 核心，不把交易准备职责继续堆入只读
Agent。它可以完成自然语言信息收集、查询、资格判断和一次操作准备，但不能
展示确认卡、记录确认或执行交易。

精确工具白名单为：

```text
get_customer_orders
get_order
get_shipment
get_inventory
search_policy
check_action_eligibility
prepare_cancel_order
prepare_return
prepare_exchange
```

以下能力不存在于该 Agent 的工具 Schema：

```text
generic prepare_action
create_handoff_ticket
authenticate_customer
present
confirm
execute
debug
任意 SQL 或网络请求
```

机器可读契约见 `docs/preparation_tool_contracts.schema.json`。

## 2. 来源绑定

宿主为每次运行生成 `server_run_id`，模型供应商返回结构化
`tool_call_id`。Preparation Agent 在执行 prepare 前把二者作为内部上下文
传给确定性后端；它们不属于模型参数。

Approval 保存：

```text
origin_server_run_id
origin_tool_call_id
order_version
canonical preview
preview_hash
```

来源对有联合唯一约束。相同来源、客户、会话和请求重放返回原 Approval；
来源相同但请求或归属不同返回 `PREPARATION_ORIGIN_CONFLICT`。如果只提供
来源对中的一个字段，返回 `PREPARATION_ORIGIN_REQUIRED`。两个字段都必须
去除首尾空白后仍非空，且分别不超过 80 和 200 个字符；不符合时返回
`PREPARATION_ORIGIN_INVALID`，避免依赖数据库后端是否执行字符串长度约束。

来源字段也进入 canonical preview hash。修改数据库中的来源字段会导致后续
预览完整性校验失败。`X-Run-ID` 仍只是不可信的客户端调试标识，不能替代
`server_run_id`。

## 3. 单次准备事务

Preparation Agent 复用只读阶段已经验证的有界循环，但拥有独立 dispatcher
和 prompt，并额外执行两条确定性控制：

1. 一个批次只要包含 prepare，就只能包含该一个调用；
2. 一次运行成功 prepare 后，模型下一轮只能返回最终说明，不能再调用工具。

第二条被违反时抛出 `TOOL_CALL_AFTER_PREPARE`。只要调用者使用项目的数据库
会话边界，整个运行事务会回滚，因此不会留下 Approval 或成功 ToolEvent。
失败的 prepare 可以把稳定错误返回模型，用于补齐事实或说明业务拒绝。

首次成功 prepare 只允许增加：

```text
1 个 Approval
脱敏 ToolEvent
```

它不能修改 Order、Inventory、ReturnRequest、ExchangeRequest 或
ActionExecution，也不能新增 ConfirmationEvent。资格会在 prepare 服务内部
重新检查，不信任模型此前的判断。

如果同一客户和会话已有未完成 Approval，安全规则要求新 prepare 在同一事务
中使旧卡失效：旧 Approval 进入 `SUPERSEDED`，其尚未执行的
ConfirmationEvent 仅写入 `consumed_at`。这属于授权控制状态失效，不是业务
执行；不会创建新确认或执行，确认事件原有的客户、会话、审批、预览和 UI
事件绑定保持不变。

## 4. 宿主责任

Preparation Agent 返回结构化 `prepared_action`，其中包含 `approval_id`、
preview、preview hash、状态和有效期。模型自由文本只能用于解释，不是确认
或执行依据。

本阶段不使用“已取消”“已退款”等关键词过滤模型文本。该方法容易被多语言、
否定句和表达变体绕过，也容易误伤正常说明。即使模型文本错误声称已执行，
宿主也必须只把结构化 `prepared_action.status` 和数据库状态作为事实；测试
保证这类文本不会创建确认、执行或业务变化。语言真实性另由 Eval 评分，不能
替代确定性授权边界。

后续宿主必须：

1. 根据 `approval_id` 从数据库重新读取 canonical preview；
2. 渲染规范化确认卡；
3. 只接受结构化按钮确认；
4. 记录绑定客户、会话、审批和 preview hash 的确认事件；
5. 在独立确定性事务中重新检查并执行。

这些能力属于后续阶段，当前 Preparation Agent 核心不提供。

## 5. 兼容和限制

现有 generic HTTP `/v1/actions/prepare` 保持兼容，用于确定性后端演示和旧
测试；它不在模型工具 Schema 中，所创建 Approval 的 origin 字段可以为空。

SQLAlchemy `create_all()` 不会给已有 SQLite 表增加 origin 列。本阶段的新
环境和内存测试会创建完整表；旧演示数据库需要备份后重建。正式迁移和
PostgreSQL 并发证明留到后续阶段，因此当前不能宣称来源重放在并发竞争下
已经得到完整证明。
