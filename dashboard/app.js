/* ============================================================
   app.js — Vol Desk dashboard.

   Read-only client over the same Supabase the worker writes to.
   Uses the ANON key + Supabase Auth; every read is constrained by
   the RLS policies in schema/010 (authenticated SELECT only). No
   service key ever reaches this file — see config.js.

   Data changes every 15-20 min, so this polls gently rather than
   streaming. One module, no build step, matching the worker's
   plain-file philosophy so it stays editable by hand.
   ============================================================ */

const CFG = window.IV_CONFIG;
const API = `${CFG.SUPABASE_URL}/rest/v1`;
const AUTH = `${CFG.SUPABASE_URL}/auth/v1`;

let session = null;
let refreshTimer = null;
const state = { series: [], activeSeries: null, spikeFilter: "all" };

/* ---------- tiny helpers ---------- */
const $ = (id) => document.getElementById(id);
const el = (tag, cls, txt) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt != null) e.textContent = txt;
  return e;
};
const fmt = (n, d = 2) => (n == null || isNaN(n) ? "—" : Number(n).toFixed(d));
const pct = (n, d = 1) => (n == null || isNaN(n) ? "—" : (n * 100).toFixed(d) + "%");
const signedPct = (n, d = 1) => {
  if (n == null || isNaN(n)) return "—";
  const v = (n * 100).toFixed(d) + "%";
  return n >= 0 ? "+" + v : v;
};
function timeAgo(iso) {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 90) return `${Math.round(s)}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

/* ---------- data access ---------- */
async function rest(path) {
  const r = await fetch(`${API}/${path}`, {
    headers: {
      apikey: CFG.SUPABASE_ANON_KEY,
      Authorization: `Bearer ${session.access_token}`,
    },
  });
  if (r.status === 401) { doLogout(); throw new Error("session expired"); }
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
  return r.json();
}

/* ---------- auth ---------- */
async function doLogin() {
  const email = $("email").value.trim();
  const password = $("password").value;
  const errBox = $("login-error");
  errBox.textContent = "";
  const btn = $("login-btn");
  btn.disabled = true;
  btn.textContent = "Signing in…";
  try {
    const r = await fetch(`${AUTH}/token?grant_type=password`, {
      method: "POST",
      headers: { apikey: CFG.SUPABASE_ANON_KEY, "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    const data = await r.json();
    if (!r.ok) {
      // Interface voice: say what to do, don't apologize or blame.
      errBox.textContent = data.error_description || "Those credentials didn't match. Check and try again.";
      return;
    }
    session = data;
    localStorage.setItem("iv_session", JSON.stringify(session));
    enterApp();
  } catch (e) {
    errBox.textContent = "Can't reach the server. Check your connection and retry.";
  } finally {
    btn.disabled = false;
    btn.textContent = "Sign in";
  }
}

function doLogout() {
  session = null;
  localStorage.removeItem("iv_session");
  if (refreshTimer) clearInterval(refreshTimer);
  $("app").style.display = "none";
  $("login").style.display = "flex";
}

function enterApp() {
  $("login").style.display = "none";
  $("app").style.display = "block";
  const email = parseJwt(session.access_token)?.email || "signed in";
  $("who").textContent = email;
  bootstrap();
}

function parseJwt(token) {
  try { return JSON.parse(atob(token.split(".")[1])); } catch { return null; }
}

/* ---------- bootstrap + refresh loop ---------- */
async function bootstrap() {
  await loadSeries();
  await refreshAll();
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = setInterval(() => {
    if (document.visibilityState === "visible") refreshAll();
  }, CFG.REFRESH_SECONDS * 1000);
}

async function loadSeries() {
  state.series = await rest("series?select=id,symbol,expiry_date_label&order=symbol");
  if (!state.activeSeries && state.series.length) state.activeSeries = state.series[0].id;

  const opts = state.series
    .map((s) => `<option value="${s.id}">${s.symbol}${s.expiry_date_label ? " · " + s.expiry_date_label : ""}</option>`)
    .join("");
  ["chain-series", "hist-series"].forEach((id) => { $(id).innerHTML = opts; });
  $("spike-series").innerHTML = `<option value="">All expiries</option>` + opts;
  $("chain-series").value = state.activeSeries;
  $("hist-series").value = state.activeSeries;
}

async function refreshAll() {
  const active = document.querySelector(".tab.active").dataset.view;
  try {
    await Promise.all([loadHealth(), renderActiveView(active)]);
  } catch (e) {
    console.error(e);
  }
}

function renderActiveView(view) {
  if (view === "chain") return loadChain();
  if (view === "history") return loadHistory();
  if (view === "spikes") return loadSpikes();
  if (view === "system") return loadSystem();
}

/* ---------- health (topbar + system tab) ---------- */
async function loadHealth() {
  const rows = await rest("worker_health?select=*");
  const ingest = rows.find((r) => r.worker === "ingest");
  const dot = $("health-dot"), txt = $("health-text");
  if (!ingest || !ingest.last_success_at) {
    dot.className = "dot dead"; txt.textContent = "no data yet";
  } else {
    const mins = (Date.now() - new Date(ingest.last_success_at).getTime()) / 60000;
    if (mins < 40) { dot.className = "dot ok"; txt.textContent = `live · ${timeAgo(ingest.last_success_at)}`; }
    else if (mins < 90) { dot.className = "dot stale"; txt.textContent = `stale · ${timeAgo(ingest.last_success_at)}`; }
    else { dot.className = "dot dead"; txt.textContent = `down · ${timeAgo(ingest.last_success_at)}`; }
  }
  state._health = rows;
}

/* ---------- Chain view ---------- */
async function loadChain() {
  const sid = $("chain-series").value;
  state.activeSeries = sid;
  const body = $("chain-body");
  body.innerHTML = `<tr><td colspan="7" class="loading">Loading chain…</td></tr>`;

  const [chain, snaps] = await Promise.all([
    rest(`latest_chain?series_id=eq.${sid}&select=*&order=strike`),
    rest(`snapshots?series_id=eq.${sid}&select=snapshot_ts,spot,atm_iv,days_to_expiry,liquid_strike_count,strike_count&order=snapshot_ts.desc&limit=1`),
  ]);

  const snap = snaps[0];
  renderChainMeta(snap);
  $("chain-updated").textContent = snap ? timeAgo(snap.snapshot_ts) : "";

  if (!chain.length) {
    body.innerHTML = `<tr><td colspan="7" class="empty">No chain data for this expiry yet.</td></tr>`;
  } else {
    const spot = snap?.spot;
    // Mark the row nearest spot so the eye lands on the money instantly.
    let atmStrike = null, best = Infinity;
    if (spot) for (const r of chain) {
      const d = Math.abs(r.strike - spot);
      if (d < best) { best = d; atmStrike = r.strike; }
    }
    body.innerHTML = chain.map((r) => rowHtml(r, r.strike === atmStrike)).join("");
  }

  await loadChainSpikes(sid);
}

function renderChainMeta(snap) {
  const box = $("chain-meta");
  if (!snap) { box.innerHTML = ""; return; }
  box.innerHTML = [
    ["Spot", fmt(snap.spot, 2)],
    ["ATM IV", snap.atm_iv != null ? fmt(snap.atm_iv, 2) : "—"],
    ["DTE", snap.days_to_expiry ?? "—"],
    ["Liquid", `${snap.liquid_strike_count ?? "—"} / ${(snap.strike_count ?? 0) * 2}`],
  ].map(([k, v]) => `<div class="stat"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");
}

