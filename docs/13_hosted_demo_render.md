# 托管演示 · Render（A4）

## 目标

在公网 HTTPS 上跑现有 `public_demo`（**默认** `preparation_scripted`、无
DeepSeek Key），仓库仍在 GitHub；运行时由 [Render](https://render.com)
Free Web Service 承载。

GitHub Pages **不能**跑 FastAPI；本方案是「代码在 GitHub、演示站在 Render」。

## 一次部署（约 10–15 分钟）

1. 注册 / 登录 [Render](https://dashboard.render.com)，连上 GitHub 账号。
2. **New → Blueprint**，选仓库 `NafYoung/customer-service-agent-v0`，
   Blueprint 文件为根目录 `render.yaml`。
3. Apply。等待首次 Docker build（数分钟）。
4. 打开服务 URL（形如 `https://rivet-public-demo.onrender.com`）。
5. 走演示路径：取消 `ORD-1001` → 确认卡 →「确认并执行」。
6. 把该 URL 写进仓库 About → Homepage（README 已回填）。

若 Blueprint 环境变量已从 `offline_replay` 改为 `preparation_scripted`，
在 Render Dashboard 对服务执行一次 **Manual Deploy**。

## 环境变量（Blueprint 已写好）

| 变量 | 值 | 说明 |
|---|---|---|
| `APP_MODE` | `public_demo` | 强制公开演示 |
| `DEMO_AGENT_MODE` | `preparation_scripted` | 真实 Preparation Agent + scripted 多轮；零外网。对照可用 `offline_replay` |
| `DEMO_COOKIE_SECURE` | `true` | HTTPS Cookie |
| `ENABLE_DEBUG_ROUTES` | `false` | 关闭调试 |
| `HOST_CONFIRMATION_TOKEN` | *generate* | 仅服务端；浏览器不可见 |
| `DEMO_ALLOWED_ORIGIN` | `https://rivet-public-demo.onrender.com` | 锁定同源；亦可空（用 `RENDER_EXTERNAL_URL`） |

**不要**在 Render 上配置 `DEEPSEEK_API_KEY`。`public_demo` 拒绝任何 live 模式。

### `preparation_scripted` vs `offline_replay`

| 模式 | 消息路径 | 外网 | 说明 |
|---|---|---|---|
| `preparation_scripted` | `PreparationAgent.run`（scripted turns） | 0 | 默认；面试可讲「UI 接 Preparation Agent」 |
| `offline_replay` | 直调 `prepare_*` | 0 | 更短路径对照；仍走同一确认卡 |
| `preparation_live` | 真实 DeepSeek（预算闸门） | ≥1 | **仅本地**；`public_demo` / Render 禁止 |

## 验收

- [x] `/health` 返回正常（含 `demo_agent_mode`）
- [x] 浏览器完成取消 ORD-1001
- [ ] DevTools Network **无** DeepSeek 出站请求（部署后复验）
- [ ] 响应 / 日志无 API Key 子串
- [ ] 闲置后冷启动：首请求可能 30–60s，属 Free 档预期

**现役 Demo：** https://rivet-public-demo.onrender.com/

## 本地对照

```bash
APP_MODE=public_demo \
DEMO_AGENT_MODE=preparation_scripted \
DEMO_ALLOWED_ORIGIN=http://127.0.0.1:8000 \
DEMO_COOKIE_SECURE=false \
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 或
docker compose --profile public_demo up --build public-demo
```

## 相关文件

- `render.yaml` — Blueprint
- `Dockerfile` — 监听 `${PORT:-8000}`
- `app/demo/preparation_runner.py` — scripted Preparation Agent 接入
- `app/config.py` — `resolve_demo_allowed_origin`
- `docs/08_host_confirmation_public_demo.md` — 安全设计
- `docs/12_phase6_publish_checklist.md` — Phase 6 清单
