# Paid purpose 与价格窗口 TDD 证据

## 来源与用户旅程

本轮行为由 Phase 2 预算对抗审查直接导出，没有外部计划文件。

- 作为付费评测运行器，我只能使用四种已审计 purpose，避免任意入口获得
  预算账本和模型网络权限。
- 作为预算守门器，我要在请求开始前确认价格有效期覆盖 HTTP 超时和固定
  余量，并在响应返回后再次检查，避免跨价格窗口的响应被当作成功。

## RED

提交：`9b6e468 test: reproduce paid purpose and price-window bypasses`

命令：

```text
.venv/bin/python -m pytest -q tests/test_paid_price_window.py --tb=short
```

结果：`6 failed, 3 passed`。失败分别证明未知 purpose 可进入账本和 HTTP、
布尔/整数 purpose 未稳定失败关闭、近过期请求仍会预留并发送 HTTP、响应
跨窗后仍会成功结算。

## GREEN

命令：

```text
.venv/bin/python -m pytest -q tests/test_paid_price_window.py \
  tests/test_deepseek_budget.py tests/test_openai_compatible_client.py \
  tests/test_eval_runtime_snapshot.py --tb=short
```

结果：`69 passed`。

## 保证清单

| # | 保证 | 测试 | 类型 | 结果 |
|---|---|---|---|---|
| 1 | paid purpose 固定为 `diagnostic`、`dev_repeat`、`holdout_formal`、`semantic_judge_calibration` | `test_paid_purpose_allowlist_is_closed_and_canonical` | 单元 | PASS |
| 2 | 未知、空白、布尔和整数 purpose 在插入运行前失败关闭 | `test_ledger_rejects_noncanonical_paid_purpose_before_run_insert` | 单元 | PASS |
| 3 | 未知 purpose 产生零次 HTTP 请求 | `test_unknown_purpose_cannot_reach_paid_http` | 集成 | PASS |
| 4 | 剩余价格窗口小于 HTTP 超时加 2 秒余量时，零预留、零 HTTP | `test_request_is_blocked_before_reservation_when_price_window_is_too_short` | 集成 | PASS |
| 5 | 响应跨窗时只发送一次 HTTP、不重试、返回稳定 `MODEL_PRICE_EXPIRED` | `test_response_crossing_price_window_is_uncertain_and_not_retried` | 集成 | PASS |
| 6 | 跨窗响应原子记为 `uncertain`，可定价 usage 保留已知成本，承诺金额取预留和已知成本较大值 | 同上 | 集成 | PASS |
| 7 | 重复跨窗标记幂等，已结算 attempt 不会被降级为 `uncertain` | `test_expired_response_marking_is_idempotent_and_never_downgrades_settled` | 单元 | PASS |
| 8 | 正常价格窗口仍按 usage 结算 | `test_response_inside_price_window_still_settles_normally` | 集成 | PASS |

## 覆盖率与已知边界

针对预算、适配器和构造器的同一组 69 个测试执行分支覆盖率检查，结果为
`80.42%`，达到本项目 80% 门槛。

测试使用 `httpx.MockTransport`、内存可控时钟和临时 SQLite，不读取 `.env`、
真实预算账本或私有 Eval artifact，也不发起模型或网络请求。项目完整离线门
由 Phase 2 三组并行修复汇合后统一执行。
