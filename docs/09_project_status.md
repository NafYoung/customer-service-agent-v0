# 项目现役状态

最后核对：2026-07-31（用户批准新 ledger 后付费校准再失败；已停付费）

本地分支：`main`

最近已提交检查点：与本文件同 Git 提交

Preparation Agent 检查点：`1b034cd`

离线修复基线：`56c5c6f`（语义裁判 prompt v2 + 付费路径传输失败快速失败关闭）

账本授权提交：`b6d5e5b`

本文是项目恢复工作的现役入口。阶段验收标准仍以
`docs/06_portfolio_completion_plan.md` 为准；历史结果保留在对应
`docs/testing/` 报告中。

## 当前结论

| 事实面 | 状态 | 证据 |
|---|---|---|
| 确定性后端与只读 Agent | verified-current | 完整离线门通过；Reference Eval 8/8 |
| Eval 证据与预算闸门 | verified-current | 开发集 40/40；新 live 账本可用 |
| holdout v1 | verified-current / retired | 唯一正式结果 46/80、`pass^4=0.35`；禁止重跑 |
| Preparation Agent | changed-and-verified | 提交 `1b034cd`；独立审查 Gate GO |
| 原子命题语义门 | changed-and-verified | `0077b1f` 三路 fresh reaudit ALL GO；离线 prompt v2 于 `56c5c6f` |
| 正式 Eval 证据链 | changed-and-verified | `0077b1f` runtime/budget 轨 Gate GO |
| 非正式付费入口 | changed-and-verified | `0077b1f` 预算/隐私轨 Gate GO |
| DeepSeek 语义校准 | **failed / stop paid** | 新 ledger 付费跑 **19/49**；裁判质量不足；**禁止**再付费重试直至离线修复有新证据 |
| 公开回归与 holdout v2 | blocked | 校准未过；holdout v2 需独立评测智能体封存，禁止本路径自封 |
| 宿主确认、并发、UI、GitHub、公开演示 | pending | 尚未实现或发布 |
| 生产运行态 | not-applicable | 没有远端、部署或公开 URL |
| Agent 记忆 | generated-read-only | 本次未获授权写入，也未修改 |

## 预算账本策略（2026-07-31 用户明示批准）

用户明示批准：归档已耗尽的本地 live 账本，换用全新本地账本，并继续付费语义校准（校准通过后跑公开回归）。合同上限不变。

| 项 | 值 |
|---|---|
| 归档只读路径 | `artifacts/private/deepseek-budget.exhausted-20260731T053125Z.sqlite3`（mode 0444；**未删除**） |
| 规范 live 路径 | `artifacts/private/deepseek-budget.sqlite3`（attestation 绑定固定路径；本轮已新建） |
| 正式硬上限 | ¥20（未上调） |
| 自动执行上限 | ¥18（未上调） |
| 传输×预算 | `56c5c6f`：付费路径 `RequestError` 首次 uncertain 后立即失败关闭 |

旧归档账本的 uncertain 仍保留为审计证据，不计入新 live committed。

## 付费语义校准（2026-07-31，新 ledger；`b6d5e5b`）

私有失败产物（`results_omitted=true`，不可 attestation）：

- `artifacts/private/semantic-judge-calibration/eval-20260731t053558z-b5e34113e0e4.untrusted.json`
- `attestation_kind=semantic_judge_calibration_failed`；`schema_version=2.0-untrusted`
- `source_git_commit=b6d5e5b…`；`run_id=eval-20260731t053558z-b5e34113e0e4`

### 门禁结果（裁判质量为主因）

| 指标 | 值 |
|---|---|
| total / passed / gate | 49 / **19** / **false** |
| positive（期望 gate pass） | 10/21 ≈ 0.48 |
| adversarial（期望 gate fail） | 9/28 ≈ 0.32 |
| failed_fixture_ids | 30 |

按夹具 `kind`：