function rowHtml(r, isAtm) {
  return `<tr class="${isAtm ? "atm-row" : ""}">
    <td class="l dim">${fmt(r.call_oi, 0)}</td>
    <td class="call">${fmt(r.call_delta, 0)}</td>
    <td class="call">${fmt(r.call_iv, 2)}</td>
    <td class="strike">${fmt(r.strike, 0)}</td>
    <td class="put">${fmt(r.put_iv, 2)}</td>
    <td class="put">${fmt(r.put_delta, 0)}</td>
    <td class="l dim">${fmt(r.put_oi, 0)}</td>
  </tr>`;
}

async function loadChainSpikes(sid) {
  const box = $("chain-spikes");
  const since = new Date(Date.now() - 24 * 3600 * 1000).toISOString();
  const rows = await rest(
    `spike_events?series_id=eq.${sid}&detected_at=gte.${since}&select=*&order=detected_at.desc&limit=30`
  );
  box.innerHTML = rows.length
    ? rows.map(spikeHtml).join("")
    : `<div class="empty">No spikes in the last 24h.</div>`;
}

/* ---------- shared spike renderer ---------- */
function spikeHtml(s) {
  const side = s.side === "c" ? "CALL" : "PUT";
  const adj = s.adj_pct_change;
  return `<div class="spike ${s.severity}">
    <div class="spike-top">
      <span class="spike-title">${side} ${fmt(s.strike, 0)} · ${s.rule}</span>
      <span class="spike-time">${timeAgo(s.detected_at)}</span>
    </div>
    <div class="spike-nums">
      raw <b class="chg-up">${signedPct(s.raw_pct_change)}</b>
      ${adj != null ? `· adj <b class="${adj >= 0 ? "chg-up" : "chg-down"}">${signedPct(adj)}</b>` : `· adj <b>n/a</b>`}
      ${s.spot_move_pct != null ? `· spot ${signedPct(s.spot_move_pct, 2)}` : ""}
    </div>
    ${s.would_suppress ? `<span class="suppress-tag">likely smile roll</span>` : ""}
  </div>`;
}

