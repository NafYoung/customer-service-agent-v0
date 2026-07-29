# P0 安全基线 TDD 证据

## 来源与范围

本轮没有使用仓库内的 `*.plan.md`。用户旅程与验收条件来自本任务中接受的对抗式审查，范围严格限定为：

1. 修复跨客户、跨会话的执行结果重放；
2. 默认关闭调试路由、增加管理员认证、Docker 仅绑定本机；
3. 移除模型可控的确认布尔值与客户端幂等键，建立可信确认状态机；
4. 将身份验证与访问令牌移出 Agent 工具 Schema。

本轮不包含 LLM 接入、PostgreSQL、CAS/高并发库存控制或 P1 评分器重构。

## 用户旅程

- 作为客户，我只能在自己的当前会话中展示、确认和重放自己的审批，不能获知其他客户或其他会话的执行结果。
- 作为宿主应用，我只能用独立宿主凭据把匹配的预览标记为已展示，并用唯一 UI 事件记录可信确认。
- 作为开发者，我默认无法访问调试路由；显式开启后仍必须使用管理员凭据，而且只能读取安全元数据。
- 作为未来 Agent，我只能获得查询、资格判断、prepare 和转人工工具，不能看到验证码、访问令牌、确认入口或执行入口。

## RED / GREEN 记录

### 初始 RED

- 先新增 `tests/test_p0_security_baseline.py`，再修改生产代码。
- 使用当前可用的 Python 3.11 环境和进程内合法演示邮箱兼容桩直接执行测试函数。
- 初始结果：`12/12 tests failed`。失败分别命中缺失的调试开关、Agent 权限收敛、可信确认、所有权优先重放、旧执行路由关闭和 Docker 本机绑定。
- 后续为过期终态新增独立测试，修复前结果为：响应已拒绝，但审批仍停留在 `PRESENTED`，未持久化为 `EXPIRED`。

### 最终 GREEN

最终执行结果：

```text
P0 security integration tests: 18/18 passed
Migrated API and audit tests: 14/14 passed
Domain rule tests: 4/4 passed
Reference environment cases: 8/8 passed
Branch-aware application coverage: 89%
Agent contracts: 10 tools
Default OpenAPI: 12 paths; no debug route; no legacy execute route
```

Reference Eval 只验证确定性环境、参考控制流和评分逻辑，不代表 LLM Agent 成功率。

## 任务证据

| P0 | 执行摘要 | RED 证据 | GREEN 保证 |
|---|---|---|---|
| P0-1 | 重放前先按 `approval_id + customer_id + conversation_id` 验证归属，再读取确认与执行记录 | 客户 B 可命中客户 A 的历史幂等结果 | 跨客户、跨会话均统一 404，不返回订单、动作或执行结果 |
| P0-2 | debug router 默认不注册；开启时要求管理员令牌并返回字段白名单；Compose 仅绑定回环地址 | 默认路由可访问、返回完整事件、Compose 发布所有接口 | 默认 OpenAPI 无 debug；错误/缺失管理员凭据为 401；响应不含客户、参数和结果 |
| P0-3 | 建立 `PREPARED → PRESENTED → CONFIRMED → EXECUTING → EXECUTED` 及终态；确认事件绑定客户、会话、审批、哈希和 UI 事件 | 模型可提交确认布尔值和幂等键直接执行 | 旧字段被 422 拒绝；无展示不能确认；精确重试只写一次；篡改、过期、替换和 stale 状态均失败关闭 |
| P0-4 | HTTP 身份验证仍保留给宿主，但不再经过 Agent facade 或产生 Agent ToolEvent；Agent Schema 删除认证工具 | Agent Schema 返回验证码/访问令牌 | 运行时与导出契约均严格为 10 个工具，递归检查无认证、确认和执行字段 |

## 测试规格

