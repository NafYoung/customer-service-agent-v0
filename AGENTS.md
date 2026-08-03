# RIVET 项目规则

## 定位

本项目是鞋服电商售后的求职作品级完整原型：模型负责自然语言理解、查询和
操作准备，确定性后端负责身份、权限、业务规则、确认、幂等与交易执行。
不得把它表述为生产系统或端到端自治客服。

## 环境与验证

- Python 3.11+；所有开发依赖只安装到项目 `.venv`。
- 安装：`.venv/bin/python -m pip install -r requirements-dev.txt`
- 完整离线门：`make verify PYTHON=.venv/bin/python`
- Reference Eval：`.venv/bin/python evals/run_reference_evals.py`
- Schema：`.venv/bin/python scripts/export_contracts.py --check`

## 安全边界

- 不读取、输出、提交或复制 `.env`、API Key、宿主令牌、预算账本和私有
  Eval artifact。
- 真实 DeepSeek 调用必须经过持久预算闸门；累计硬上限为人民币 20 元。
- 公开演示不得携带 DeepSeek Key，不得产生模型网络出口。
- 模型永远不能获得认证、`present`、`confirm`、`execute`、debug 或任意
  SQL/网络工具。
- holdout v1 已退役，禁止重跑；新 holdout 只允许一次正式运行。

## 代码约定

- 保持单 Agent、阶段白名单和确定性交易后端；不为展示引入多 Agent、
  LangGraph、MCP 或完整 Eval 框架。
- 修改行为时同步测试、类型、Schema、README 和对应 `docs/` 合同。
- 评分中工具、权限、状态和写入由代码硬判；LLM 裁判只补充语言语义，
  任何无效或歧义裁判结果都失败关闭。
- 使用 `apply_patch` 做手工文件编辑；保留用户已有改动，不做破坏性清理。

## 目录

- `app/`：API、Agent、工具与确定性服务
- `evals/`：案例、评分器、预算与证据包
- `tests/`：单元、集成、对抗和协议测试
- `docs/`：业务、架构、评测与交付合同
- `policies/`：合成版本化政策

## 当前状态

现役进度、验证证据和唯一恢复顺序见
`docs/09_project_status.md`；阶段验收合同见
`docs/06_portfolio_completion_plan.md`。不要从旧会话或历史 artifact
推断当前完成度。

## Cursor Cloud specific instructions

单进程 Python 应用（FastAPI + 内嵌 SQLite），无独立前端/数据库服务。启动脚本已
建好 `.venv` 并装好 `requirements-dev.txt`，直接用 `.venv/bin/python` 即可。

- 系统依赖：创建 `.venv` 需要 `python3.12-venv`（Debian/Ubuntu 默认没有），环境构建
  已安装；本机无网重建 venv 时若报 `ensurepip is not available`，即缺此包。
- 离线门与运行命令见根 `README.md` 与 `Makefile`（`make verify`/`make run`/`make test`）；
  Reference Eval 见 AGENTS「环境与验证」。全部离线，无需 API Key。
- 公开演示（浏览器端到端，推荐）：`APP_MODE=public_demo`、
  `DEMO_AGENT_MODE=preparation_scripted`、`DEMO_ALLOWED_ORIGIN=http://127.0.0.1:8000`、
  `DEMO_COOKIE_SECURE=false` 四个变量必须内联传入 uvicorn（完整命令见 README）。此模式用
  **内存 SQLite（重启即丢）**，且拒绝 live DeepSeek，`provider_http_calls` 恒为 0。
- 用 curl 打 `/demo/*` 时的非显然点：写请求必须带 `Origin` 头、会话 Cookie，以及
  CSRF 头 `X-CSRF-Token`（值取自 `POST /demo/session` 返回的 `csrf_token`），否则报
  `CSRF_FORBIDDEN`。流程：`session → messages → pending-action → presented → confirm`。
- `local` 模式（`make run`，带 Swagger `/docs`）用文件库 `./customer_service.db` 并自动播种；
  要跑 present/confirm/execute 写流程需在 `.env` 设 `HOST_CONFIRMATION_TOKEN`。
