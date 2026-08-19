# 项目现役状态

最后核对：2026-08-19（P1-1 转人工闭环接线：reject/会话限额/预算耗尽 → SupportTicket；此前 2026-08-18 行业对标章节；2026-08-03 宿主 UI 接 Preparation Agent / Render Demo URL 回填）

本地分支：以当前 Git 为准

最近已提交检查点：与本文件同 Git 提交

Preparation Agent 检查点：`1b034cd`（核心）；宿主 UI 接入见本分支

离线修复基线（prompt v2）：`56c5c6f`

账本授权提交：`b6d5e5b`

第一次付费失败记录：`5ef6180`（19/49）

离线 v3 加固：`72f3de7`；付费 #2 失败：`8de406d`（27/49）

离线 v4 加固：`4f41a35`（`atomic-claims-v4`）

holdout 失败后加固链：`9337e55`（prompt/回归）→ `84eca79` / `69030d2` / `f7f221a`（语义裁判恢复 + 短语）

本文是项目恢复工作的现役入口。阶段验收标准仍以
`docs/06_portfolio_completion_plan.md` 为准；历史结果保留在对应
`docs/testing/` 报告中。

## 当前结论

| 事实面 | 状态 | 证据 |
|---|---|---|
| 确定性后端与只读 Agent | verified-current | 完整离线门通过；Reference Eval 8/8 |
| Eval 证据与预算闸门 | verified-current | 开发集 40/40；live 账本可用 |
| holdout v1 | verified-current / retired | 唯一正式结果 46/80、`pass^4=0.35`；禁止重跑 |
| Preparation Agent | **verified-current** | 核心 `1b034cd`；公开 UI `preparation_scripted`；本地可选 `preparation_live` |
| 宿主确认、并发、UI、公开演示 | **updated** | reject/tool_trace/补槽 + 浏览器手工验收；Demo https://rivet-public-demo.onrender.com/ （仍 `preparation_scripted`）；Phase 5 Postgres 仍待 |
| 转人工闭环（demo 宿主） | **updated** | reject / 会话限额 / live 预算耗尽 → 落 `SupportTicket` 并回传工单号（按原因去重）；Agent 保持精确 9 工具；live 预算 run_id 派生修复 |
| 原子命题语义门 | **verified-current** | `atomic-claims-v4`；校准 **#4** @ `8884b1a` 49/49 |
| DeepSeek 语义校准 | **passed** | #4：`eval-20260731t080100z-4d65de51789c` **49/49**；review GO |
| 公开回归 7×4（holdout 绑定历史） | historical | `eval-20260731t080946z-dd64553ceb3e` **28/28** @ `8884b1a` |
| 公开回归 7×4（加固后现役） | **passed** | `eval-20260731t102036z-9be142ce84ec` **28/28** @ `f7f221a`；`pass^4=1.00`；业务写入 0 |
| holdout v2 | **failed / retired** | 唯一正式跑 44/80、`pass^4=0.40`；禁止同题集重跑；见 `docs/testing/holdout-v2-postmortem.md` |
| 作品集对外叙事 | **updated** | README 指标与 holdout FAIL→加固→复验对齐；README + `README_DETAILED` 新增行业对标章节（per-resolution 成本 / Air Canada 判例 / 仅退款退潮背景）；`docs/12_phase6_publish_checklist.md` |
| 架构决策记录 | **updated** | `docs/14_architecture_decisions.md`：单 Agent / 确定性后端 / 结构化政策 / 原子命题裁判 / 自研评测 / 预算闸门 |
| 指标口径与合规叙事 | **updated** | README + `evals/README.md` 补充 resolution/deflection/handoff 口径定义与成本换算；演示 UI 增加「本回复由 AI 生成」标识；README 增 PIPL/PCI 声明 |
| 公开 GitHub | **created** | https://github.com/NafYoung/customer-service-agent-v0 |
| 生产运行态 | not-applicable | 托管演示为作品原型，非生产 |

## 预算账本策略

| 项 | 值 |
|---|---|
| 规范 live 路径 | `artifacts/private/deepseek-budget.sqlite3` |
| 正式硬上限 | ¥20（未上调） |
| 自动执行上限 | ¥18（未上调） |
| 价格快照 | 2026-08-19 刷新：官方定价改为空闲/高峰双档；按**高峰档保守上界**录入（缓存命中 0.10 / 未命中 3.0 / 输出 9.0 元每百万）；`pricing/deepseek-v4-flash-2026-08-19.json`，`valid_until` 2026-08-26 |
| 当前 remaining_execution | 以 live 账本为准（加固后多次公开回归后仍远高于执行闸） |
| reserved / uncertain | 以 live 账本为准 |

账本注记（owner-only）：

