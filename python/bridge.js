(() => {
  if (window.__HELPTOA_INSTALLED__) return;
  window.__HELPTOA_INSTALLED__ = true;

  const Native = window.WebSocket;
  const sockets = new Map();
  let seq = 0;
  const ALLOWED = new Set([
    "get_version", "get_helpdesk_details", "get_helpdesk_chats", "get_active_chats",
    "get_contact_list", "get_online_users", "get_chat_messages", "start_chat",
    "send_message", "send_action", "leave_chat", "join_chat", "invite_to_chat",
    "move_chat", "search_contact",
  ]);

  const target = (u) => String(u || "").includes("ocs.oraclecloud.com") && String(u).includes("/websocket/");

  async function norm(v) {
    if (typeof v === "string") {
      try { return { format: "json", value: JSON.parse(v) }; } catch (e) { return { format: "text", value: v }; }
    }
    if (v instanceof Blob) {
      const t = await v.text();
      try { return { format: "json", value: JSON.parse(t) }; } catch (e) { return { format: "blob-text", value: t }; }
    }
    return { format: typeof v, value: String(v) };
  }

  function push(rec) {
    try {
      if (typeof window.__helptoaPush === "function") window.__helptoaPush(rec);
    } catch (e) { /* Python ainda não ligou o binding */ }
  }

  function slimSearchContact(data) {
    if (!data || data.a !== "search_contact" || !data.d || !data.d.result || typeof data.d.result !== "object") {
      return data;
    }
    const slim = {};
    for (const [qa, qd] of Object.entries(data.d.result)) {
      const ops = [];
      for (const op of (qd && qd.operators) || []) {
        if (op && op.address) ops.push({ address: op.address, name: op.name || "" });
      }
      slim[qa] = { name: (qd && qd.name) || "", operators: ops };
    }
    return Object.assign({}, data, { d: Object.assign({}, data.d, { result: slim }) });
  }

  async function emit(direction, url, payload, socketId) {
    const n = await norm(payload);
    let data = n.value;
    if (data && typeof data === "object" && data.data && (data.data.n || data.data.a) && !data.a && !data.n) {
      data = data.data;
    }
    data = slimSearchContact(data);
    push({ direction, url, socketId, payloadFormat: n.format, data });
  }

  const registry = new WeakMap();

  function adopt(ws, url) {
    const known = registry.get(ws);
    if (known) return known;
    const id = "toa-socket-" + (++seq);
    const entry = { id, ws, url };
    registry.set(ws, entry);
    sockets.set(id, entry);
    ws.addEventListener("open", () => {
      emit("event", url, JSON.stringify({ event: "open" }), id);
    });
    ws.addEventListener("close", (e) => {
      emit("event", url, JSON.stringify({ event: "close", code: e.code }), id);
      sockets.delete(id);
    });
    ws.addEventListener("message", (e) => emit("received", url, e.data, id));
    if (ws.readyState === Native.OPEN) {
      emit("event", url, JSON.stringify({ event: "open" }), id);
    }
    return entry;
  }

  function validate(p) {
    if (!p || !ALLOWED.has(p.a)) throw Error("Ação não permitida");
    const d = p.d || {};
    if (["send_message", "send_action", "leave_chat", "get_chat_messages", "join_chat", "move_chat", "invite_to_chat"].includes(p.a)
        && !Number.isFinite(Number(d.chat_id))) throw Error("chat_id obrigatório");
    if (p.a === "move_chat" && !String(d.channel_address || "").trim()) throw Error("channel_address obrigatório");
    if (p.a === "invite_to_chat" && !String(d.address || "").trim()) throw Error("address obrigatório");
    if (p.a === "send_message") {
      const m = d.message || {};
      if (!String(m.text || "").trim() && !m.attachment) throw Error("Mensagem vazia");
    }
    if (p.a === "start_chat" && (!Array.isArray(d.recipients && d.recipients.users) || !d.recipients.users.length)) {
      throw Error("Destinatário obrigatório");
    }
  }

  function openEntry() {
    return [...sockets.values()].find((x) => x.ws.readyState === Native.OPEN) || null;
  }

  window.__HELPTOA = {
    send(payload) {
      validate(payload);
      const s = openEntry();
      if (!s) throw Error("WebSocket do TOA não está aberto");
      const r = String(payload.r || Math.floor(Math.random() * 9e7 + 1e7));
      const p = Object.assign({}, payload, { d: payload.d || {}, r, t: String(payload.t || Date.now()) });
      s.ws.send(JSON.stringify(p));
      return { ok: true, r };
    },
    info() {
      const s = openEntry();
      if (!s) return { open: false, u: "" };
      let u = "";
      try { u = new URL(s.url).searchParams.get("u") || ""; } catch (e) { /* ignore */ }
      return { open: true, u };
    },
  };

  const protoSend = Native.prototype.send;
  Native.prototype.send = function (d) {
    try {
      if (target(this.url)) {
        const entry = registry.get(this) || adopt(this, this.url);
        emit("sent", entry.url, d, entry.id);
      }
    } catch (e) { /* ignore */ }
    return protoSend.apply(this, arguments);
  };

  function W(url, protocols) {
    const ws = protocols === undefined ? new Native(url) : new Native(url, protocols);
    if (target(url)) adopt(ws, url);
    return ws;
  }
  W.prototype = Native.prototype;
  Object.setPrototypeOf(W, Native);
  ["CONNECTING", "OPEN", "CLOSING", "CLOSED"].forEach((k) => {
    Object.defineProperty(W, k, { value: Native[k] });
  });
  window.WebSocket = W;
})();
