# Commerce Agent 工具结果 Fencing：TDD 证据

## 1. 来源与用户旅程

本轮没有外部计划文件。用户要求把 Anthropic 开源的
[`commerce-agents`](https://github.com/anthropics/commerce-agents) 与 RIVET
结合，在提升 Agent 的同时形成高质量开源项目学习材料。源码对照固定到上游
提交 `fd4d59224ab96b43c6dc6888207c67b3bd5a24cf`。

由此形成两个用户旅程：

1. 作为受控客服 Agent 的宿主，我希望第三方政策文本进入下一轮模型上下文前
   被固定边界包裹并清洗伪造控制标记，以免数据伪装成角色、工具或 fence 边界。
2. 作为调试和评测人员，我希望保留剔除敏感字段后的原始 trace，同时只把清洗
   副本交给模型，以便复现攻击载荷且不破坏取证。

对照后没有重做 RIVET 已有的工具白名单、Approval 来源、canonical card 或可信
宿主确认，也没有引入 Claude runtime、购物 Agent、商家 Agent、MCP 或多 Agent。

## 2. RED

先提交测试检查点 `a35252b`，新增或修改以下测试：

```text
tests/test_tool_result_fencing.py
tests/test_readonly_agent.py
tests/test_offline_adversarial_agent_patterns.py
```

执行：

```bash
.venv/bin/python -m pytest -q \
  tests/test_tool_result_fencing.py \
  tests/test_readonly_agent.py \
  tests/test_offline_adversarial_agent_patterns.py
```

预期失败：

```text
ModuleNotFoundError: No module named 'app.agent.tool_result_fencing'
3 errors during collection
```

失败来自目标安全边界模块尚未实现；同一批原有相关测试在改动前为 `19 passed`，
因此不是依赖或测试环境故障。

## 3. GREEN

实现检查点为 `0712f8e`。相同聚焦命令结果：

```text
24 passed
```

| # | 可验证保证 | 测试 | 类型 | 结果 |
|---|---|---|---|---|
| 1 | 正常中文政策、数字和比较符仍可读，NFKC 归一化结果稳定 | `test_sanitizer_preserves_normal_policy_text` | unit | PASS |
| 2 | 不可见/bidi/control、伪造 role、tool、特殊令牌和 fence marker 被中性化 | `test_sanitizer_neutralizes_hidden_and_forged_model_boundaries` | unit | PASS |
| 3 | 每条模型可见工具消息只有一个固定 fence，带服务端生成的工具来源 | `test_tool_message_has_fixed_server_provenance_and_escape_proof_fence` | unit | PASS |
| 4 | 超大工具数据有固定上下文上限 | `test_tool_message_caps_oversized_model_context` | unit | PASS |
| 5 | 参数错误和业务错误也经过同一信封，错误码仍可供模型处理 | `test_agent_returns_invalid_arguments_to_model_without_executing_tool`、`test_agent_surfaces_customer_safe_tool_error_and_can_finish` | integration | PASS |
| 6 | 普通自然语言注入只能留在数据 fence 内，不能改变只读工具权限 | `test_plain_untrusted_policy_instruction_stays_inside_a_server_tool_fence`、`test_untrusted_policy_instruction_cannot_unlock_next_round_prepare_tool_call` | adversarial integration | PASS |
| 7 | 结构化恶意标记在原始 trace 中可复现，在模型副本中已清洗，业务状态不变 | `test_untrusted_policy_markup_cannot_escape_the_server_tool_fence` | adversarial integration | PASS |
| 8 | Preparation Agent 继承同一工具循环，因此 prepare 前的查询结果也经过该边界 | 完整 `tests/test_preparation_agent.py` 与全套回归 | integration | PASS |

## 4. 完整验证

```text
ruff: passed
mypy: 58 source files passed
schema freshness: passed
pytest: 624 passed
branch coverage: 83.35%（门槛 80%）
pip-audit: no known vulnerabilities
Reference Eval: 8/8
```

Apache-2.0 原始许可证逐字保存在 `LICENSES/Apache-2.0.txt`，其 SHA-256 与上游
许可证文件一致：

```text
cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30
```

归属、固定上游提交和 RIVET 修改说明见 `THIRD_PARTY_NOTICES.md`。

## 5. 已知边界

- fencing 建立数据与控制的结构边界，不会删除所有自然语言恶意指令，也不保证
  模型永不受其影响；精确工具白名单、宿主确认和确定性后端仍是交易安全边界。
- 原始 trace 只剔除既有敏感字段，仍可能包含攻击文字；它是离线调试/评测证据，
  不能直接拼接回模型 prompt 或展示给终端客户。
- 12,000 字符上限会截断过大结果；当前合成工具结果远小于该值。真实后端接入前
  仍需为每个工具定义分页、字段最小化和业务级大小限制。
- 本轮没有读取 `.env`、私有预算账本或私有 Eval artifact，没有调用真实模型，
  也没有证明生产身份、并发、安全审计或真实业务效果。

## 6. 提交证据

```text
a35252b test: specify fenced tool-result boundary
0712f8e fix: fence model-visible tool results
```

两个提交均位于 `codex/commerce-agent-fencing`，RED 和 GREEN 历史未被改写。
