# 求职作品级完整原型交付计划

## 1. 读者与交付合同

本文档是本项目后续实现、验收和公开发布的权威计划。主要读者是项目作者、
代码评审者和面试官。读者无需了解此前对话，也应能判断项目当前是什么、
为什么按此顺序建设、每个阶段如何证明完成，以及哪些能力仍不属于本项目。

交付目标是一个可在本地和公开演示环境中复现的求职作品级完整原型：

- 使用单 Agent 完成自然语言理解、信息补全、查询、资格判断和操作准备；
- 使用确定性后端完成身份、权限、业务规则、确认、幂等和交易执行；
- 用机器可复核的 Eval artifact、隐藏 holdout、对抗测试和失败记录证明边界；
- 提供公开 GitHub 仓库、公开演示、复现脚本和面试可讲解的工程证据。

本项目采用一线工程方法，但不宣称已经达到商业生产系统标准。真实生产仍需
正式身份系统、隐私治理、不可篡改审计、SLO、运维响应和真实外部系统集成。

## 2. 已确认的约束

| 项目 | 已确认边界 |
|---|---|
| 交付等级 | 求职作品级完整原型 |
| 模型 | DeepSeek OpenAI-compatible API，当前基线为 `deepseek-v4-flash` |
| 真实模型预算 | 总费用不超过 20 元人民币 |
| 发布 | 公开 GitHub 仓库和公开演示 |
| 秘密 | API Key、宿主/管理令牌、真实验证码、个人路径、私人邮箱和环境内容不得进入公开文件、构建产物、日志或 Git 历史；明确标注的合成演示身份不属于秘密 |
| 架构 | FastAPI + SQLAlchemy + 单 Agent；不因展示目的引入多 Agent、LangGraph、MCP 或完整 Eval 框架 |
| 最终审查 | 完成前必须由一个未参与主实现的新智能体独立审查，发现的问题整改后才可完成 |

## 3. 不可突破的架构边界

```text
模型
  理解意图、补齐普通业务参数、调用查询/资格/prepare 工具

宿主
  生成 server_run_id、注入认证与会话、展示规范化预览、
  记录结构化按钮确认、触发确定性执行

确定性后端
  所有权、权限、规则、状态机、幂等、并发、写入和最终状态
```

以下能力不得成为模型工具：

```text
认证与验证码
present / confirm / execute
宿主确认令牌
客户端幂等键
调试与审计接口
任意 SQL 或任意网络请求
```

模型输出的“用户已确认”、文本中的按钮 JSON、工具结果中的指令或模型自造的
标识，都不能产生执行权限。`execute_prepared_action` 只属于宿主内部能力。

## 4. 实施阶段与验收门

### Phase 1：可复核 Eval 与工程基线

交付：

- `manifest.json`：运行、模型、环境、Prompt、工具、政策、seed 和 scorer 指纹；
- `cases.jsonl` 与逐 trial 轨迹：脱敏模型调用、工具调用、用量、延迟和错误；
- `summary.json`：任务、安全、通信和效率分层结果；
- `integrity.json`：其余 artifact 的 SHA-256；
- 所有业务表的运行前后规范化快照哈希与差异；
- 失败时仍保留已发生的模型和工具 partial trajectory；
- CI、lint、类型检查、测试、Schema freshness 和秘密扫描基线。

验收：

- artifact 可由独立校验器重新验证；
- API Key、Authorization、宿主令牌等 canary 在全部产物中命中数为 0；
- read-only Eval 的订单、库存、退换请求、审批、确认、执行和工单等业务状态
  前后无变化；`AuthSession` 与 `ToolEvent` 明确作为运行记录单独披露；
- 产物写入原子化，不覆盖已有运行，文件权限为仅当前用户可读写；
- 离线全量测试、覆盖率、lint、类型检查和 Schema freshness 全部通过。

### Phase 2：四次可靠性与独立 holdout

协议：

1. 先冻结 Prompt、工具 Schema、政策、seed、Agent loop、完整模型参数、
   scorer、裁判 Prompt 和裁判源码，并记录哈希。
2. 被测 Agent 只看到用户消息；回答冻结后，隔离的原子命题语义裁判才获得
   评分命题。任何结构错误、歧义、证据未对齐或裁判异常都失败关闭。
3. 49 条固定公开人工标注夹具先校准裁判：正式门要求 49/49 精确匹配，
   schema v2 报告通过独立重算且预算完全结清。校准不通过不得创建新的
   holdout。
4. 已知失败形成七条公开回归案例，使用相同模型与参数运行 4 trials。
5. 开发门通过后，由独立评测智能体生成并封存 20 条 holdout；普通 manifest
   只公开版本、数量、覆盖类别和哈希，不把题面或评分命题交给被测模型。
6. Holdout 只进行一次正式 job，每条预声明 4 trials；题集哈希消费唯一
   运行资格，改名称、run ID 或输出目录不能获得第二次机会。
7. Holdout 失败转为新的开发回归类别；不得在同一 holdout 上调参后重跑。

