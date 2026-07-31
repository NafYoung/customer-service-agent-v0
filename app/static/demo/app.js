(() => {
  const state = {
    csrf: null,
    hasPending: false,
    presented: false,
  };

  const logEl = document.getElementById("log");
  const cardEl = document.getElementById("card");
  const statusEl = document.getElementById("status");
  const confirmBtn = document.getElementById("confirm");
  const hintsEl = document.getElementById("hints");
  const form = document.getElementById("composer");
  const messageEl = document.getElementById("message");
  const resetBtn = document.getElementById("reset");

  function setStatus(text, kind) {
    statusEl.textContent = text || "";
    statusEl.className = "status" + (kind ? " " + kind : "");
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
    logEl.appendChild(div);
    logEl.scrollTop = logEl.scrollHeight;
  }

  function actionLabel(type) {
    if (type === "CANCEL_ORDER") return "取消订单";
    if (type === "RETURN_ITEM") return "退货申请";
    if (type === "EXCHANGE_ITEM") return "换货申请";
    return type;
  }

  function renderEmptyCard() {
    cardEl.className = "card empty";
    cardEl.replaceChildren();
    const p = document.createElement("p");
    p.className = "card-empty-copy";
    p.textContent =
      "尚无待确认操作。完成离线准备后，这里会展示规范预览。";
    cardEl.appendChild(p);
    confirmBtn.disabled = true;
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
    warn.textContent = card.note || "尚未执行";
    cardEl.appendChild(warn);

    state.hasPending = true;
    confirmBtn.disabled = !state.presented;
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
    confirmBtn.disabled = false;
    setStatus("确认卡已由宿主标记为已展示。", "ok");
  }

  async function startSession() {
    const data = await api("/demo/session", {
      method: "POST",
      body: "{}",
    });
    state.csrf = data.csrf_token;
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
    appendBubble(
      "assistant",
      "你好，" +
        data.customer_display_name +
        "。这是 RIVET 公开离线演示：准备操作后请核对确认卡，再点击确认执行。"
    );
    renderEmptyCard();
    setStatus("会话已建立。");
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
    confirmBtn.disabled = true;
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
      confirmBtn.disabled = false;
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
