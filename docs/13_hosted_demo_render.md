# 托管演示 · Render（A4）

## 目标

在公网 HTTPS 上跑现有 `public_demo`（offline replay、无 DeepSeek Key），
仓库仍在 GitHub；运行时由 [Render](https://render.com) Free Web Service 承载。

GitHub Pages **不能**跑 FastAPI；本方案是「代码在 GitHub、演示站在 Render」。

## 一次部署（约 10–15 分钟）

1. 注册 / 登录 [Render](https://dashboard.render.com)，连上 GitHub 账号。
2. **New → Blueprint**，选仓库 `NafYoung/customer-service-agent-v0`，
   Blueprint 文件为根目录 `render.yaml`。
3. Apply。等待首次 Docker build（数分钟）。
4. 打开服务 URL（形如 `https://rivet-public-demo.onrender.com`）。
5. 走演示路径：取消 `ORD-1001` → 确认卡 →「确认并执行」。
6. 把该 URL 写进：
   - 仓库 About → Homepage
   - `README.md`「在线演示」一行
   - （可选）显式设置环境变量 `DEMO_ALLOWED_ORIGIN=https://…`
     （不设时应用会读 Render 注入的 `RENDER_EXTERNAL_URL`）

## 环境变量（Blueprint 已写好）

| 变量 | 值 | 说明 |
|---|---|---|
| `APP_MODE` | `public_demo` | 强制公开演示 |
| `DEMO_AGENT_MODE` | `offline_replay` | 禁止 live 模型 |
| `DEMO_COOKIE_SECURE` | `true` | HTTPS Cookie |
| `ENABLE_DEBUG_ROUTES` | `false` | 关闭调试 |
| `HOST_CONFIRMATION_TOKEN` | *generate* | 仅服务端；浏览器不可见 |
| `DEMO_ALLOWED_ORIGIN` | （可空） | 空则用 `RENDER_EXTERNAL_URL` |

**不要**在 Render 上配置 `DEEPSEEK_API_KEY`。

## 验收

- [ ] `/health` 返回正常
- [ ] 浏览器完成取消 ORD-1001
- [ ] DevTools Network **无** DeepSeek 出站请求
- [ ] 响应 / 日志无 API Key 子串
- [ ] 闲置后冷启动：首请求可能 30–60s，属 Free 档预期

## 本地对照

```bash
docker compose --profile public_demo up --build public-demo
```

## 相关文件

- `render.yaml` — Blueprint
- `Dockerfile` — 监听 `${PORT:-8000}`
- `app/config.py` — `resolve_demo_allowed_origin`
- `docs/08_host_confirmation_public_demo.md` — 安全设计
- `docs/12_phase6_publish_checklist.md` — Phase 6 清单
