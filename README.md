# RIVET · Customer Service Agent v0

鞋服电商售后的**有界单 Agent 作品原型**：语言理解与交易执行分离；写操作走  
`prepare → 宿主展示 → 可信确认 → execute`，模型拿不到执行权限。

> **不是生产系统。** 数据全为合成样例。详细文档见 [`docs/README_DETAILED.md`](docs/README_DETAILED.md)。

## 一句话架构

```text
用户 → Host UI
        ├─ 只读 Agent（精确 6 工具）→ DeepSeek（OpenAI-compatible）
        ├─ Preparation Agent（精确 9 工具，只 prepare，不 execute）
        └─ 宿主确认令牌 → 确定性后端执行（幂等）
```

- FastAPI + SQLAlchemy + SQLite；跨客户隔离  
- 取消 / 退货 / 换货：确定性资格规则 + 版本化政策  
- Eval：开发集 / 公开回归 / holdout（含失败后归因复验）+ 持久预算闸门  
- CI：`ruff` · `mypy` · 覆盖率 · Schema freshness · `pip-audit` · Gitleaks  

## 评测结果（面试可讲）

| 门 | 结果 | 说明 |
|---|---|---|
| 开发集 10×4 | 40/40，`pass^4=1.00` | 参与过 Prompt 优化，不能冒充 holdout |
| 语义校准 #4 | 49/49 | 隔离裁判 |
| 公开回归 7×4 | 28/28，`pass^4=1.00` | 加固后现役 |
| holdout v1 | 46/80，`pass^4=0.35` | 已退役；无真实安全写入违规 |
| holdout v2 | 44/80，`pass^4=0.40` | 已退役；见 [postmortem](docs/testing/holdout-v2-postmortem.md) |
| 业务写入（只读门） | 0 | 工具白名单 + 状态哈希 |

诚实叙事：**holdout 未过门** → 归因 → 修复 → 用公开 7×4 证明不回退。不是把 FAIL 改写成 PASS。

## 在线演示（Render）

公网托管步骤见 [`docs/13_hosted_demo_render.md`](docs/13_hosted_demo_render.md)
（GitHub 仓库 + Render Free Web Service；**不是** GitHub Pages）。

**Demo：** https://rivet-public-demo.onrender.com/

Free 档闲置约 15 分钟会休眠，首次打开可能需等待约 1 分钟。

## 3 分钟本地演示（无需 API Key）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
test -f .env || cp .env.example .env

APP_MODE=public_demo \
DEMO_AGENT_MODE=preparation_scripted \
DEMO_ALLOWED_ORIGIN=http://127.0.0.1:8000 \
DEMO_COOKIE_SECURE=false \
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

打开 http://127.0.0.1:8000/ →「取消订单 ORD-1001」→ 核对确认卡 →「确认并执行」或「拒绝」。
消息经 **Preparation Agent**（scripted 多轮工具，零外网）写出 pending；确认与执行仍由宿主完成。模糊意图（如「我想退货」）会先补问订单号。

本地 live DeepSeek（**禁止**公开部署；走预算闸门，累计硬上限见项目规则）：

```bash
APP_MODE=local \
DEMO_AGENT_MODE=preparation_live \
DEMO_ALLOWED_ORIGIN=http://127.0.0.1:8000 \
DEMO_COOKIE_SECURE=false \
DEEPSEEK_API_KEY=... \
HOST_CONFIRMATION_TOKEN=... \
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## 开发运行

```bash
# 建议 Python ≥ 3.11
pip install -r requirements-dev.txt
cp .env.example .env   # 设置 HOST_CONFIRMATION_TOKEN；live eval 再加 DEEPSEEK_API_KEY
uvicorn app.main:app --reload --env-file .env
```

- API：http://127.0.0.1:8000/docs  
- 演示账号：`linfan@example.com` / `246810`  
- 测试：`pytest` · `make verify`  

更多（架构图、取消全流程 curl、Eval 协议、文档索引）→ [`docs/README_DETAILED.md`](docs/README_DETAILED.md) · [`docs/09_project_status.md`](docs/09_project_status.md)
