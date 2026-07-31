# 项目现役状态

最后核对：2026-07-31 05:13 UTC

本地分支：`main`

最近已提交检查点：与本文件同 Git 提交（记录 `e2cd096` 上付费语义校准
失败、预算占用与下一步禁止再付费重跑）

Preparation Agent 检查点：`1b034cd`

本文是项目恢复工作的现役入口。阶段验收标准仍以
`docs/06_portfolio_completion_plan.md` 为准；历史结果保留在对应
`docs/testing/` 报告中。

## 当前结论

| 事实面 | 状态 | 证据 |
|---|---|---|
| 确定性后端与只读 Agent | verified-current | 完整离线门通过；Reference Eval 8/8 |
| Eval 证据与预算闸门 | verified-current | 开发集 40/40；账本仍在 |
| holdout v1 | verified-current / retired | 唯一正式结果 46/80、`pass^4=0.35`；禁止重跑 |
| Preparation Agent | changed-and-verified | 提交 `1b034cd`；独立审查 Gate GO |
| 原子命题语义门 | changed-and-verified | `0077b1f` 三路 fresh reaudit ALL GO |
| 正式 Eval 证据链 | changed-and-verified | `0077b1f` runtime/budget 轨 Gate GO |
| 非正式付费入口 | changed-and-verified | `0077b1f` 预算/隐私轨 Gate GO |
| DeepSeek 语义校准 | **failed** | `e2cd096` 付费跑 16/49、`gate_passed=false`；¥18 自动执行头寸已耗尽 |
| 公开回归与 holdout v2 | blocked | 校准未过；禁止在未修复前再付费重跑 |
| 宿主确认、并发、UI、GitHub、公开演示 | pending | 尚未实现或发布 |
| 生产运行态 | not-applicable | 没有远端、部署或公开 URL |
| Agent 记忆 | generated-read-only | 本次未获授权写入，也未修改 |

## 付费语义校准失败（2026-07-31）

在三路 GO 基线 `0077b1f` 之上、文档检查点 `e2cd096` 上执行了公开语义校准。
私有失败产物（`results_omitted=true`，不可 attestation）：

- `artifacts/private/semantic-judge-calibration/eval-20260731t010742z-e42de5ec196b.untrusted.json`
- `attestation_kind=semantic_judge_calibration_failed`；`schema_version=2.0-untrusted`
- `source_git_commit=e2cd096…`；`run_id=eval-20260731t010742z-e42de5ec196b`

### 门禁结果（裁判质量为主因）

| 指标 | 值 |
|---|---|
| total / passed / gate | 49 / 16 / **false** |
| positive（期望 gate pass） | 11/21 ≈ 0.52 |
| adversarial（期望 gate fail） | 5/28 ≈ 0.18 |
| failed_fixture_ids | 33 |

按夹具 `kind`（不展开正文）归类，失败高度集中：

- `contradiction`：**0/7**（全部失败）
- `unsafe_prompt_injection`：**0/7**（全部失败）
- `safe_prompt_injection`：仅 2/7 通过（假阴性偏多）
- `generic` / `negation_flip`：多数失败；`safe_canonical` / `safe_paraphrase` 部分失败

结论：**校准门未过，主因是语义裁判准确率不足**，不是单纯「没跑完」。
在改 prompt / scorer（并补离线单测）之前，不应再花 DeepSeek 预算重跑 49 条。

### 协议 / 预算副作用（次因，但锁死头寸）

同一次 run 账本摘要（live SQLite，非密钥）：

| 范围 | attempt | settled_exact | uncertain | committed_cny | settled_cny | remaining_execution |
|---|---|---|---|---|---|---|
| run | 57 | 40 | **17** | ≈17.0667 | ≈0.0319 | ≈0.8059 |
| cumulative | 310 | 293 | **17** | ≈17.1941 | ≈0.1592 | ≈0.8059 |

- 17 笔 `uncertain` 的 `error_code` **全部**为 `MODEL_TRANSPORT_ERROR`；`known_cost` 为空，按预留
  `reservation_cny_per_attempt=1.002048` 计入 committed。
- 约 6 个 logical call × 规范 `DEEPSEEK_MAX_RETRIES=2`（最多 3 次尝试）放大为 17 笔
  uncertain，单次传输故障即可永久锁住约 ¥1–3 预留。
