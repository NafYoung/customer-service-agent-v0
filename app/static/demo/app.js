(() => {
  const state = {
    csrf: null,
    hasPending: false,
    presented: false,
    mode: null,
    modeLabel: null,
  };

  const logEl = document.getElementById("log");
  const cardEl = document.getElementById("card");
  const statusEl = document.getElementById("status");
  const confirmBtn = document.getElementById("confirm");
  const rejectBtn = document.getElementById("reject");
  const hintsEl = document.getElementById("hints");
  const form = document.getElementById("composer");
  const messageEl = document.getElementById("message");
  const resetBtn = document.getElementById("reset");
  const modeBadgeEl = document.getElementById("mode-badge");
  const panelNoteEl = document.getElementById("panel-note");

  function setStatus(text, kind) {
    statusEl.textContent = text || "";
    statusEl.className = "status" + (kind ? " " + kind : "");
  }

  function applyMode(data) {
    state.mode = data.demo_agent_mode || null;
    state.modeLabel = data.mode_label || data.demo_agent_mode || "";
    if (modeBadgeEl) modeBadgeEl.textContent = state.modeLabel || "演示";
    if (panelNoteEl) {
      if (state.mode === "preparation_live") {
        panelNoteEl.textContent = "本地 live DeepSeek · 预算闸门";
      } else if (state.mode === "preparation_scripted") {
        panelNoteEl.textContent = "Preparation Agent · scripted · 零外网";
      } else {
        panelNoteEl.textContent = "离线脚本 · 不调用 DeepSeek";
      }
    }
  }

  function welcomeCopy(name) {
    if (state.mode === "preparation_live") {
      return (
        "你好，" +
        name +
        "。本地 live 模式：真实 DeepSeek 只负责查询与准备；右侧确认卡由宿主展示，确认/拒绝/执行均在模型工具面之外。"
      );
    }
    if (state.mode === "preparation_scripted") {
      return (
        "你好，" +
        name +
        "。消息经 Preparation Agent（scripted 多轮工具，零外网）写出 pending；请核对确认卡后再确认或拒绝。"
      );
    }
    return (
      "你好，" +
      name +
      "。这是离线演示：准备操作后请核对确认卡，再点击确认执行或拒绝。"
    );
  }

  function appendBubble(role, text) {
    const div = document.createElement("div");
    div.className = "bubble " + role;
    const who = document.createElement("span");
    who.className = "who";
    who.textContent = role === "user" ? "你" : "RIVET";
    const body = document.createElement("div");
    body.textContent = text;
    div.appendChild(who);
    div.appendChild(body);
    if (role === "assistant") {
      const badge = document.createElement("span");
      badge.className = "ai-badge";
      badge.textContent = "本回复由 AI 生成";
      div.appendChild(badge);
    }
    logEl.appendChild(div);
    logEl.scrollTop = logEl.scrollHeight;
  }

  function appendToolTrace(items) {
    if (!items || !items.length) return;
    const div = document.createElement("div");
    div.className = "bubble assistant trace";
    const who = document.createElement("span");
    who.className = "who";
    who.textContent = "工具轨迹";
    div.appendChild(who);
    const list = document.createElement("ol");
    list.className = "tool-trace";
    items.forEach((item) => {
      const li = document.createElement("li");
      li.className = item.success ? "ok" : "fail";
      li.textContent =
        (item.success ? "✓ " : "✗ ") +
        (item.tool_name || "?") +
        " — " +
        (item.summary || "");
      list.appendChild(li);
    });
    div.appendChild(list);
    logEl.appendChild(div);
    logEl.scrollTop = logEl.scrollHeight;
  }

  function actionLabel(type) {
    if (type === "CANCEL_ORDER") return "取消订单";
    if (type === "RETURN_ITEM") return "退货申请";
    if (type === "EXCHANGE_ITEM") return "换货申请";
    return type;
  }

  function setCardButtonsEnabled(enabled) {
    confirmBtn.disabled = !enabled;
    rejectBtn.disabled = !enabled;
  }

  function renderEmptyCard() {
    cardEl.className = "card empty";
    cardEl.replaceChildren();
    const p = document.createElement("p");
    p.className = "card-empty-copy";
    p.textContent = "尚无待确认操作。完成准备后，这里会展示规范预览。";
    cardEl.appendChild(p);
    setCardButtonsEnabled(false);
    state.presented = false;
    state.hasPending = false;
  }

  function addRow(dl, label, value) {
    if (value == null || value === "") return;
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = String(value);
    dl.appendChild(dt);
    dl.appendChild(dd);
  }

  function renderCard(card) {
    cardEl.className = "card";
    cardEl.replaceChildren();

    const kicker = document.createElement("p");
    kicker.className = "card-kicker";
    kicker.textContent = "Canonical preview · " + card.status;
    cardEl.appendChild(kicker);

    const title = document.createElement("h3");
    title.textContent = actionLabel(card.action_type);
    cardEl.appendChild(title);

    const dl = document.createElement("dl");
    addRow(dl, "订单", card.order_id);
    addRow(dl, "商品", card.product_name);
    addRow(dl, "尺码", card.size || card.current_size);
    addRow(dl, "目标尺码", card.target_size);
    addRow(dl, "数量", card.quantity);
    addRow(dl, "订单状态", card.current_order_status);
    addRow(dl, "品相", card.declared_condition);
    addRow(dl, "原因", card.issue_type);
    addRow(dl, "影响", card.effect);
    addRow(dl, "政策", card.policy_decision);
    addRow(dl, "到期", card.expires_at);
    cardEl.appendChild(dl);

    const warn = document.createElement("p");
    warn.className = "warn";
    warn.textContent = card.note || "尚未执行；确认前不会写入业务状态。";
    cardEl.appendChild(warn);

    state.hasPending = true;
    setCardButtonsEnabled(!!state.presented);
  }

  async function api(path, options) {
    const headers = Object.assign(
      { Accept: "application/json" },
      options && options.headers
    );
    if (options && options.method && options.method !== "GET") {
      headers["Content-Type"] = "application/json";
      if (state.csrf) headers["X-CSRF-Token"] = state.csrf;
    }
    const res = await fetch(path, {
      method: (options && options.method) || "GET",
      credentials: "same-origin",
      headers,
      body: options && options.body !== undefined ? options.body : undefined,
    });
    const text = await res.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch (_) {
      data = { raw: text };
    }
    if (!res.ok) {
      const msg =
        (data && data.error && data.error.message) ||
        "请求失败 (" + res.status + ")";
      const err = new Error(msg);
      err.status = res.status;
      err.payload = data;
      throw err;
    }
    return data;
  }

  async function refreshPending() {
    try {
      const card = await api("/demo/pending-action");
      renderCard(card);
      return card;
    } catch (err) {
      if (err.status === 404) {
        renderEmptyCard();
        return null;
      }
      throw err;
    }
  }

  async function markPresented() {
    const card = await api("/demo/pending-action/presented", {
      method: "POST",
      body: "{}",
    });
    state.presented = true;
    renderCard(card);
    setCardButtonsEnabled(true);
    setStatus("确认卡已由宿主标记为已展示；可确认或拒绝。", "ok");
  }

  async function startSession() {
    const data = await api("/demo/session", {
      method: "POST",
      body: "{}",
    });
    state.csrf = data.csrf_token;
    applyMode(data);
    hintsEl.replaceChildren();
    (data.supported_scenarios || []).forEach((item) => {
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = item;
      btn.addEventListener("click", () => {
        messageEl.value = item;
        messageEl.focus();
      });
      li.appendChild(btn);
      hintsEl.appendChild(li);
    });
    appendBubble("assistant", welcomeCopy(data.customer_display_name));
    renderEmptyCard();
    setStatus("会话已建立 · " + (state.modeLabel || ""));
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = messageEl.value.trim();
    if (!text) return;
    appendBubble("user", text);
    messageEl.value = "";
    setStatus("处理中…");
    try {
      const data = await api("/demo/messages", {
        method: "POST",
        body: JSON.stringify({ message: text }),
      });
      appendBubble("assistant", data.reply);
      appendToolTrace(data.tool_trace);
      if (data.has_pending_action) {
        await refreshPending();
        await markPresented();
      } else {
        renderEmptyCard();
      }
      setStatus("provider HTTP 调用：" + (data.provider_http_calls || 0), "ok");
    } catch (err) {
      appendBubble("assistant", "错误：" + err.message);
      setStatus(err.message, "err");
    }
  });

  confirmBtn.addEventListener("click", async () => {
    if (!state.hasPending || !state.presented) return;
    setCardButtonsEnabled(false);
    setStatus("确认并执行中…");
    try {
      const data = await api("/demo/pending-action/confirm", {
        method: "POST",
        body: "{}",
      });
      appendBubble("assistant", data.result_summary);
      renderEmptyCard();
      setStatus(data.result_summary, "ok");
    } catch (err) {
      setStatus(err.message, "err");
      setCardButtonsEnabled(true);
    }
  });

  rejectBtn.addEventListener("click", async () => {
    if (!state.hasPending || !state.presented) return;
    setCardButtonsEnabled(false);
    setStatus("拒绝中…");
    try {
      const data = await api("/demo/pending-action/reject", {
        method: "POST",
        body: "{}",
      });
      appendBubble("assistant", data.message || "已拒绝待确认操作。");
      renderEmptyCard();
      setStatus(data.message || "已拒绝", "ok");
    } catch (err) {
      setStatus(err.message, "err");
      setCardButtonsEnabled(true);
    }
  });

  resetBtn.addEventListener("click", async () => {
    setStatus("重置中…");
    try {
      const data = await api("/demo/reset", {
        method: "POST",
        body: "{}",
      });
      state.csrf = data.csrf_token;
      applyMode(data);
      logEl.replaceChildren();
      appendBubble("assistant", data.message);
      renderEmptyCard();
      setStatus("已重置。", "ok");
    } catch (err) {
      setStatus(err.message, "err");
    }
  });

  startSession().catch((err) => {
    setStatus(err.message, "err");
    appendBubble("assistant", "无法创建演示会话：" + err.message);
  });
})();