| kind | pass |
|---|---|
| contradiction | **0/7** |
| unsafe_prompt_injection | 1/7 |
| negation_flip | 2/7 |
| safe_prompt_injection | 3/7 |
| safe_paraphrase | 3/7 |
| safe_canonical | 4/7 |
| generic | 6/7 |

相对早先 `e42de5ec`（16/49）略升至 19/49，但 contradiction 仍全灭；**校准门未过**。

未生成独立 review receipt；未跑七条公开回归；未封存 holdout v2。

### 预算（新 live；无传输锁定）

| 范围 | attempt | settled_exact | uncertain | settled_cny | remaining_execution |
|---|---|---|---|---|---|
| run / cumulative | 49 | **49** | **0** | ≈**0.0369** | ≈**17.963** |

- 本轮 spend delta ≈ ¥0.0369；无 uncertain；传输快速失败关闭生效。
- 头寸仍充足，但按协议：**裁判质量失败后停止付费重试**，改离线分析。
- `FORMAL_*` 未上调。

离线对照：`tests/test_semantic_calibration.py` 全绿；label oracle 仍可复现
21/28 gate。差距在 DeepSeek 实跑与 prompt/合同对齐，不在离线夹具表面。

## 历史：旧 live 账本上的失败（已归档）

- `eval-20260731t010742z-e42de5ec196b`：16/49；17 笔 `MODEL_TRANSPORT_ERROR`
  uncertain 锁死 ¥18 头寸。
- 账本已归档为 `deepseek-budget.exhausted-20260731T053125Z.sqlite3`。
- 更早 `failed-calib-20260730`（SHA `68468a1`）：49/49 协议错误，见
  `artifacts/private/phase2-reaudit/failed-calib-20260730/`。

## 离线修复基线（`56c5c6f`）

1. 语义裁判 prompt v2（评估顺序、evaluator-manipulation、矛盾规则）。
2. 付费路径传输失败不再连环 uncertain。

**本轮证明：** (2) 有效（0 uncertain）；(1) **不足以**让 DeepSeek 达到 49/49。

## 最近验证

对基线 `0077b1f` 的三路 `phase2_fresh_adversarial_reaudit` 仍为 ALL GO。
价格快照 `pricing/deepseek-v4-flash-2026-07-30.json` 在本轮时仍有效
（`valid_until=2026-08-06T17:20:00+00:00`）。

## 当前唯一执行顺序

1. ~~三路 reaudit / 离线修复 / 用户批准新 ledger / 一次付费校准。~~ **已完成。**
2. **停付费。** 离线强化裁判合同（优先 contradiction + unsafe prompt injection），
   用 label oracle / 单测证明后再申请下一次付费校准。
3. 仅当新的一次付费校准 49/49 + validator + 5 条程序性 GO 复核通过后，才跑
   七条公开回归（28/28、`pass^4=1.00`）。
4. holdout v2：**禁止本会话自封**；须独立评测智能体按
   `docs/testing/readonly-holdout-v2-protocol.md` 建题与正式运行。

## 不可突破的恢复边界

- 总 DeepSeek 费用硬上限 ¥20；自动执行上限 ¥18。
- `.env`、预算账本、私有案例、原始 artifact、本机路径和 provider request
  ID 不进入 Git 或公开构建产物。
- 公开演示只使用合成数据和离线已验证轨迹，不部署项目 DeepSeek Key。
- 语义裁判不能覆盖工具、权限、写入、状态或确认的确定性失败。
- 最终完成前必须再由一个全新、未参与实现的智能体做完整平行审查。
- uncertain 预留永久计入 committed；换新账本仅在用户明示批准且旧账本只读归档时。
- **裁判质量失败后不得连环付费重试。**

## 当前工作区说明

新 live 账本 remaining_execution ≈ ¥17.96；校准 19/49 失败关闭。下一步是
**离线裁判修复**，不是再次付费。
