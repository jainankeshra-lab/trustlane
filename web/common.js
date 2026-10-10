// Shared helpers for the TrustLane pages.
const TL = {
  async api(path, body, method, headers) {
    const res = await fetch(path, {
      method: method || (body === undefined ? "GET" : "POST"),
      headers: { "Content-Type": "application/json", ...(headers || {}) },
      body: body === undefined ? undefined : (typeof body === "string" ? body : JSON.stringify(body)),
    });
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = text; }
    if (!res.ok) {
      const err = new Error((data && data.detail) || res.statusText);
      err.status = res.status;
      throw err;
    }
    return { status: res.status, data };
  },

  // Reconnecting WebSocket on one channel. `query` (optional) returns fresh credentials per connect.
  socket(channel, onMessage, onState, query) {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const open = () => {
      const qs = query ? "?" + new URLSearchParams(query()).toString() : "";
      const ws = new WebSocket(`${proto}://${location.host}/ws/${encodeURIComponent(channel)}${qs}`);
      ws.onopen = () => onState && onState(true);
      ws.onmessage = (e) => onMessage(JSON.parse(e.data));
      ws.onclose = () => { onState && onState(false); setTimeout(open, 1500); };
    };
    open();
  },

  // Staff PIN for the ops console and simulator (kept for this browser tab only).
  staffPin(force) {
    let pin = null;
    try { pin = sessionStorage.getItem("tl.pin"); } catch { /* storage blocked */ }
    if (!pin || force) {
      pin = (window.prompt ? window.prompt("Staff PIN (printed in the server console, or data/staff_pin.txt)") : "") || "";
      try { sessionStorage.setItem("tl.pin", pin); } catch { /* storage blocked */ }
    }
    return pin;
  },

  async staffApi(path, body, method) {
    try {
      return await TL.api(path, body, method, { "X-Staff-Pin": TL.staffPin() });
    } catch (e) {
      if (e.status === 401) {
        TL.staffPin(true);
        return TL.api(path, body, method, { "X-Staff-Pin": TL.staffPin() });
      }
      throw e;
    }
  },

  rupees(paise) {
    return "₹" + (paise / 100).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  },

  esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  },

  clock(ts) {
    return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  },

  toast(text, kind) {
    const el = document.createElement("div");
    el.className = "toast" + (kind ? " " + kind : "");
    el.textContent = text;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 3500);
  },

  load(key, fallback) {
    try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; }
  },

  save(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* storage blocked: keep going in memory */ }
  },
};
