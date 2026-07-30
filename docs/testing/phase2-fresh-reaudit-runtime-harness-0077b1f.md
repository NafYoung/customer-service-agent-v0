# Phase 2 新鲜对抗复审 — Runtime / Capability / Source / Harness

独立对抗审查。审查者**未**实现被审代码。仅报告；无生产改动；无 DeepSeek 调用；无密钥输出。

## 审查 SHA

- 要求：`0077b1f57ef2d5eb7155a92683041ed9e76fb38e`
- 核实：`git rev-parse HEAD` → `0077b1f57ef2d5eb7155a92683041ed9e76fb38e`
- 审查开始时工作树：干净（`git status --porcelain` 为空）
- 写报告前：HEAD 仍匹配；除本报告外无其它工作树改动意图
- HEAD 说明：`fix: seal formal send path and bind failure ledger evidence`

## 范围

**仅**跟踪：`holdout_formal` 的 runtime / capability / source / harness 身份绑定。

先验 NO-GO 上下文：
`docs/testing/phase2-fresh-reaudit-runtime-harness-ac6ccd8.md`（SHA `ac6ccd8`）。

须核实闭合、并继续挖洞的检查项：

1. 封印 `sealed_httpx_client_id` + `sealed_httpx_transport_id` + `sealed_httpx_mounts`
2. 无实例 `MethodType` 遮蔽 `Client.send` / `.post` / `.request`
3. 兄弟 `HTTPTransport` 对象替换被拒
4. `_mounts` MockTransport 注入被拒
5. 先验整 client 替换 / `transport_mode` 撒谎 / ledger·price 重绑仍闭合
6. 类级 `HTTPTransport.handle_request` monkeypatch：仍为残余，或已新闭合

范围外：案例集质量、LLM 裁判标定语义、预算账本隐私轨、同 OS 用户任意 FS/RCE 作为声称 TCB。

## 方法

1. 确认 SHA + 干净工作树（停止规则已满足）。
2. 阅读 `ValidatedFormalExecutionCapability` 封印字段、`_seal_httpx_mounts`、
   `_require_bound_formal_runtime_objects`、`transport_mode_for_client`、
   `_source_fingerprints`、TDD（`formal-runtime-capability-binding.tdd.md`）与
   对抗测试。
3. 离线谓词探针（无 provider I/O）：issue 等价封印后篡改通道 / 方法 / 预算图，
   再以 sealed ids 调用 `_require_bound_formal_runtime_objects`。
4. 运行：
   `.venv/bin/python -m pytest tests/test_dev_repeat_paid_gate.py -k
   'httpx_client_swap or sibling_http_transport or client_method_shadow or
   mounts_mock_injection or transport_mode_lie or budget_graph_rebinding or
   formal_execution_capability or formal_model_public_runtime_config' -q`
   → **23 passed**, 107 deselected。

## 先验发现闭合情况

| 先验 | 主张 | `0077b1f` 状态 |
|---|---|---|
| ac6ccd8 P0 | Issue 后整对象 `_client` 替换 / MockTransport 通道 | **仍闭合** — `sealed_httpx_client_id`；MockTransport → `live_transport_mode=="custom"`。离线：`client_swap BINDING_REJECT`；测试 `httpx_client_swap`。 |
| ac6ccd8 P0 变体 | 自定义 transport + `_transport_mode = "default"` 撒谎 | **仍闭合** — live 推导；可写属性无效。离线：`transport_mode_lie BINDING_REJECT`；测试 `transport_mode_lie`。 |
| ac6ccd8 P1（阻断） | 未封印 send 路径 / transport 对象 id / mounts；实例 `send`/`post` 遮蔽与兄弟 `HTTPTransport` / mounts 注入可通过绑定 | **已闭合** — 见下节矩阵。 |
| ac6ccd8 P1 预算 | Issue 后 `_ledger` / `_price_snapshot` 重绑 | **仍闭合** — 封印对象 id。离线：`ledger_swap`/`price_swap BINDING_REJECT`；测试 `budget_graph_rebinding`。 |
| ac6ccd8 P2 | 中间目录 symlink 进入 `source_tree_sha256` | **代码仍闭合** — `os.walk(..., followlinks=False)` + symlink 目录/文件剔除 + `O_NOFOLLOW` 读。**覆盖缺口仍在：** 无专门对抗单测锁住中间目录 symlink。 |
| (capability) | Actor / judge / harness / model+guard 方法替换 | **仍失败关闭** — 实例 `model.complete` → `BINDING_REJECT`；既有 capability 测试通过。 |

