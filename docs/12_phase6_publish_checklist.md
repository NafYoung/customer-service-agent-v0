# Phase 6 公开发布清单

## 读者与目的

实现者在首次 push / 托管前对照本清单。目标是公开 GitHub 仓库 + 可选托管
演示，且不泄露 Key、账本、私有 Eval artifact 或本机路径。

权威验收合同仍以 `docs/06_portfolio_completion_plan.md` §Phase 6 为准；
本文件是可执行检查表，**不**自动创建远程仓库或部署。

## 当前状态（2026-07-31）

| 项 | 状态 |
|---|---|
| 本地 `public_demo` 离线切片 | 可跑 |
| Docker `public_demo` profile（无 DeepSeek Key） | 可跑 |
| 发布卫生脚本 | `scripts/check_public_demo_secrets.sh` + `scripts/check_publish_preflight.sh` |
| 公开 GitHub URL | **未创建**（需作者授权 push） |
| 托管演示 URL | **未部署** |
| README 指标与 holdout 叙事 | 已与 `docs/09` 对齐 |

## A. 发布前卫生（本地，免费）

```bash
# 1) 演示 / 镜像上下文：路径级密钥卫生（不打印秘密值）
./scripts/check_public_demo_secrets.sh

# 2) 公开发布预检：忽略规则、工作树、敏感路径名
./scripts/check_publish_preflight.sh

# 3) 完整离线门（发布前建议再跑）
make verify PYTHON=.venv/bin/python
```

必须为真：

- `.env`、`artifacts/`、`*.db`、`.venv/`、`data/` 在 `.gitignore` 与
  `.dockerignore` 中；
- 工作树无未忽略的私有 artifact / 账本 / Key 文件；
- Compose / Dockerfile **不**注入 `DEEPSEEK_API_KEY`；
- 镜像以非 root `appuser` 运行。

可选（有工具时）：

```bash
# Git 历史秘密扫描（CI 已配置 Gitleaks；本地有 CLI 时）
gitleaks detect --source . --no-git || true
```

## B. 对外叙事自检（fresh-reader）

打开 README 首页，无上下文读者应能在 60 秒内看到：

1. 这是求职原型，不是生产客服；
2. 模型不做 auth / confirm / execute；
3. 关键评测数字（含 holdout FAIL）与「失败→加固→复验」一句；
4. 3–5 分钟本地演示命令。

禁止：

- 把开发集或公开回归冒充 holdout；
- 把已退役 holdout 说成通过；
- 粘贴私有 run 轨迹、题面、预算账本余额精确值（可用「远低于 ¥18」量级表述）。

## C. 首次公开 GitHub（需作者授权）

未获「创建远程 / push」授权前，停在本机提交。授权后建议顺序：

1. 确认 `git status` 干净，且无 `artifacts/`、`.env` 被 track；
2. 创建空的 public 仓库（不要用「含 README 的初始化」覆盖本地历史，除非你有意）；
3. `git remote add origin <url>`；
4. `git push -u origin main`（或你指定的默认分支）；
5. 在仓库 About / README 顶部填入 clone URL；托管演示 URL 另栏填写。

公开仓只含合成数据、公开回归题面、脱敏 holdout manifest 投影；原始
`artifacts/private/**` 永不提交。

## D. 托管演示（可选，需作者选平台）

推荐约束：

- 默认 `APP_MODE=public_demo` + `DEMO_AGENT_MODE=offline_replay`；
- **不**注入项目 DeepSeek Key；
- `DEMO_COOKIE_SECURE=true`（HTTPS）；
- `DEMO_ALLOWED_ORIGIN` 与公开 Origin 完全一致；
- 调试路由保持关闭。

验收：浏览器走完取消 ORD-1001；Network 中无 DeepSeek 出口；响应/日志无
Key 子串。

## E. 发布后检查

- [ ] clone 到干净目录可 `pip install -r requirements-dev.txt` +
  `make verify`（或至少 pytest 聚焦门）；
- [ ] 按 README 启动 `public_demo`；
- [ ] 再跑 `./scripts/check_publish_preflight.sh`；
- [ ] README 指标仍能追溯到 `docs/09` / 公开 manifest，而非私有路径。

## F. 明确不在本清单内

- 同题重跑 holdout v1 / v2；
- 未授权的新 holdout 正式跑；
- 上调 ¥20 / ¥18 预算；
- 引入多 Agent / LangGraph / MCP。

## 相关文件

- `docs/06_portfolio_completion_plan.md` — Phase 6 验收合同
- `docs/08_host_confirmation_public_demo.md` — 零密钥演示安全设计
- `docs/09_project_status.md` — 现役评测证据
- `docs/10_public_demo_status.md` — 本地演示进度
- `docs/testing/holdout-v2-postmortem.md` — holdout FAIL 聚合归因
- `scripts/check_public_demo_secrets.sh`
- `scripts/check_publish_preflight.sh`