| # | 可验证保证 | 测试位置 | 类型 | 结果 |
|---:|---|---|---|---|
| 1 | 客户 B 不能重放客户 A 已执行的审批 | `tests/test_p0_security_baseline.py::test_cross_customer_cannot_replay_another_customers_execution` | API/数据库集成 | PASS |
| 2 | 同一客户不能跨会话重放审批 | `tests/test_p0_security_baseline.py::test_same_customer_cannot_replay_execution_from_another_conversation` | API/数据库集成 | PASS |
| 3 | 未展示、错误哈希或缺少宿主凭据均不能执行 | `tests/test_p0_security_baseline.py::test_confirmation_requires_presented_matching_preview_and_is_idempotent` | API/状态机集成 | PASS |
| 4 | 相同确认事件精确重试只产生一个确认事件和一个执行记录 | 同上 | API/数据库集成 | PASS |
| 5 | 一个 UI 事件不能确认另一份审批 | `tests/test_p0_security_baseline.py::test_ui_confirmation_event_cannot_be_reused_for_another_approval` | API/数据库集成 | PASS |
| 6 | 同会话新预览使旧审批进入 `SUPERSEDED` | `tests/test_p0_security_baseline.py::test_new_approval_supersedes_old_approval_in_same_conversation` | API/状态机集成 | PASS |
| 7 | 新预览会使已记录但未执行的旧确认失效并被消费 | `tests/test_p0_security_baseline.py::test_new_preview_invalidates_a_recorded_unexecuted_confirmation` | 服务/API 状态机集成 | PASS |
| 8 | 审批快照被篡改时返回完整性错误且不写业务数据 | `tests/test_p0_security_baseline.py::test_tampered_approval_snapshot_fails_closed` | 安全集成 | PASS |
| 9 | stale 确认进入 `FAILED`、消费确认事件且无业务写 | `tests/test_p0_security_baseline.py::test_stale_confirmed_action_is_terminalized_without_business_write` | 事务集成 | PASS |
| 10 | 过期审批进入 `EXPIRED`，不产生确认或执行记录 | `tests/test_p0_security_baseline.py::test_expired_presented_action_is_terminalized_without_confirmation` | 事务集成 | PASS |
| 11 | 旧 `/v1/actions/execute` 与默认 debug 路径不在 OpenAPI | `tests/test_p0_security_baseline.py::test_legacy_execute_route_is_not_available` | 契约集成 | PASS |
| 12 | debug 开启后仍要求管理员认证并只返回安全字段 | `tests/test_p0_security_baseline.py::test_enabled_debug_route_requires_admin_and_returns_safe_trace_fields` | API 安全 | PASS |
| 13 | Agent 工具面严格等于 10 个允许工具，且不含敏感/执行字段 | `tests/test_p0_security_baseline.py::test_agent_contract_excludes_authentication_and_execution` | 契约 | PASS |

## 覆盖率与已知缺口

- 用户授权后已在项目根目录创建独立 `.venv`，使用 Python 3.11.15 安装 `requirements-dev.txt`；`pip check` 未发现依赖冲突。
- 标准 `python -m pytest` 结果为 `36 passed`；`pytest --cov=app --cov-branch --cov-report=term-missing` 得到总覆盖率 `89%`，满足本工作流 80% 门槛。
- 测试输出包含一条 Starlette 弃用警告：当前 `TestClient` 通过 `httpx` 的兼容入口工作，未来依赖升级需要迁移到其建议的新客户端；当前测试行为未受影响。
- SQLite 下的测试不能证明 PostgreSQL 行锁、并发 prepare/confirm/execute 或库存 CAS；这些属于明确保留的 P1。
- 后端测试只能证明受信宿主凭据和确认事件被校验，不能证明真实 UI 确实向真人渲染了卡片，也不代表尚未接入的 Agent 适配器一定遵守白名单。
- P0 改变了 SQLite Schema，当前没有迁移工具；旧演示数据库需先备份再重建。

## 合并证据

当前目录没有 Git 元数据，因此无法创建或验证 RED/GREEN checkpoint commit。本文件保留本轮 RED、GREEN、范围和验证缺口，供后续纳入版本库或 PR。