1. 公开回归前：17 笔无 usage 的 HTTP/传输失败 → `voided`（备份
   `…pre-void-20260731T074227Z…`）。
2. R1（用户授权）：中断 holdout / 误开回归上的 2 reserved + 1 uncertain →
   `voided`（备份 `…pre-void-20260731T082510Z…`，审计
   `budget-void-audit-20260731T082510Z.md`）。
3. `fc45c41` 起快照将 `voided` 排除出 committed 与证据桶。

## holdout v2（2026-07-31；`8884b1a`；R1 后唯一正式跑）

| 指标 | 值 |
|---|---|
| run id | `eval-20260731t090131z-b093a07fad66` |
| total / passed | 80 / **44** |
| `pass^1` | **0.55**（门槛 ≥0.90） |
| `pass^4` | **0.40**（8/20；门槛 ≥0.80） |
| security | 63/80；`all_trials_passed=false` |
| business state changes | **0** |
| run settled_cny | ≈**0.10435** |
| cumulative settled_cny | ≈**0.40253** |
| source git commit | `8884b1a` |
| gate | **FAIL** |
| lifecycle | **retired**；`rerun_policy=prohibited` |

公开脱敏投影：`evals/readonly_holdout_v2.manifest.json`。

私有产物（owner-only）：证据包
`artifacts/private/eval-runs/eval-20260731t090131z-b093a07fad66/`；
`verify_eval_bundle.py` →
`VALID: … (44/80 strict trials) with complete formal receipt chain`。

证据边界：本机职责/时序隔离；用户授权 R1 清锁重封后的唯一正式运行——**不是**
密码学第三方盲测。首次 start（`…081633z…`）因并行中断已归档至
`artifacts/private/holdout/r1-retired-20260731T082510Z/`，不算可重开的成功路径。

聚合失败聚类与 Prompt/回归/裁判加固见
`docs/testing/holdout-v2-postmortem.md`。**v2 题集仍退役；新盲测需新
case_set + 另授权。**

## 付费语义校准 #4（2026-07-31；绑裁判 @ `8884b1a`）

| 指标 | 值 |
|---|---|
| run id | `eval-20260731t080100z-4d65de51789c` |
| total / passed / gate | 49 / **49** / **true** |
| positive / adversarial | 1.00 / 1.00 |
| `semantic_judge_source_sha256` | `2fcda13d…`（#4 当时 HEAD） |
| review | GO；`reviewer_id=rebind-calib-reviewer-20260731`；5 条分层样本 |

私有报告/回执：`artifacts/private/semantic-judge-calibration/eval-20260731t080100z-4d65de51789c.{json,review.json}`。

说明：后续语义裁判恢复补丁（`84eca79` / `69030d2` / `f7f221a`）改变了
`semantic_judge_source_sha256`；#4 仍是 holdout 封存当时的有效校准。若再开新
holdout，需在干净树上重绑校准 + 同提交公开回归。

## 公开回归（加固后现役；`f7f221a`）

| 指标 | 值 |
|---|---|
| run id | `eval-20260731t102036z-9be142ce84ec` |
| total / passed / `pass^4` | 28 / **28** / **1.00** |
| business state changes | **0** |
| source git commit | `f7f221a` |
| purpose | `dev_repeat`；case-set `readonly-regression-v1` |

较早同形状绿跑（holdout 绑定历史）：`eval-20260731t080946z-dd64553ceb3e` @
`8884b1a`。中间加固过程中的 26/28、27/28 尝试仅作诊断，不算现役门。

## 当前唯一执行顺序

1. ~~付费校准 49/49（裁判重绑）。~~ **#4 完成（holdout 当时）。**
2. ~~同提交公开回归 28/28。~~ **holdout 当时完成；加固后再次 28/28。**
3. ~~holdout v2 唯一正式运行。~~ **已跑；未过门；题集退役。**
4. ~~holdout 失败后 Prompt/回归/裁判加固 + 公开 7×4 复验。~~ **完成。**
5. ~~作品集叙事收口（README + Phase 6 清单）。~~ **完成（本地）。**
6. Phase 6：公开仓已创建并 push；可选托管演示仍待选平台（见 `docs/12_phase6_publish_checklist.md` §D）。
7. 可选（需新授权）：新 holdout 题集（新 `case_set_sha256`，先重绑校准）；**禁止**同题集调参重跑。

## 不可突破的恢复边界

- 总 DeepSeek 费用硬上限 ¥20；自动执行上限 ¥18。
- 模型永远不能获得认证、`present`、`confirm`、`execute`、debug 或任意 SQL/网络工具。
- holdout v1 / v2 均已退役，禁止同题重跑。
- 不为展示引入多 Agent、LangGraph、MCP 或完整 Eval 框架。