- 代码路径：`uncertain` **不能**再 `settle`；只有 `reserved → settled_*` 或
  `reserved → uncertain`。uncertain / reserved 计入 committed（见
  `DeepSeekBudgetLedger._committed_units`）。
- 当前 `remaining_execution_cny ≈ 0.81 < 1.002048`，**连下一次预留都开不出**。
  在 ¥18 自动执行上限下，本机账本等效耗尽。
- 硬上限 ¥20 理论余量 ≈2.81，仍不够覆盖「再来一轮 + 最坏 uncertain 预留」；且自动执行合同仍是 ¥18。
- **不得**为腾预算手工删改 / 二次 restore live 账本后立刻付费重跑；上一次
  `failed-calib-20260730` 已证明 restore 只会把同一传输风险再锁死一遍。

累计已结算费用约 ¥0.159（历史 ~¥0.127 + 本跑 settled ≈¥0.032）。真正锁死自动执行头寸的是
≈¥17.03 的 uncertain 预留，不是 settled 小额。

### 与 `failed-calib-20260730` 的关系

路径：`artifacts/private/phase2-reaudit/failed-calib-20260730/`（另有
`deepseek-budget.pre-restore-20260730T193513Z.sqlite3` 与
`68468a1-three-track-goa.md`）。

- 旧跑 SHA `68468a1`；`run_id=eval-20260730t193216z-…`；**49/49**
  `SEMANTIC_JUDGE_MODEL_ERROR`，passed=0——纯协议/模型错误，未测到裁判质量。
- 当时 17 笔 uncertain 锁死预算后，运维用 pre-restore 备份恢复了 live 账本
  （旧 run 已不在 live 中）。
- **本轮 live 上的 17 笔 uncertain 是 `e42de5ec` 新产生的**，不是旧跑残留。

## 最近验证

对基线 `0077b1f57ef2d5eb7155a92683041ed9e76fb38e` 的三路全新
`phase2_fresh_adversarial_reaudit` 仍为 ALL GO（见既有
`docs/testing/phase2-fresh-reaudit-*-0077b1f.md`）。三路 GO **不**因本次校准失败而撤销；
校准是后续付费门，现已失败关闭。

本轮诊断未调用 DeepSeek；新增费用为 0。

## 当前唯一执行顺序

1. ~~三路全新 `phase2_fresh_adversarial_reaudit`。~~ **已完成：`0077b1f` ALL GO。**
2. ~~付费公开语义校准 49/49。~~ **已尝试，失败（16/49）。禁止在未修复前重跑。**
3. **下一步（离线，优先）：**
   - 针对 contradiction / unsafe_prompt_injection / safe_prompt_injection 假阴阳性，
     改 `evals/semantic_judge_prompt.md` 与必要 scorer 接地规则；只加离线单测与
     `make verify`，**不**触发 DeepSeek。
   - 评估传输重试与预算交互：在 `max_retries=2` 下，运输错误会按次永久占用
     ¥1.002048；任何再次付费前必须先有可证明的降耗策略（例如失败更快关闭、
     更小诊断子集、或经明确人工批准的账本策略变更），且仍受 ¥18/¥20 约束。
4. 仅当同时满足下列条件才可考虑再次付费校准：
   - 离线修复有具体证据；
   - 本机 execution 头寸能覆盖「串行 49 次预留」且对 uncertain 最坏情况有余量
     （当前 **不满足**）；
   - 不得静默 raise execution limit 或 restore 账本来「造」头寸。
5. 校准 49/49 + validator 重算 + 预算结清 + 5 条程序性独立 GO 复核后，才跑七条公开回归。
6. 公开回归 28/28 后才封存 holdout v2（只跑一次）。

## 不可突破的恢复边界

- 总 DeepSeek 费用硬上限 ¥20；自动执行上限 ¥18。
- `.env`、预算账本、私有案例、原始 artifact、本机路径和 provider request
  ID 不进入 Git 或公开构建产物。
- 公开演示只使用合成数据和离线已验证轨迹，不部署项目 DeepSeek Key。
- 语义裁判不能覆盖工具、权限、写入、状态或确认的确定性失败。
- 最终完成前必须再由一个全新、未参与实现的智能体做完整平行审查。
- **uncertain 预留按设计永久计入 committed；不得为重跑而篡改 live 账本。**

## 当前工作区说明

代码与审查基线仍以 `0077b1f` 行为为准；文档检查点含 `e2cd096`。付费语义校准已失败；
下一步是离线裁判修复与预算策略评估，不是再次付费。
