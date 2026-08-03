# Preparation Agent v1 TDD 证据

## 1. 来源计划

用户旅程和验收条件来自
`docs/06_portfolio_completion_plan.md` 的 Phase 3：

- 认证客户可以用自然语言查询并准备一次取消、退货或换货预览；
- 模型不能获得认证、转人工、展示、确认、执行或调试能力；
- 每个 Agent Approval 可追溯到服务端运行和结构化工具调用；
- prepare 不能修改订单、库存、退换请求或执行状态，也不能新增确认；新预览
  只可确定性地使同会话旧 Approval 和其未执行确认失效。

## 2. RED

先新增：

```text
tests/test_preparation_agent.py
tests/test_preparation_origin.py
```

运行：

```bash
.venv/bin/python -m pytest \
  tests/test_preparation_agent.py \
  tests/test_preparation_origin.py
```

预期失败：

```text
ModuleNotFoundError: No module named 'app.agent.preparation'
1 error during collection
```

该失败由目标 Agent 模块尚未实现导致，不是依赖、语法或测试环境故障。

来源重放完整性又单独经历一次 RED：篡改已保存 preview 后，相同来源重放
最初未抛错，测试报告 `Failed: DID NOT RAISE ServiceError`。GREEN 实现会在
返回幂等结果前重新计算 canonical hash。

空字符串来源对也经历了同类 RED；后续审查又复现 SQLite 接受空白、81 字符
run ID 和 201 字符 tool call ID。GREEN 后只有“两个字段均为 `None`”才表示
兼容的非 Agent 来源；Agent 来源字段必须成对、无首尾空白、非空，并分别
限制为 80/200 个字符。

## 3. GREEN 保证

| # | 保证 | 测试 |
|---|---|---|
| 1 | Preparation Agent 工具集精确为 6 个只读/资格工具加 3 个明确 prepare 工具 | `test_preparation_contracts_are_an_exact_allowlist` |
| 2 | generic prepare、ticket、认证、present、confirm、execute 和 debug 不暴露 | `test_agent_fails_closed_on_non_allowlisted_tools` |
| 3 | cancel、return、exchange 各只生成一个待确认 Approval | `test_agent_prepares_exactly_one_action_without_business_mutation` |
| 4 | Order、Inventory、退换请求和执行记录不改变，且不新增确认 | 同上 |
| 5 | 客户、会话、运行和来源字段不能由模型参数注入 | `test_agent_rejects_model_supplied_identity_and_origin_fields` |
| 6 | return/exchange 缺失事实时不使用默认值 | `test_agent_does_not_default_missing_prepare_facts` |
| 7 | Agent prepare 必须有可信 server run 与 tool call 来源 | `test_agent_requires_a_trusted_server_run_for_prepare` |
| 8 | 含 prepare 的批次必须只有一个调用 | `test_agent_rejects_a_mixed_prepare_batch_before_any_execution` |
| 9 | prepare 成功后再调用工具会回滚整个运行，即使宿主在会话内捕获错误也不提交 | `test_agent_rolls_back_prepare_if_model_requests_another_tool_afterward`、`test_agent_rolls_back_when_host_catches_run_error_inside_session` |
| 10 | 同来源同请求重放不重复创建 Approval | `test_same_prepare_origin_and_request_is_idempotent` |
| 11 | 同来源异请求冲突失败 | `test_same_prepare_origin_with_different_request_conflicts` |
| 12 | origin 或已保存 preview 被篡改后 canonical 校验失败 | `test_origin_tampering_invalidates_the_canonical_preview`、`test_same_origin_replay_rejects_a_tampered_stored_preview` |
| 13 | 旧 HTTP prepare 保持兼容且 origin 允许为空 | `test_generic_http_prepare_remains_compatible_without_agent_origin` |
| 14 | 模型自由文本谎称已执行也不能改变 PREPARED 状态或创建确认、执行和业务写入 | `test_false_execution_claim_cannot_change_structured_or_business_state` |
| 15 | 缺失、空白或超长来源不能创建 Approval | `test_invalid_prepare_origin_is_rejected`、`test_agent_requires_a_trusted_server_run_for_prepare` |
| 16 | 合法参数引用其他客户订单时只返回通用未找到，不创建 Approval 或泄露归属数据 | `test_agent_does_not_disclose_or_prepare_another_customers_order` |
| 17 | 新 prepare 仅使旧 Approval 和未执行确认失效，业务表与确认绑定不变 | `test_new_agent_prepare_only_invalidates_prior_control_state` |

目标测试 GREEN：

```text
31 passed
```

完整离线回归：

```text
187 passed
```

## 4. 静态与 Schema 验证

验证入口：

```bash
.venv/bin/ruff check app evals scripts tests
.venv/bin/mypy
.venv/bin/python scripts/export_contracts.py --check
```

结果：

```text
All checks passed!
Success: no issues found in 45 source files
Contracts are fresh.
总分支覆盖率 80.11%（门槛 80%）
Reference Eval 8/8
```

## 5. 已知边界

- 没有调用真实模型；本报告只证明离线控制流和确定性后端保证。
- 公开宿主 UI 已通过 `DEMO_AGENT_MODE=preparation_scripted` 接入：
  `app/demo/preparation_runner.py` 驱动真实 `PreparationAgent`（scripted
  多轮），确认卡 / present / confirm / execute 复用既有 BFF。
  证据：`tests/test_demo_preparation_integration.py`。
- SQLite 的联合唯一约束提供顺序重放兜底，不代表 PostgreSQL 并发竞争已验证。
- 旧 SQLite 数据库没有 Alembic 迁移，需要备份后重建。