/* ---------- History view (inline SVG chart) ---------- */
async function loadHistory() {
  const sid = $("hist-series").value;
  await populateStrikeOptions(sid);
  const strike = $("hist-strike").value;
  const side = $("hist-side").value;
  const hours = +$("hist-window").value;
  const wrap = $("hist-chart");
  if (!strike) { wrap.innerHTML = `<div class="empty">No strikes available yet.</div>`; return; }

  wrap.innerHTML = `<div class="loading">Loading…</div>`;
  const since = new Date(Date.now() - hours * 3600 * 1000).toISOString();
  const rows = await rest(
    `iv_ticks?series_id=eq.${sid}&strike=eq.${strike}&side=eq.${side}&snapshot_ts=gte.${since}` +
    `&iv=not.is.null&select=snapshot_ts,iv&order=snapshot_ts`
  );
  $("hist-title").textContent = `${side === "c" ? "Call" : "Put"} ${strike} IV — last ${hours}h`;
  drawChart(wrap, rows.map((r) => [new Date(r.snapshot_ts).getTime(), r.iv]));
}

async function populateStrikeOptions(sid) {
  if ($("hist-strike").dataset.sid === sid && $("hist-strike").options.length) return;
  const rows = await rest(`latest_chain?series_id=eq.${sid}&select=strike&order=strike`);
  $("hist-strike").innerHTML = rows.map((r) => `<option value="${r.strike}">${fmt(r.strike, 0)}</option>`).join("");
  $("hist-strike").dataset.sid = sid;
  // Default to a mid strike rather than the far wing.
  if (rows.length) $("hist-strike").selectedIndex = Math.floor(rows.length / 2);
}

function drawChart(wrap, pts) {
  if (pts.length < 2) { wrap.innerHTML = `<div class="empty">Not enough history for this strike yet — check back after a few more snapshots.</div>`; return; }
  const W = wrap.clientWidth || 800, H = 320, m = { t: 16, r: 16, b: 28, l: 44 };
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  const xmin = Math.min(...xs), xmax = Math.max(...xs);
  let ymin = Math.min(...ys), ymax = Math.max(...ys);
  const pad = (ymax - ymin) * 0.15 || 1; ymin -= pad; ymax += pad;
  const px = (x) => m.l + ((x - xmin) / (xmax - xmin || 1)) * (W - m.l - m.r);
  const py = (y) => m.t + (1 - (y - ymin) / (ymax - ymin || 1)) * (H - m.t - m.b);

  let g = "";
  const yticks = 4;
  for (let i = 0; i <= yticks; i++) {
    const yv = ymin + (i / yticks) * (ymax - ymin), yy = py(yv);
    g += `<line class="grid" x1="${m.l}" y1="${yy}" x2="${W - m.r}" y2="${yy}"/>`;
    g += `<text class="axis-label" x="${m.l - 8}" y="${yy + 3}" text-anchor="end">${yv.toFixed(1)}</text>`;
  }
  const xticks = 4;
  for (let i = 0; i <= xticks; i++) {
    const xv = xmin + (i / xticks) * (xmax - xmin), xx = px(xv);
    const d = new Date(xv), lbl = `${d.getHours()}:${String(d.getMinutes()).padStart(2, "0")}`;
    g += `<text class="axis-label" x="${xx}" y="${H - 10}" text-anchor="middle">${lbl}</text>`;
  }
  const path = pts.map((p, i) => `${i ? "L" : "M"}${px(p[0]).toFixed(1)},${py(p[1]).toFixed(1)}`).join("");
  wrap.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}">${g}<path class="series-line" d="${path}"/></svg>`;
}

