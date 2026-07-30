# Phase 2 新鲜对抗复审 — Runtime / Capability / Source / Harness

独立对抗审查。审查者**未**实现被审代码。仅报告；无生产改动；无 DeepSeek 调用；无密钥输出。

## 审查 SHA

- 要求：`ac6ccd84533f827b93287b4aece601a5f1aee0e2`
- 核实：`git rev-parse HEAD` → `ac6ccd84533f827b93287b4aece601a5f1aee0e2`
- 审查开始时与写报告前工作树：干净（`git status --porcelain` 为空）
- HEAD 说明：`fix: seal Phase 2 runtime and bind paid evidence digests`

## 范围

**仅**跟踪：`holdout_formal` 的 runtime / capability / source / harness 身份绑定。

先验 NO-GO 上下文：
`docs/testing/phase2-fresh-reaudit-runtime-harness-a10facb.md`（SHA `a10facb`）。

须核实闭合、并继续挖洞的四点：

1. Live `httpx.Client` / transport 在 issue + consume 封印；`transport_mode` 不可靠可写属性伪造。
2. `budget_guard._ledger` / `_price_snapshot` 对象身份封印。
3. Source-tree fingerprint 不跟随中间目录 symlink。
4. Formal capability 的对象 / 方法替换仍失败关闭。

范围外：案例集质量、LLM 裁判标定语义、预算账本隐私轨、同 OS 用户任意 FS/RCE 作为声称 TCB。

## 方法

1. 确认 SHA + 干净工作树（停止规则已满足）。
2. 阅读 capability issue/consume、`transport_mode_for_client`、`_source_fingerprints`、TDD 证据，以及针对先验 P0/P1 的对抗测试。
3. 离线谓词探针（无 provider I/O）：issue 后篡改通道 / 预算图，再以 sealed id 调用 `_require_bound_formal_runtime_objects`。
4. 运行：
   `.venv/bin/python -m pytest tests/test_dev_repeat_paid_gate.py -k
   'httpx_client_swap or transport_mode_lie or budget_graph_rebinding or
   formal_execution_capability or formal_model_public_runtime_config' -q`
   → **18 passed**。

## 先验发现闭合情况

| 先验 | 主张 | `ac6ccd8` 状态 |
|---|---|---|
| P0 | Issue 后整对象 `_client` 替换 / MockTransport 通道 | **已闭合** — 记录 `sealed_httpx_client_id`；consume 拒绝 id 不匹配；同 client 上换成 MockTransport → `live_transport_mode()=="custom"` → 拒绝。测试：`httpx_client_swap`。 |
| P0 变体 | 自定义 transport + `_transport_mode = "default"` 撒谎 | **已闭合** — `public_runtime_config` / 绑定经 `transport_mode_for_client(live _client)` 推导（`type(transport) is httpx.HTTPTransport`）。可写 `_transport_mode` 无效。测试：`transport_mode_lie`。 |
| P1 | Issue 后 `_ledger` / `_price_snapshot` 重绑 | **已闭合** — issue 封印对象 id；consume 拒绝不同对象替换。测试：`budget_graph_rebinding`。离线：两个独立 snapshot 实例 → `DISTINCT_PRICE_SWAP BINDING_REJECT`。 |
| P2 | 中间目录 symlink 被跟入 `source_tree_sha256` | **代码已闭合** — `_source_fingerprints` 使用 `os.walk(..., followlinks=False)`，剔除 symlink 目录名，跳过 symlink 文件；最终读取仍 `O_NOFOLLOW`。离线 walk 复现：外链目录下字节**不**进入 digest。**覆盖缺口：** 无专门对抗单测锁住中间目录 symlink 行为。 |
| note | 付费 vs 标定路径对 `/v1` 不一致 | **已闭合 / 失败关闭** — `validate_paid_eval_settings` 现要求 `path.rstrip("/") == ""`（拒绝 `/v1`）；标定仍允许 `""`/`"/"`。Formal issue 两者都调。 |
| (4) | Actor / judge / harness / model+guard 方法替换 | **仍失败关闭** — 既有 capability 测试通过；实例 `model.complete` 遮蔽 → `BINDING_REJECT`。 |

## 本 SHA 发现

### P1 — 封印的 `httpx.Client` 身份并未封印 live send 路径

**主张仍不完整：** Formal 绑定记录 `sealed_httpx_client_id`，并要求
`type(_transport) is httpx.HTTPTransport`，但**未**绑定：

- `httpx.Client.send` / `.post` / `.request` 方法身份
- `_transport` **对象**身份（只查类型）
- `_mounts` 图
- 类级 `httpx.HTTPTransport.handle_request`

**利用形态（与先验 P0 同一程序化窗口）：**

1. Issue 合法 `ValidatedFormalExecutionCapability`（工厂默认 client）。
2. 用实例 `MethodType` 遮蔽 `model._client.send`（或 `.post`）返回伪造
   `httpx.Response` — 或把 `_transport` 换成另一个 `httpx.HTTPTransport()` —
   或向 `_mounts` 注入 `MockTransport` — 或在类上 monkeypatch
   `HTTPTransport.handle_request`。
