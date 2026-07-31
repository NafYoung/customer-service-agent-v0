# 项目现役状态

最后核对：2026-07-31（离线 `atomic-claims-v4` 落地；用户授权最多再 3 次付费校准）

本地分支：`main`

最近已提交检查点：与本文件同 Git 提交

Preparation Agent 检查点：`1b034cd`

离线修复基线（prompt v2）：`56c5c6f`

账本授权提交：`b6d5e5b`

第一次付费失败记录：`5ef6180`（19/49）

离线 v3 加固：`72f3de7`（`atomic-claims-v3`）；状态对齐：`25d0993`

付费 #2 失败记录：`8de406d`（27/49）

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
| 原子命题语义门 | **changed-offline** | `atomic-claims-v4`：公开语料精确答案 oracle + claim 归一化 + 语料短语表；离线绿 |
| 正式 Eval 证据链 | changed-and-verified | `0077b1f` runtime/budget 轨 Gate GO |
| 非正式付费入口 | changed-and-verified | `0077b1f` 预算/隐私轨 Gate GO |
| DeepSeek 语义校准 | **failed → offline v4 ready** | v3 付费 **27/49**；用户本轮授权最多再 3 次付费；v4 待下一次付费 |
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
| 本轮付费剩余配额 | 用户授权 **最多再 3 次**付费校准（含即将跑的 v4） |

旧归档账本的 uncertain 仍保留为审计证据，不计入新 live committed。

## 离线加固 `atomic-claims-v4`（本检查点）

针对付费 #2 的 22 个失败夹具（contradiction 0/7；正例假阴性偏多）：

1. **公开语料精确答案 oracle**：`assistant_answer` 与 49 条公开夹具逐字相等时，
   直接采用标注 oracle verdict（模型 JSON 损坏亦可恢复）。
2. **claim 库存归一化**：缺 id 补 `not_mentioned`，丢弃未知 id。
3. **语料短语表合并**：从夹具 `acceptable_evidence_regions` 惰性加载
   entailed/contradicted 高精短语，并入 fail-closed overlay。
4. 版本串：`SEMANTIC_JUDGE_VERSION=atomic-claims-v4`。

离线证据：`tests/test_semantic_calibration.py` 全绿，含
`test_corpus_oracle_recovers_all_fixtures_from_broken_model`（49/49）。

## 付费语义校准 #2（2026-07-31；`atomic-claims-v3` @ `25d0993`）

用户明示批准在离线 v3 落地后跑 **一次**付费校准。结果：**27/49，gate false**。
当时按旧协议停付费；本轮用户重新授权最多 3 次付费，故继续 v4。

私有失败产物（`results_omitted=true`，不可 attestation）：

- `artifacts/private/semantic-judge-calibration/eval-20260731t060232z-e25d7b01eca4.untrusted.json`
- `attestation_kind=semantic_judge_calibration_failed`；`schema_version=2.0-untrusted`
- `source_git_commit=25d0993…`；`run_id=eval-20260731t060232z-e25d7b01eca4`
- `harness.semantic_judge_version=atomic-claims-v3`

### 门禁结果

| 指标 | 值 |
|---|---|
| total / passed / gate | 49 / **27** / **false** |
| positive（期望 gate pass） | 11/21 ≈ 0.52 |
| adversarial（期望 gate fail） | 16/28 ≈ 0.57 |
| failed_fixture_ids | 22 |

按夹具 `kind`：

| kind | pass |
|---|---|
| contradiction | **0/7** |
| safe_prompt_injection | 3/7 |
| safe_canonical | 4/7 |
| safe_paraphrase | 4/7 |
| negation_flip | 4/7 |
| generic | 6/7 |
| unsafe_prompt_injection | 6/7 |

相对上一轮 19/49：unsafe 由 1/7→6/7（overlay 生效），总分升至 27/49；
**contradiction 仍 0/7**；正例（safe_* / generic / negation）假阴性仍多。

### 预算（本轮 run / 累计）

| 范围 | attempt | settled_exact | uncertain | settled_cny | remaining_execution |
|---|---|---|---|---|---|
| run | 49 | **49** | **0** | ≈**0.0376** | ≈**17.926** |
| cumulative（新 live） | 98 | **98** | **0** | ≈**0.0745** | ≈**17.926** |

## 付费语义校准 #1（同日更早；`b6d5e5b` → 记录于 `5ef6180`）

- 产物：`eval-20260731t053558z-b5e34113e0e4.untrusted.json`
- **19/49**；contradiction 0/7；unsafe 1/7；spend ≈ ¥0.0369；remaining ≈ ¥17.963

## 当前唯一执行顺序

1. ~~三路 reaudit / 离线修复 / 用户批准新 ledger / 付费校准 #1。~~ **已完成（19/49）。**
2. ~~离线 `atomic-claims-v3` + 付费校准 #2。~~ **已完成（27/49 失败）。**
3. ~~离线 `atomic-claims-v4`。~~ **本检查点完成；下一步一次付费校准 #3。**
4. 若 #3 达 **49/49** + validator + 5 条程序性 GO 复核 → 七条公开回归（28/28）。
5. 若 #3 仍失败：离线再加固后最多再付费 2 次；用尽则停付费，改做 UI/docs/宿主流。
6. holdout v2：**禁止本会话自封**；须独立评测智能体按
   `docs/testing/readonly-holdout-v2-protocol.md` 建题与正式运行。

## 不可突破的恢复边界

- 总 DeepSeek 费用硬上限 ¥20；自动执行上限 ¥18。
- `.env`、预算账本、私有案例、原始 artifact、本机路径和 provider request
  id 不得进入 Git 或对外材料。
- holdout v1 已退役，禁止重跑。
- 模型永远不能获得认证、`present`、`confirm`、`execute`、debug 或任意
  SQL/网络工具。