/* ---------- Spike Log view ---------- */
async function loadSpikes() {
  const sid = $("spike-series").value;
  const list = $("spike-list");
  list.innerHTML = `<div class="loading">Loading…</div>`;
  const since = new Date(Date.now() - 7 * 24 * 3600 * 1000).toISOString();
  let q = `spike_events?detected_at=gte.${since}&select=*&order=detected_at.desc&limit=300`;
  if (sid) q += `&series_id=eq.${sid}`;
  let rows = await rest(q);

  if (state.spikeFilter === "real") rows = rows.filter((r) => !r.would_suppress);
  else if (state.spikeFilter === "suppressed") rows = rows.filter((r) => r.would_suppress);

  $("spike-count").textContent = `${rows.length} in 7d`;
  list.innerHTML = rows.length
    ? rows.map(spikeHtml).join("")
    : `<div class="empty">Nothing matches this filter.</div>`;
}

/* ---------- System view ---------- */
async function loadSystem() {
  const rows = state._health || (await rest("worker_health?select=*"));
  $("health-panel").innerHTML = rows.length
    ? rows.map((r) => {
        const ok = r.last_success_at && (Date.now() - new Date(r.last_success_at).getTime()) < 40 * 60000;
        return `<div class="log-line">
          <span class="lvl ${ok ? "info" : "error"}">${ok ? "ok" : "check"}</span>
          <span class="l" style="flex:1">${r.worker}</span>
          <span class="ts">${r.last_success_at ? timeAgo(r.last_success_at) : "never"}</span>
        </div>${r.last_error ? `<div class="log-line"><span class="lvl error">err</span><span style="flex:1">${escapeHtml(r.last_error)}</span></div>` : ""}`;
      }).join("")
    : `<div class="empty">No worker has reported in yet.</div>`;

  const logs = await rest("debug_log?select=*&order=logged_at.desc&limit=100");
  $("log-panel").innerHTML = logs.length
    ? logs.map((l) => `<div class="log-line">
        <span class="ts">${new Date(l.logged_at).toLocaleTimeString()}</span>
        <span class="lvl ${l.level}">${l.level}</span>
        <span style="flex:1">${escapeHtml(l.message)}</span>
      </div>`).join("")
    : `<div class="empty">Log is empty.</div>`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

/* ---------- wiring ---------- */
function wire() {
  $("login-btn").addEventListener("click", doLogin);
  $("password").addEventListener("keydown", (e) => { if (e.key === "Enter") doLogin(); });
  $("logout").addEventListener("click", doLogout);

  document.querySelectorAll(".tab").forEach((t) => {
    t.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
      document.querySelectorAll(".view").forEach((x) => x.classList.remove("active"));
      t.classList.add("active");
      $(`view-${t.dataset.view}`).classList.add("active");
      renderActiveView(t.dataset.view);
    });
  });

  $("chain-series").addEventListener("change", loadChain);
  ["hist-series", "hist-strike", "hist-side", "hist-window"].forEach((id) =>
    $(id).addEventListener("change", () => {
      if (id === "hist-series") $("hist-strike").dataset.sid = "";
      loadHistory();
    }));
  $("spike-series").addEventListener("change", loadSpikes);
  document.querySelectorAll(".chip").forEach((c) =>
    c.addEventListener("click", () => {
      document.querySelectorAll(".chip").forEach((x) => x.classList.remove("on"));
      c.classList.add("on");
      state.spikeFilter = c.dataset.filter;
      loadSpikes();
    }));
}

/* ---------- start ---------- */
(function start() {
  if (!CFG || CFG.SUPABASE_URL.includes("YOUR-PROJECT")) {
    document.body.innerHTML = `<div style="padding:40px;font-family:monospace;color:#E5484D">
      config.js still has placeholder values. Add your Supabase URL and anon key, then reload.</div>`;
    return;
  }
  wire();
  const saved = localStorage.getItem("iv_session");
  if (saved) {
    try {
      session = JSON.parse(saved);
      const claims = parseJwt(session.access_token);
      if (claims && claims.exp * 1000 > Date.now()) { enterApp(); return; }
    } catch {}
    localStorage.removeItem("iv_session");
  }
})();