## 本 SHA 发现

### 闭合 — 封印 client + transport 对象 id + mounts，并拒绝 send 路径劫持

Issue 现记录并在 consume 复核：

- `sealed_httpx_client_id`
- `sealed_httpx_transport_id`（`id(client._transport)`，且类型须为 `httpx.HTTPTransport`）
- `sealed_httpx_mounts`（pattern → transport 对象 id；`None` 旁路槽密封为 `0`；非 `HTTPTransport` 的非空 mount 失败关闭）
- 实例字典与类级 / 绑定方法身份：`httpx.Client.send` / `.post` / `.request` 对 import-time 引用

**本 SHA 离线探针矩阵（带 sealed ids，除非另注）：**

| Issue 后攻击 | 绑定结果 |
|---|---|
| 整对象 `_client` → MockTransport client | `BINDING_REJECT` |
| 同 client `_transport` → MockTransport | `BINDING_REJECT`（live mode `custom`） |
| 同 client `_transport` → 另一 `HTTPTransport` | `BINDING_REJECT`（live mode 仍 `default`） |
| 实例 `Client.send` / `.post` / `.request` 遮蔽 | `BINDING_REJECT` |
| 类级 `httpx.Client.send` monkeypatch | `BINDING_REJECT` |
| `_mounts` 注入 MockTransport（默认 `_transport` 未改） | `BINDING_REJECT` |
| `_mounts` 换成兄弟 `HTTPTransport` | `BINDING_REJECT` |
| 仅可写 `_transport_mode` 撒谎（issue，无 seals） | `BINDING_REJECT`（live 仍 `custom`） |
| 不同对象 `_ledger` / `_price_snapshot` 替换 | `BINDING_REJECT` |
| 实例 `model.complete` 遮蔽 | `BINDING_REJECT` |
| 类级 `HTTPTransport.handle_request` 补丁 | `BINDING_PASS`（残余） |

**对照说明：** 若跳过绑定检查，实例遮蔽的 `send` 仍可劫持 `Client.post`
（`POST_VIA_SHADOWED_SEND_DIVERTED_IF_UNCHECKED True`）——这证实原攻击面真实；
consume 路径现于零调用前拒绝，故 formal 循环不再驶过该旁路。

对抗测试现覆盖：`sibling_http_transport`、`client_method_shadow`（parametrized
send/post/request）、`mounts_mock_injection`，与先验 client swap /
transport_mode lie / budget graph 一并 **23 passed**。

### residual — 类级 `HTTPTransport.handle_request` monkeypatch

**仍为残余，未新闭合。** 与 TDD「Scope and known gaps」及
`docs/09_project_status.md` 声明一致：对象身份封印合同之外的进程内类方法
补丁仍可通过 `_require_bound_formal_runtime_objects`。不把此项抬为对本轨的
新阻断，除非声称 TCB 扩展到“任意同进程类 monkeypatch”。

### note — 显式 `transport=httpx.HTTPTransport(...)` 于 issue

仍视为 default-equivalent（类型门）；具体 transport 对象 id 与 mounts 已封印，
issue 后兄弟替换失败关闭。与先验 note 一致，非旁路。

### note — Source fingerprint 覆盖

代码层中间目录 symlink 逃逸仍闭。仍无专门单测断言 `followlinks=False` /
symlink 目录剔除；回归风险保留为覆盖缺口，非本 SHA 新洞。

### note — Holdout 锁 / 同用户 FS

同 OS 用户重命名/删除私有锁文件可重开“首次”formal start：声称 TCB 外，不变。

### note — Profile 冻结 / 无凭证配置

Canonical 强制与 `public_runtime_config` 无凭证字段 / live `transport_mode`
行为未回退。

## Verdict Gate

**GO**

**理由：** `ac6ccd8` 阻断 P1（client 内 send 路径 / 兄弟 `HTTPTransport` /
mounts 注入）在本 SHA **已闭合并有对抗测试**；先验 P0 级 client 替换、
transport_mode 撒谎、ledger·price 重绑、capability 方法/实体替换**仍失败关闭**。
类级 `HTTPTransport.handle_request` monkeypatch **仍为文档化残余**，非新开洞、
亦非本轨 GO 门槛内的未修阻断。Source 中间目录 symlink 代码仍闭；缺专用单测
为覆盖缺口 note。

本轨对 formal runtime / capability / source / harness 身份绑定：**GO**
（带已声明的类级 transport `handle_request` 残余）。
