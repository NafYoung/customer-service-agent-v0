# 公开演示进度（Phase 4–6 切片）

## 读者

实现者与面试官。本文件记录公开演示垂直切片的当前状态，避免与
`docs/09_project_status.md`（回归评测进度）争写。

## 目标

在浏览器中本地跑通：

```text
离线对话 → 真实 prepare_* → canonical confirmation card
→ 结构化按钮确认 → 确定性 execute → reset
```

约束：`APP_MODE=public_demo` 时强制 `DEMO_AGENT_MODE=offline_replay`，
忽略 DeepSeek Key，不注册 `/v1` 写路由与 debug，provider HTTP 调用为 0。

## 如何运行

```bash
cd customer-service-agent-v0
source .venv/bin/activate

# 本机 HTTP 演示（关闭 Secure Cookie；Origin 需与浏览器地址一致）
APP_MODE=public_demo \
DEMO_AGENT_MODE=offline_replay \
DEMO_ALLOWED_ORIGIN=http://127.0.0.1:8000 \
DEMO_COOKIE_SECURE=false \
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

打开 `http://127.0.0.1:8000/`。

建议演示路径：

1. 发送「取消订单 ORD-1001」
2. 核对右侧确认卡（数据库 preview）
3. 点击「确认并执行」
4. 可选：点「重置演示」恢复 seed

聚焦测试：

```bash
.venv/bin/python -m pytest tests/test_public_demo.py -q
```

## 已交付

| 项 | 状态 |
|---|---|
| `APP_MODE=public_demo` 失败关闭 / 忽略 DeepSeek Key | 完成 |
| Demo BFF：session / messages / pending-action / presented / confirm / reset | 完成 |
| 确认卡仅投影 DB canonical preview；按钮确认；宿主令牌服务端 | 完成 |
| 每会话 ephemeral SQLite + seed；reset 轮换 Cookie | 完成 |
| 同域静态 UI（RIVET 品牌） | 完成 |
| `tests/test_public_demo.py` | 完成 |
| 本运行说明 | 完成 |

## 尚未完成（留给后续）

### Phase 5（并发 / 故障）

- **骨架已落**：`docs/11_phase5_concurrency_plan.md` + `tests/test_action_concurrency.py`
  （SQLite 可证：EXECUTING 认领、幂等重放、竞争 confirm 不双执行）
- 仍待：PostgreSQL + Alembic、库存竞争、故障注入与完整回滚、跨连接丢响应重试

### Phase 6 发布门（GitHub / 公开站）

- Docker 非 root、依赖锁、部署 CSP/密钥扫描
- 公开 GitHub 与托管演示链接
- README 正式 3–5 分钟演示路径与指标汇总（本切片仅 WIP 注记）
- 端到端浏览器网络抓包验收、活动会话/速率门的更完整压测

### Holdout v2（独立评测，回归 GO 后）

- 交接：`docs/handoff-holdout-v2.md`（协议：`docs/testing/readonly-holdout-v2-protocol.md`）
- **硬门**：公开回归 28/28 后由未参与实现的智能体封存并唯一正式运行；禁止重跑 v1

## 关键文件

- `app/demo/` — BFF、会话、离线回放、确认投影
- `app/static/demo/` — UI
- `app/main.py` / `app/config.py` — 模式开关
- `tests/test_public_demo.py`
- `tests/test_action_concurrency.py` — Phase 5 SQLite 并发骨架
- `docs/08_host_confirmation_public_demo.md` — 权威安全设计
- `docs/11_phase5_concurrency_plan.md` — SQLite vs PostgreSQL 边界
- `docs/handoff-holdout-v2.md` — holdout v2 独立智能体交接
