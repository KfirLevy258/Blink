// Frontend: poll the local server for cached usage, render gauges, and tick
// the reset countdowns once per second (purely client-side, no extra calls).
const RING_CIRCUMFERENCE = 2 * Math.PI * 52; // matches stroke-dasharray in CSS
const POLL_MS = 15_000; // poll the *local* cache cheaply

const el = (id) => document.getElementById(id);
let latest = null; // last /api/usage payload

// ---- rendering -------------------------------------------------------------
function colorFor(pct) {
  if (pct >= 90) return getComputedStyle(document.documentElement).getPropertyValue("--red");
  if (pct >= 70) return getComputedStyle(document.documentElement).getPropertyValue("--amber");
  return getComputedStyle(document.documentElement).getPropertyValue("--green");
}

function setGauge(prefix, window) {
  const pctEl = el(`${prefix}-pct`);
  const fillEl = el(`${prefix}-fill`);
  if (!window) {
    pctEl.textContent = "—";
    fillEl.style.strokeDashoffset = RING_CIRCUMFERENCE;
    return;
  }
  const pct = Math.max(0, Math.min(100, window.pct));
  pctEl.textContent = `${Math.round(pct)}%`;
  fillEl.style.strokeDashoffset = RING_CIRCUMFERENCE * (1 - pct / 100);
  fillEl.style.stroke = colorFor(pct).trim();
}

function fmtCountdown(resetsAt) {
  if (!resetsAt) return "resets in —";
  const ms = new Date(resetsAt).getTime() - Date.now();
  if (ms <= 0) return "resetting…";
  const s = Math.floor(ms / 1000);
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (d > 0) return `resets in ${d}d ${h}h ${m}m`;
  if (h > 0) return `resets in ${h}h ${m}m ${sec}s`;
  return `resets in ${m}m ${sec}s`;
}

const STATE_UI = {
  connected: { dot: "ok", label: "Connected" },
  rate_limited: { dot: "warn", label: "Rate-limited (showing last)" },
  stale: { dot: "warn", label: "Stale (showing last)" },
  error: { dot: "bad", label: "Error" },
  logged_out: { dot: "", label: "Logged out" },
};

function render() {
  if (!latest) return;
  const { authed, state, data, error } = latest;

  el("login-view").hidden = authed;
  el("gauges-view").hidden = !authed;
  el("logout").hidden = !authed;

  const ui = STATE_UI[state] || STATE_UI.error;
  el("dot").className = `dot ${ui.dot}`;
  el("state-label").textContent = error || ui.label;

  if (data) {
    setGauge("session", data.session);
    setGauge("weekly", data.weekly);
    el("session-reset").textContent = fmtCountdown(data.session?.resetsAt);
    el("weekly-reset").textContent = fmtCountdown(data.weekly?.resetsAt);
    el("updated").textContent = "· updated " + new Date(data.fetchedAt).toLocaleTimeString();

    const sub = [];
    if (data.weeklySonnet) sub.push(`Sonnet ${Math.round(data.weeklySonnet.pct)}%`);
    if (data.weeklyOpus) sub.push(`Opus ${Math.round(data.weeklyOpus.pct)}%`);
    el("weekly-sub").textContent = sub.join(" · ");
  }
}

// Re-tick countdowns every second without re-fetching.
function tickCountdowns() {
  if (latest?.data) {
    el("session-reset").textContent = fmtCountdown(latest.data.session?.resetsAt);
    el("weekly-reset").textContent = fmtCountdown(latest.data.weekly?.resetsAt);
  }
}

// ---- data ------------------------------------------------------------------
async function poll() {
  try {
    const res = await fetch("/api/usage");
    latest = await res.json();
    render();
  } catch {
    /* server momentarily unreachable; keep showing last */
  }
}

// ---- auth ------------------------------------------------------------------
el("login-btn").addEventListener("click", async () => {
  const res = await fetch("/auth/login", { method: "POST" });
  const { url } = await res.json();
  window.open(url, "_blank", "noopener");
  el("code-step").hidden = false;
  el("code-input").focus();
});

el("exchange-btn").addEventListener("click", async () => {
  const code = el("code-input").value.trim();
  const errEl = el("login-error");
  errEl.hidden = true;
  if (!code) return;
  const res = await fetch("/auth/exchange", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ code }),
  });
  const out = await res.json();
  if (out.ok) {
    el("code-input").value = "";
    el("code-step").hidden = true;
    poll();
  } else {
    errEl.textContent = out.error || "Could not connect. Try logging in again.";
    errEl.hidden = false;
  }
});

el("code-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") el("exchange-btn").click();
});

el("logout").addEventListener("click", async () => {
  await fetch("/auth/logout", { method: "POST" });
  poll();
});

// ---- start -----------------------------------------------------------------
poll();
setInterval(poll, POLL_MS);
setInterval(tickCountdowns, 1000);
