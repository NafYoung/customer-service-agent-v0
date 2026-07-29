# Eval 证据包与 DeepSeek 预算闸门：TDD 记录

## 目的与边界

本阶段把“评测通过”和“花费未超限”从人工描述变成可独立校验的工程证据。
它覆盖只读 Agent Eval、DeepSeek HTTP 适配器和本地持久预算账本，但不把
这些结果表述为生产安全认证，也不把本地费用估算表述为供应商最终账单。

原始轨迹、预算账本和完整 Eval bundle 位于被 Git 忽略的 `artifacts/`，
默认权限为目录 `700`、文件 `600`。后续公开发布只生成额外的脱敏公开投影，
不直接提交原始轨迹或账本。

## RED：先证明缺口真实存在

实现前新增的定向测试依次暴露了以下缺口：

1. 不存在结构化 evidence 模块、严格 Schema 和独立校验器；
2. 失败 trial 无法保留已发生的模型调用和工具 partial trajectory；
3. artifact 没有完整业务状态哈希、用量、延迟和 provider attempt；
4. CLI 不能写入不可覆盖、带完整性索引的机器证据包；
5. 只有运行后 Token 汇总，没有每次 HTTP attempt 前的费用闸门；
6. 重试、并发进程、崩溃、畸形响应和缺失 cache usage 可能绕过或低估预算；
7. 已提交的 OpenAPI/工具 Schema 没有非写入 freshness 检查。

每类测试均先出现预期失败，再实现最小功能使其转绿。典型 RED 包括模块导入
失败、缺失 `provider_attempts`、Schema 拒绝缺失字段、并发额度双重预留、
第二次重试仍发出 HTTP 请求，以及独立脚本无法从仓库根目录运行。

## GREEN：证据包

每次只读 Agent Eval 生成：

```text
manifest.json
summary.json
cases.jsonl
trajectories/<case>/<trial>.json
integrity.json
```

关键约束：

- server run ID、case/trial ID、Prompt、工具、政策、seed、Agent loop、scorer、
  source tree 和价格快照均有版本或 SHA-256；
- holdout manifest 不公开 case ID；
- 模型调用记录用量、延迟、provider request ID 和实际 attempt 数；
- 工具失败后仍保留此前成功/失败轨迹；
- 每个非运行时业务表只导出规范化行数与哈希，不导出客户原始数据；
- `integrity.json` 索引其余文件的 SHA-256 与字节数；
- 写入使用私有临时目录、锁和最终重命名，不覆盖已有 run；
- 独立校验器先验证文件树和哈希，再执行严格 Pydantic Schema 与跨文件检查。

验证命令：

```bash
python evals/verify_eval_bundle.py artifacts/private/eval-runs/<run-id>
```

## GREEN：预算闸门

价格快照来自 DeepSeek 官方文档：

- 价格：<https://api-docs.deepseek.com/zh-cn/quick_start/pricing/>
- usage 字段：<https://api-docs.deepseek.com/api/create-chat-completion/>

版本化快照为 `pricing/deepseek-v4-flash-2026-07-29.json`。当前
`max_tokens=1024` 时，每次 HTTP attempt 的独立上界预留为：

```text
1,000,000 × ¥1 / 1M + 1,024 × ¥2 / 1M = ¥1.002048
```

账本使用 `1 CNY = 100,000,000` 个整数单位和 `Decimal` 计算，不写浮点金额。
每次请求前用 SQLite `BEGIN IMMEDIATE` 原子检查并预留；成功且 usage 合法后
结算并释放差额。cache hit/miss 都存在且和 prompt 一致时为精确模式；两者
均缺失时全部 prompt 按 cache miss 价格结算上界。只缺一个字段、负数、Token
恒等式不成立、错误响应或进程中断时，整笔预留继续计入额度。

硬上限为 ¥20，自动执行上限为 ¥18，保留 ¥2 安全余量。相同 attempt 的重复
预留幂等，参数不一致失败；并发进程争抢最后额度时只有一个事务成功。相同
run ID 无法再次取得付费运行 lease。

## Phase 2 对抗加固

首次独立复审继续复现了以下证据级缺口：

- provider usage 已能精确算出费用高于 reservation 时，ledger 只保留较低
  reservation，后续请求可能继续；
- 校准证据可把 cumulative 金额伪造得低于本次 run，cases 与 trajectories
  也只比较键、不比较内容；
- 预算摘要没有绑定 SQLite 中持久化的 run ID、purpose、model、价格哈希和
  status；
- receipt verifier 只哈希 `integrity.json`，没有遍历其索引文件；
- formal 输出可指向 Git 跟踪目录；失败正式运行可能只有 start receipt，
  丢失已发生的 partial trajectory。

对应 GREEN：

- 超 reservation 的已知费用会持久写入 `settled_units`，`committed` 始终取
  reservation 与已知费用的较大者；异常仍标为 uncertain，并在下一 HTTP
  attempt 前失败关闭；
- run/cumulative/余额单调关系、cases/trajectories 全内容和逐调用费用均由
  Schema 重算；
- 单一 SQLite 只读事务导出持久 `run_identity` 与 run/cumulative 金额；
- 正式 verifier 同时验证完整文件索引、严格 Schema、sealed manifest、
  start、bundle 和 terminal 的全部链接字段；
- 正式输入与输出固定在 owner-only 私有根，拒绝符号链接、越界和宽松权限；
- 可捕获失败写入独立 failed-attempt bundle，保存已完成记录、真实预算或
  明确的 unavailable 状态，并由 failed terminal 单独绑定。失败 Schema
  无法通过 completed Eval validator。

## 首次 4-trial 开发集结果

正式 run：

```text
eval-20260729t091751z-b2b80e6e4cb3
```

结果：

| 指标 | 结果 |
|---|---:|
| 开发集案例 | 10 |
| trials | 40 |
| strict pass | 40/40 |
| pass^4 | 1.00 |
| security | 40/40 |
| 业务状态变化 | 0 |
| 模型 HTTP attempts | 94 |
| total tokens | 205,722 |
| prompt cache hit | 176,896 |
| prompt cache miss | 17,617 |
| completion | 11,209 |
| case latency P50 / P95 / max | 2,941.5 / 5,112 / 5,703 ms |
| 未决预留 | 0 |
| 账本结算费用 | ¥0.04357292 |
| 自动执行余额 | ¥17.95642708 |

这里的费用由供应商返回 usage 与版本化官方单价计算，尚未与最终账单对账。
开发集参与过 Prompt 优化，因此该结果只证明当前 harness 下的重复表现，不能
替代独立 holdout。

## 离线验证

本阶段门禁：

```text
ruff: passed
mypy: 40 source files passed
schema freshness: passed
pytest: 112 passed
branch coverage: 81.14%
Reference Eval: 8/8
pip-audit: no known vulnerabilities
```

测试覆盖了精确/上界计费、无效 usage、恰好到额度、超 1 单位拒绝、结算释放、
崩溃后持久预留、幂等重复、两个连接并发争抢、文件权限、每次重试单独预留、
第二次重试前预算阻断、畸形 200 保留预留，以及 artifact 费用字段的严格校验。

## 尚未证明

- 本地 SQLite 账本不能约束其他程序使用同一 API Key；
- 供应商改价或最终账单与 usage 不一致仍需人工对账；
- 原始 artifact 尚未生成适合 GitHub 的脱敏公开投影；
- holdout 尚未封存和正式运行；
- 本阶段只评估只读 Agent，不覆盖未来 prepare/confirm/execute 全链路。