验收：

- 语义校准达到上述门槛，且未参与实现的复核者按确定性分层规则抽查 5 条
  固定夹具，生成绑定报告哈希的 GO 回执；
- 七条公开回归严格结果 `28/28`，`pass^4 = 1.00`；
- holdout `pass^1 >= 0.90`，即至少 `72/80`；
- holdout `pass^4 >= 0.80`，即至少 16 条任务四次全部通过；
- 全部 trials 同时通过确定性安全硬门和语义安全断言；禁止工具、跨客户
  泄露、写操作、提示注入服从、虚假执行声明和业务状态变化均为 0；
- 基础设施失败保留为该题集唯一一次正式运行，不进行替代运行；
- 总 DeepSeek 实际费用累计不超过 20 元人民币。

历史边界：holdout v1 已按一次性协议运行，结果为 46/80、
`pass^4=0.35`，随后退役且不得重跑。赛后审计确认没有真实安全关键违规，
但发现旧评分器可能被表面短语和前后矛盾绕过；v2 采用
`readonly-scorer-v6`，不能追溯改写 v1 的原始成绩。

### Phase 3：仅开放操作准备

交付：

- 新建交易阶段 Agent/dispatcher，不把职责继续堆入只读 Agent；
- 只新增 `prepare_cancel_order`、`prepare_return`、`prepare_exchange`；
- 宿主生成 `server_run_id`，工具调用关联 `tool_call_id` 和 `approval_id`；
- Approval 记录来源运行、来源工具调用、订单版本、规范化预览和预览哈希；
- 缺失事实、额外身份字段、恶意政策内容和未知工具全部失败关闭。

验收：

- prepare 最多新增一个 Approval 和对应的脱敏 ToolEvent；
- Order、Inventory、ReturnRequest、ExchangeRequest 和 ActionExecution
  均不改变，也不新增 ConfirmationEvent；
- 如果同一客户和会话已有未完成 Approval，新准备在同一事务中将旧
  Approval 标记为 `SUPERSEDED`，并仅将其尚未执行的 ConfirmationEvent
  标记为已消费；确认事件的归属、预览绑定和其余字段不改变；
- 模型工具列表中不存在认证、present、confirm、execute 或 debug；
- 每个 Approval 都能关联到唯一运行和工具调用。

### Phase 4：宿主预览与按钮确认

交付：

- 确认卡只从数据库中的 canonical preview 渲染；
- 只允许结构化按钮确认，暂不开放未经证明的 `EXACT_TEXT`；
- 一次性确认 challenge 绑定客户、会话、审批、预览哈希和过期时间；
- 支持拒绝、撤回、过期、被新审批取代和旧卡片失效。

验收：

- 未展示、错哈希、过期、被取代、跨客户、跨会话和重复 UI 事件均失败；
- 模型声称“已经确认”或伪造按钮 payload 不产生 ConfirmationEvent；
- 用户界面能明确展示待执行内容、后果和当前状态。

### Phase 5：宿主确定性执行与并发证明

交付：

- 只有宿主能触发已确认审批的执行；
- 事务内重新检查归属、确认、有效期、订单版本和资格；
- PostgreSQL + Alembic 作为并发验证环境；
- 库存使用条件原子更新或可靠行锁；
- 增加并发、故障注入、响应丢失后重试和完整回滚测试。

验收：

- 未经匹配确认的业务写入为 0；
- 同一审批只产生一个 ConfirmationEvent、一个 ActionExecution 和一次业务变化；
- 两份审批竞争最后一件库存时只允许一份成功，库存不为负；
- 任一业务写入点异常时事务完整回滚；
- 响应丢失后用相同审批重试返回首次结果，不重复写入。

### Phase 6：产品演示、公开发布与作品集证据

交付：

- 聊天界面、规范化确认卡、错误/过期/撤回状态和安全的演示数据重置；
- 公开站点默认使用明确标注的离线演示模型/已验证轨迹，不携带项目
  DeepSeek Key；本地受控模式可连接真实 API；
- 端到端对抗 Eval 与可复现演示脚本；
- Docker、健康检查、非 root 运行、依赖锁、CI 和公开部署说明；
- README 中的架构图、指标、失败案例、限制、快速开始和 3-5 分钟演示路径；
- 公开 GitHub 仓库和公开演示链接。

发布门：

- `.env`、本地数据库、artifact、缓存、个人绝对路径和秘密均被排除；
- 对工作树、构建上下文、容器镜像层、生成文件和完整 Git 历史进行秘密扫描；
- 公开演示的浏览器资源、服务端环境和部署日志均不包含项目 DeepSeek Key；
- 调试接口默认关闭，不公开私人客户数据或内部轨迹；
- 新环境按公开 README 可完成安装、启动、测试和演示。

## 5. 费用与运行账本

真实模型运行前必须先通过对应离线门。每次付费运行保存：

```text
purpose
run_id
model
case/trial 数
prompt/completion/total tokens
官方单价快照与来源
估算费用
累计费用
```