3. Consume / `_require_bound_formal_runtime_objects(..., sealed_httpx_client_id=...)`
   仍通过：client `id` 不变，transport 类型仍为 `HTTPTransport`（send/post/mounts/
   兄弟 transport 情形），`transport_mode` 仍为 `"default"`。
4. `model._complete` 调用 `self._client.post(...)`；离线探针证明实例遮蔽的
   `send` **确实劫持** `Client.post`（`POST_VIA_SHADOWED_SEND_DIVERTED True`）。

**本 SHA 离线探针矩阵：**

| Issue 后攻击 | 带 sealed id 的绑定结果 |
|---|---|
| 整对象 `_client` → MockTransport client | `BINDING_REJECT`（先验 P0 已闭） |
| 同 client `_transport` → MockTransport | `BINDING_REJECT` |
| 同 client `_transport` → 另一 `HTTPTransport` | `BINDING_PASS` |
| 实例 `Client.send` / `Client.post` 遮蔽 | `BINDING_PASS` |
| `_mounts` 注入 MockTransport | `BINDING_PASS` |
| 类级 `HTTPTransport.handle_request` 补丁 | `BINDING_PASS` |
| 仅可写 `_transport_mode` 撒谎 | live mode 仍正确（属性非权威） |
| 不同对象 `_ledger` / `_price_snapshot` 替换 | `BINDING_REJECT` |
| 实例 `model.complete` 遮蔽 | `BINDING_REJECT` |

**覆盖缺口：** 新对抗测试覆盖整 client 替换与 transport-mode 属性撒谎，
**未**覆盖 client 内 `send`/`post` 遮蔽、兄弟 `HTTPTransport` 替换、mounts
注入。TDD（`formal-runtime-capability-binding.tdd.md`）已写明任意
`HTTPTransport` 视为 default-equivalent — 该仅按类型的规则把 send 路径洞留开。

**影响：** 与先验 P0 同一攻击目标 — formal eval 可在替换后的 provider 通道上
执行，而 capability 注册表、sealed client id、harness fingerprint、lock
receipt 仍显得规范。先验 P0 在 `_client` 对象边界已闭；通道仍可在**该对象内部**
重绑。

### note — 构造时显式 `transport=httpx.HTTPTransport(...)`

Issue 接受显式注入的 `HTTPTransport`（`EXPLICIT_HTTPTRANSPORT_ISSUE
BINDING_PASS`）。TDD 已记为 default-equivalent。本身不是新旁路；与 P1
send 路径洞并列，属设计边界。

### note — Source fingerprint 覆盖

代码层中间目录 symlink 逃逸看起来已闭。无测试断言 `followlinks=False` /
symlink 目录剔除；若 walk 改回 `Path.rglob` 有回归风险。

### note — Holdout 锁 / 同用户 FS 重命名

残余不变：同 OS 用户重命名/删除私有锁文件可重开“首次”formal start。在声称
TCB 外。CLI `--run-id` / `--output-root` 重定向仍失败关闭（既有测试）。

### note — Profile 冻结 / 无凭证配置

Canonical `30` / `1024` / `2` / `4` / `12`、模型、temperature 0、host 在
formal-eligible 路径仍强制。`public_runtime_config` 仍无凭证字段，并用
`live_transport_mode()`。

## Verdict Gate

**NO-GO**

**阻断理由：** a10facb 的 P0/P1/P2 检查项按原文主张大体**已闭合**，但仍有
**P1 残余**：formal capability 封印的是 `httpx.Client` **对象身份**与
transport **类型**，不是 live `send`/`post` 路径（也不封印 transport 对象 id /
mounts）。Issue 后实例级 `Client.send`（或等价手段）仍可通过 consume 绑定，并
把 formal 案例循环驶离规范通道 — 威胁模型与攻击目标与原 transport-swap
NO-GO 相同。

## 残余风险 / GO 前最低修复

1. **在 issue + consume 封印 live client send 路径**（失败关闭，零调用），至少其一：
   - 拒绝 `httpx.Client.send` / `.post` / `.request` 的实例字典遮蔽，并要求类方法
     身份对 import-time 引用；
   - 与 `sealed_httpx_client_id` 一并记录并复核 `id(client._transport)`（可选
     mounts digest）；
   - 包装 client，使 `OpenAICompatibleChatClient._complete` 使用的出站调用面
     无法在不触发 model/client 身份检查的情况下重绑。
2. 对抗测试对齐既有 capability 用例：
   - Issue 后 `Client.send`（及 `.post`）遮蔽 → 拒绝，零模型调用，零预算 attempt；
   - Issue 后兄弟 `HTTPTransport` 对象替换与 `_mounts` MockTransport 注入 → 拒绝
     （若另有封印使劫持不可能，则须在 TDD 显式接受并论证）。
3. 增加 source-tree fingerprint 测试：中间目录 symlink 不得把外部文件字节计入
   `current_source_tree_sha256`。
4. 同用户锁文件删除继续记为残余 TCB，不视为 capability 工作已修。

在 (1)+(2) 落地并通过离线对抗测试之前，本轨对 formal runtime / harness 身份仍为
**NO-GO**。
