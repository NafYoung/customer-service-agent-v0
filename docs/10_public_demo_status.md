# 公开演示进度（Phase 4–6 切片）

## 读者

实现者与面试官。本文件记录公开演示垂直切片的当前状态，避免与
`docs/09_project_status.md`（回归评测进度）争写。

## 目标

在浏览器中本地跑通：

```text
对话（Preparation Agent / scripted 或 offline_replay）
→ 真实 prepare_* → canonical confirmation card
→ 结构化按钮确认 → 确定性 execute → reset
```

约束：`APP_MODE=public_demo` 时 `DEMO_AGENT_MODE` 仅允许
`preparation_scripted` 或 `offline_replay`；忽略 DeepSeek Key，不注册 `/v1`
写路由与 debug，provider HTTP 调用为 0。默认推荐 `preparation_scripted`
（真实 Preparation Agent 循环 + scripted 多轮工具）。

## 如何运行

### 本机（推荐，本地 trust / 调试）

```bash
cd customer-service-agent-v0
source .venv/bin/activate

# 本机 HTTP 演示（关闭 Secure Cookie；Origin 需与浏览器地址一致）
APP_MODE=public_demo \
DEMO_AGENT_MODE=preparation_scripted \
DEMO_ALLOWED_ORIGIN=http://127.0.0.1:8000 \
DEMO_COOKIE_SECURE=false \
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

打开 `http://127.0.0.1:8000/`。本地 trust / 付费 DeepSeek 仍用宿主 `.venv`，
**不要**把项目 Key 写进 Compose 或镜像。

### Docker（可选 public_demo profile）

```bash
# 发布卫生（只打印路径/模式名，不打印秘密值）
./scripts/check_public_demo_secrets.sh

docker compose --profile public_demo up --build public-demo
```

镜像以非 root `appuser` 运行；`.dockerignore` 排除 `.env` / `.venv` /
`artifacts` / `*.db`；Compose 的 `public_demo` profile **不**注入
`DEEPSEEK_API_KEY`。

建议演示路径：

1. 发送「取消订单 ORD-1001」
2. 核对右侧确认卡（数据库 preview）
3. 点击「确认并执行」
4. 可选：点「重置演示」恢复 seed

聚焦测试：

```bash
.venv/bin/python -m pytest tests/test_public_demo.py \
  tests/test_demo_preparation_integration.py -q
.venv/bin/python -m pytest \
  tests/test_action_concurrency.py \
  tests/test_public_demo.py \
  tests/test_api_actions.py \
  tests/test_demo_preparation_integration.py -q
```

## 已交付

| 项 | 状态 |
|---|---|
| `APP_MODE=public_demo` 失败关闭 / 忽略 DeepSeek Key | 完成 |
| Demo BFF：session / messages / pending-action / presented / confirm / reset | 完成 |
| 确认卡仅投影 DB canonical preview；按钮确认；宿主令牌服务端 | 完成 |
| `DEMO_AGENT_MODE=preparation_scripted` 经 Preparation Agent 写出 pending | 完成 |
| 每会话 ephemeral SQLite + seed；reset 轮换 Cookie | 完成 |
| 同域静态 UI（RIVET 品牌） | 完成 |
| `tests/test_public_demo.py` + `tests/test_demo_preparation_integration.py` | 完成 |
| Docker 非 root + 精简 COPY + `.dockerignore` 密钥卫生 | 完成 |
| `scripts/check_public_demo_secrets.sh` | 完成 |
| Compose `public_demo` profile（无 DeepSeek Key） | 完成 |
| Render 公网 Demo URL | 完成（https://rivet-public-demo.onrender.com/） |
| 本运行说明 | 完成 |

## 尚未完成（留给后续）

### Phase 5（并发 / 故障）

- **骨架已落**：`docs/11_phase5_concurrency_plan.md` + `tests/test_action_concurrency.py`
  （SQLite 可证：幂等重放、竞争 confirm 不双执行；2/2 通过）
- 仍待：PostgreSQL + Alembic、库存竞争、故障注入与完整回滚、跨连接丢响应重试

### Phase 6 发布门（GitHub / 公开站）

清单：`docs/12_phase6_publish_checklist.md`。本地卫生脚本：

```bash
./scripts/check_public_demo_secrets.sh
./scripts/check_publish_preflight.sh
```

公网 URL 已回填 README。Render 改 `DEMO_AGENT_MODE` 后需 Manual Deploy 一次。

步骤：`docs/13_hosted_demo_render.md`。

### Holdout（独立评测）

- v1 / v2 均已唯一正式运行并**退役**；禁止同题重跑。
- v2 聚合 FAIL 与加固：`docs/testing/holdout-v2-postmortem.md`。
- 新盲测需新 `case_set` + 重绑校准 + 另授权。
## 关键文件

- `app/demo/` — BFF、会话、scripted Preparation runner、离线回放、确认投影
- `app/static/demo/` — UI
- `app/main.py` / `app/config.py` — 模式开关
- `Dockerfile` / `docker-compose.yml` / `.dockerignore`
- `scripts/check_public_demo_secrets.sh`
- `scripts/check_publish_preflight.sh`
- `tests/test_public_demo.py`
- `tests/test_demo_preparation_integration.py`
- `tests/test_action_concurrency.py` — Phase 5 SQLite 并发骨架
- `docs/08_host_confirmation_public_demo.md` — 权威安全设计
- `docs/11_phase5_concurrency_plan.md` — SQLite vs PostgreSQL 边界
- `docs/12_phase6_publish_checklist.md` — 公开发布检查表
- `docs/13_hosted_demo_render.md` — Render 托管步骤
- `docs/testing/holdout-v2-postmortem.md` — holdout v2 FAIL 归因
- `render.yaml` — Render Blueprint
- `docs/handoff-holdout-v2.md` — holdout v2 独立智能体交接（题集已退役）