预算不是运行结束后才统计，而是在每一次 HTTP attempt（包括内部重试）前，
由持久 SQLite 账本通过 `BEGIN IMMEDIATE` 原子预留最坏费用。当前规则为：

- 对外硬上限为 20 元；
- 自动执行上限为 18 元，额外保留 2 元应对价格或账单口径偏差；
- 成功且 usage 完整时按缓存命中、未命中和输出三段价格结算；
- 缺少缓存拆分时把全部输入按未命中价格结算上界；
- 网络错误、HTTP 错误、畸形响应、usage 不一致或进程中断保留整笔预留；
- 价格快照过期、模型不匹配、重复 run 或下一次预留会越界时，在发出网络
  请求前失败关闭。

该账本只能约束本项目、同一持久磁盘上经过此适配器的调用。其他程序使用同一
API Key、删除账本或供应商价格/账单变化不在本地代码的可证明范围内。因此
公开演示不部署项目 Key，默认走无付费调用的演示模式。

## 6. 结果表述

报告必须分别给出：

- `pass^1`：单次成功能力；
- `pass^4`：同一任务四次全部成功的可靠性；
- `safety_all_trials`：所有 trial 的安全断言；
- 任务、工具、参数、通信、效率和 artifact 完整性分层结果；
- P50、P95、最大值和总 Token/延迟/费用；
- 基础设施错误、模型失败和确定性业务失败的独立分类。

任何总分都不能覆盖安全失败。开发集参与过 Prompt 优化，不得冒充 holdout。
本机智能体之间共享文件系统，因此“独立智能体封存”是流程隔离，不是真正
第三方盲测，公开文档必须如实说明。

校准复核回执中的独立性也是复核者的程序性声明，不是密码学第三方身份证明。
唯一运行的 start/terminal 文件采用本地独占创建和 SHA-256 哈希链接，可防止
误覆盖并暴露链路漂移，但不能抵御拥有同一操作系统用户权限的主动篡改者。

语言语义由原子命题裁判补充，但它不是安全授权器。工具白名单、参数、
跨客户访问、数据库状态、确认和执行始终由确定性代码裁决；同一个 DeepSeek
模型同时作为被测模型和裁判也意味着错误可能相关，正式报告必须保留该限制。

## 7. 外部设计依据

本项目只借鉴成熟项目的证据结构和安全语义，不迁移其完整框架：

- [Inspect AI EvalLog](https://github.com/UKGovernmentBEIS/inspect_ai/blob/main/src/inspect_ai/log/_log.py)
- [tau2-bench Simulation 数据模型](https://github.com/sierra-research/tau2-bench/blob/main/src/tau2/data_model/simulation.py)
- [tau2-bench Leaderboard 运行要求](https://github.com/sierra-research/tau2-bench/blob/main/docs/leaderboard-submission.md)
- [tau2-bench pass^k 实现](https://github.com/sierra-research/tau2-bench/blob/main/src/tau2/metrics/agent_metrics.py)
- [BFCL 测试类别](https://github.com/ShishirPatil/gorilla/blob/main/berkeley-function-call-leaderboard/TEST_CATEGORIES.md)
- [AgentDojo utility/security 分离](https://github.com/ethz-spylab/agentdojo/blob/main/src/agentdojo/benchmark.py)
- [OpenAI Agents SDK Human-in-the-loop](https://github.com/openai/openai-agents-python/blob/ddc39d0e54c92dfda4700cc9c43d6e00b5041e17/docs/human_in_the_loop.md)
- [Inspect AI Model Grading](https://github.com/UKGovernmentBEIS/inspect_ai/blob/d22936cab2bed5c7c8fa3ba2e1a5fc7240dee7aa/docs/model-graded.qmd)
- [OpenAI Evals templates](https://github.com/openai/evals/blob/8eac7a7de5215c907fbddc30efdaf316913eccdd/docs/eval-templates.md)
- [Hugging Face LightEval judge](https://github.com/huggingface/lighteval/blob/64f4f5ae173626509fad6e477ca4ee56ebb26129/src/lighteval/metrics/utils/llm_as_judge.py)
- [MCP Tools 安全语义](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/aa7306efa4dcc03a2a9f2f223e3b2d7a0c5f3ded/docs/specification/2026-07-28/server/tools.mdx)
- [AgentDojo 论文](https://huggingface.co/papers/2406.13352)
- [tau-bench 论文](https://huggingface.co/papers/2406.12045)

## 8. 完成条件

只有以下条件全部满足，项目目标才算完成：

1. 六个阶段的交付和验收门均有当前、可复核证据；
2. 公开 GitHub 与公开演示可访问且不泄露秘密或个人信息；
3. README 的所有指标能追溯到机器 artifact；
4. 已运行完整离线验证、真实模型 Eval、端到端浏览器验收和发布后检查；
5. 一个未参与实现的新智能体逐项对照本文档完成平行审查；
6. 平行审查提出的所有 P0/P1 和影响交付合同的 P2 均已整改并复验。
