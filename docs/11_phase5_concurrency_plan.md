# Phase 5：并发证明计划（骨架）

## 读者

实现者。公开演示切片见 `docs/10_public_demo_status.md`；完整验收合同见
`docs/06_portfolio_completion_plan.md` §Phase 5。

## 目标

证明宿主确定性执行在并发与重试下仍满足：

- 同一审批至多一个 `ConfirmationEvent`、一个 `ActionExecution`、一次业务变化；
- `CONFIRMED → EXECUTING` 认领后再做副作用；
- 同一审批再次 execute 返回首次结果（`idempotent_replay=True`）；
- 两路竞争 confirm 不能双写执行。

## SQLite 现在能证明什么

当前默认引擎是文件 / 内存 SQLite（Dockerfile **无** PostgreSQL）。SQLite 写事务
在库级串行化；`SELECT … FOR UPDATE` 在 SQLite 上基本是空操作。

因此现役测试可锁定：

| 保证 | SQLite 证据形态 |
|---|---|
| 幂等重放 | 顺序二次 `execute_confirmed_action` / API confirm 重放 → 同一 `execution_id` |
| 认领后再写 | execute 路径在副作用前将状态置 `EXECUTING` 并 `flush` |
| 竞争 confirm 不双执行 | 文件库 + 双线程；恰好 1 条 confirmation、1 条 execution、订单只变一次；败者 `ConflictError` |
| 唯一约束兜底 | `confirmation_events.approval_id` / `action_executions.approval_id` UNIQUE |

**不能**用 SQLite 单独声称：

- 真正的行级锁与 `SKIP LOCKED` 式认领；
- 多写者重叠读 `PRESENTED`/`CONFIRMED` 下的可重复隔离证明；
- 库存条件更新在高并发下的线性化（需 PG 或等价）；
- 故障注入半提交后的完整跨连接回滚矩阵。

## 需要 PostgreSQL（后续）才闭合的部分

按作品集合同，完整 Phase 5 仍需：

1. PostgreSQL + Alembic 并发验证环境（当前 Dockerfile 未引入，**本骨架不迁**）。
2. 库存 `UPDATE … WHERE available_qty > 0` 或可靠行锁；两审批抢最后一件只成一单。
3. 故障注入（执行中杀进程 / 故意异常）与事务完整回滚。
4. 响应丢失后跨连接重试稳定回到首次 `ActionExecution`。
5. 可选：SQLite `BEGIN IMMEDIATE` 作为过渡加固（仍非 PG 等价物）。

## 现役代码锚点

- `ActionService._owned_approval`：`with_for_update()`（PG 有效；SQLite 忽略）
- `record_confirmation`：状态机 + `approval_id` / `ui_event_id` 唯一
- `execute_confirmed_action`：先查已有 execution → 幂等；认领 `EXECUTING` 并
  `flush` 后再改订单/库存；最后 `EXECUTED` + 唯一 `ActionExecution`

## 测试入口

```bash
.venv/bin/python -m pytest tests/test_action_concurrency.py -q
```

## 停止条件（本骨架）

- 上表 SQLite 可证项有聚焦 pytest 锁定；
- 本文写清与 PG 的边界；
- **不**引入 Postgres 服务或 Alembic，除非后续明确开迁库切片。
