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

  // ---- Staff sign-in (per-person accounts with roles). Kept for this browser tab only.
  // The setup PIN still works as a break-glass admin sign-in, to create the first accounts.
  _session(key, value) {
    try {
      if (value === undefined) return JSON.parse(sessionStorage.getItem(key) || "null");
      if (value === null) sessionStorage.removeItem(key); else sessionStorage.setItem(key, JSON.stringify(value));
    } catch { return null; }
  },
  staffUser() { return TL._session("tl.staff"); },
  staffHeaders() {
    const s = TL.staffUser();
    if (!s) return {};
    return s.token ? { Authorization: `Bearer ${s.token}` } : { "X-Staff-Pin": s.pin };
  },
  staffSocketQuery() {
    const s = TL.staffUser() || {};
    return s.token ? { token: s.token } : { pin: s.pin || "" };
  },
  staffSignOut() { TL._session("tl.staff", null); location.reload(); },

  _signingIn: null,
  staffSignIn() {
    if (TL._signingIn) return TL._signingIn;
    TL._signingIn = new Promise((resolve) => {
      const d = document.createElement("dialog");
      d.className = "signin";
      d.innerHTML = `
        <form method="dialog" class="stack" style="padding:20px;gap:12px;min-width:min(340px,86vw)">
          <h2>Staff sign-in</h2>
          <label class="small stack" style="gap:4px">Username
            <input name="u" autocomplete="username" required style="font:inherit;padding:8px 10px;border-radius:10px;border:1px solid var(--line)"></label>
          <label class="small stack" style="gap:4px">Password
            <input name="p" type="password" autocomplete="current-password" required style="font:inherit;padding:8px 10px;border-radius:10px;border:1px solid var(--line)"></label>
          <p class="small" style="margin:0;color:var(--red)" data-err role="alert"></p>
          <button class="primary">Sign in</button>
          <button type="button" class="ghost small" data-pin>Use the setup PIN instead</button>
        </form>`;
      document.body.appendChild(d);
      const form = d.querySelector("form");
      const err = d.querySelector("[data-err]");
      const done = (session) => { TL._session("tl.staff", session); d.close(); d.remove(); TL._signingIn = null; resolve(session); };
      d.addEventListener("cancel", (e) => e.preventDefault());  // staff pages need a sign-in
      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        try {
          const { data } = await TL.api("/api/staff/login", { username: form.u.value, password: form.p.value });
          done(data);
        } catch (ex) { err.textContent = ex.message; form.p.value = ""; form.p.focus(); }
      });
      d.querySelector("[data-pin]").onclick = async () => {
        const pin = (window.prompt("Setup PIN (server console, or data/staff_pin.txt)") || "").trim();
        if (!pin) return;
        try {
          await TL.api("/api/staff/me", undefined, "GET", { "X-Staff-Pin": pin });
          done({ pin, username: "setup-pin", name: "Setup PIN", role: "admin" });
        } catch (ex) { err.textContent = ex.message; }
      };
      d.showModal();
      form.u.focus();
    });
    return TL._signingIn;
  },

  async staffApi(path, body, method) {
    if (!TL.staffUser()) await TL.staffSignIn();
    try {
      return await TL.api(path, body, method, TL.staffHeaders());
    } catch (e) {
      if (e.status !== 401) throw e;
      TL._session("tl.staff", null);  // expired or revoked
      await TL.staffSignIn();
      return TL.api(path, body, method, TL.staffHeaders());
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
