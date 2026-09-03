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
const ROUND_STEP = 25;  // gold: multiples of 25 count as "round"
const state = {
  series: [], activeSeries: null,
  filters: { side: "all", type: "all", roundOnly: false, minRaw: null, maxRaw: null, rule: "" },
};
const isRound = (strike) => Math.abs(strike % ROUND_STEP) < 1e-6;
// Rule labels: ema -> EMA, drift_6h -> Drift 6h (tidy, human)
function prettyRule(rule) {
  if (!rule) return "";
  if (rule.toLowerCase() === "ema") return "EMA";
  const m = rule.match(/^drift_(\d+)h$/i);
  if (m) return `Drift ${m[1]}h`;
  return rule.charAt(0).toUpperCase() + rule.slice(1);
}
let _spikeTimer = null;
function debouncedSpikes() { clearTimeout(_spikeTimer); _spikeTimer = setTimeout(loadSpikes, 300); }

/* ---------- tiny helpers ---------- */
const $ = (id) => document.getElementById(id);
const el = (tag, cls, txt) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt != null) e.textContent = txt;
  return e;
};
const fmt = (n, d = 2) => (n == null || isNaN(n) ? "" : Number(n).toFixed(d));
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
    ["ATM IV", snap.atm_iv != null ? fmt(snap.atm_iv, 2) : ""],
    ["DTE", snap.days_to_expiry ?? ""],
    ["Liquid", `${snap.liquid_strike_count ?? ""} / ${(snap.strike_count ?? 0) * 2}`],
  ].map(([k, v]) => `<div class="stat"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");
}

function rowHtml(r, isAtm) {
  const cls = [isAtm ? "atm-row" : "", isRound(r.strike) ? "round-strike" : ""].filter(Boolean).join(" ");
  return `<tr class="${cls}">
    <td class="l dim">${fmt(r.call_oi, 0)}</td>
    <td class="call">${fmt(r.call_delta, 0)}</td>
    <td class="call">${fmt(r.call_iv, 2)}</td>
    <td class="strike c">${fmt(r.strike, 0)}</td>
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
      <span class="spike-title">${side} ${fmt(s.strike, 0)} · ${prettyRule(s.rule)}</span>
      <span class="spike-time">${timeAgo(s.detected_at)}</span>
    </div>
    <div class="spike-nums">
      Raw <b class="chg-up">${signedPct(s.raw_pct_change)}</b>
      ${adj != null ? `· Adj <b class="${adj >= 0 ? "chg-up" : "chg-down"}">${signedPct(adj)}</b>` : `· Adj <b>n/a</b>`}
      ${s.spot_move_pct != null ? `· spot ${signedPct(s.spot_move_pct, 2)}` : ""}
    </div>
    <div class="badges">
      ${s.would_suppress ? `<span class="badge smile">likely smile roll</span>` : `<span class="badge real">real vol move</span>`}
      ${isRound(s.strike) ? `<span class="badge round">round ×25</span>` : ""}
    </div>
  </div>`;
}

/* ---------- History view (inline SVG chart) ---------- */
async function loadHistory() {
  const sid = $("hist-series").value;
  await populateStrikeOptions(sid);
  const strike = $("hist-strike").value;
  const side = $("hist-side").value;
  const hours = +$("hist-window").value;
  const showUnder = $("show-underlying").checked;
  const wrap = $("hist-chart");
  if (!strike) { wrap.innerHTML = `<div class="empty">No strikes available yet.</div>`; return; }

  wrap.innerHTML = `<div class="loading">Loading…</div>`;
  const since = new Date(Date.now() - hours * 3600 * 1000).toISOString();

  // IV series for the chosen strike, and (optionally) the spot path over
  // the same window from the snapshots table — so you can see whether a
  // vol move lined up with the underlying moving.
  const reqs = [rest(
    `iv_ticks?series_id=eq.${sid}&strike=eq.${strike}&side=eq.${side}&snapshot_ts=gte.${since}` +
    `&iv=not.is.null&select=snapshot_ts,iv&order=snapshot_ts`
  )];
  if (showUnder) reqs.push(rest(
    `snapshots?series_id=eq.${sid}&snapshot_ts=gte.${since}&spot=not.is.null&select=snapshot_ts,spot&order=snapshot_ts`
  ));
  const [ivRows, underRows] = await Promise.all(reqs);

  $("hist-title").textContent = `${side === "c" ? "Call" : "Put"} ${strike} IV — last ${hours}h`;
  const iv = ivRows.map((r) => [new Date(r.snapshot_ts).getTime(), r.iv]);
  const under = (underRows || []).map((r) => [new Date(r.snapshot_ts).getTime(), r.spot]);

  $("hist-legend").innerHTML = showUnder && under.length
    ? `<span class="legend-item"><span class="legend-sw" style="background:#2DD4A7"></span>IV %</span>` +
      `<span class="legend-item"><span class="legend-sw" style="background:#FF9F0A"></span>Spot</span>`
    : "";
  drawChart(wrap, iv, showUnder ? under : null);
}

async function populateStrikeOptions(sid) {
  if ($("hist-strike").dataset.sid === sid && $("hist-strike").options.length) return;
  const rows = await rest(`latest_chain?series_id=eq.${sid}&select=strike&order=strike`);
  $("hist-strike").innerHTML = rows.map((r) => `<option value="${r.strike}">${fmt(r.strike, 0)}</option>`).join("");
  $("hist-strike").dataset.sid = sid;
  // Default to a mid strike rather than the far wing.
  if (rows.length) $("hist-strike").selectedIndex = Math.floor(rows.length / 2);
}

function drawChart(wrap, iv, under) {
  if (iv.length < 2) { wrap.innerHTML = `<div class="empty">Not enough history for this strike yet — check back after a few more snapshots.</div>`; return; }
  const W = wrap.clientWidth || 900, H = 360, m = { t: 18, r: under ? 52 : 18, b: 30, l: 52 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;

  const xs = iv.map((p) => p[0]);
  const xmin = Math.min(...xs), xmax = Math.max(...xs);
  const px = (x) => m.l + ((x - xmin) / (xmax - xmin || 1)) * iw;

  // Left axis: IV %.
  const ys = iv.map((p) => p[1]);
  let ymin = Math.min(...ys), ymax = Math.max(...ys);
  const pad = (ymax - ymin) * 0.15 || 1; ymin -= pad; ymax += pad;
  const py = (y) => m.t + (1 - (y - ymin) / (ymax - ymin || 1)) * ih;

  // Right axis: spot, only when overlay is on. Independent scale so the
  // two lines share the plot without one flattening the other.
  let uymin, uymax, upy = null;
  if (under && under.length) {
    const us = under.map((p) => p[1]);
    uymin = Math.min(...us); uymax = Math.max(...us);
    const upad = (uymax - uymin) * 0.15 || 1; uymin -= upad; uymax += upad;
    upy = (y) => m.t + (1 - (y - uymin) / (uymax - uymin || 1)) * ih;
  }

  let g = "";
  const yt = 4;
  for (let i = 0; i <= yt; i++) {
    const yv = ymin + (i / yt) * (ymax - ymin), yy = py(yv);
    g += `<line class="grid" x1="${m.l}" y1="${yy}" x2="${W - m.r}" y2="${yy}"/>`;
    g += `<text class="axis-label" x="${m.l - 8}" y="${yy + 3}" text-anchor="end">${yv.toFixed(1)}%</text>`;
    if (upy) g += `<text class="axis-label" x="${W - m.r + 8}" y="${yy + 3}" text-anchor="start" fill="#FF9F0A">${(uymin + (i/yt)*(uymax-uymin)).toFixed(0)}</text>`;
  }
  const xt = 4;
  for (let i = 0; i <= xt; i++) {
    const xv = xmin + (i / xt) * (xmax - xmin), xx = px(xv);
    const d = new Date(xv), lbl = `${d.getHours()}:${String(d.getMinutes()).padStart(2, "0")}`;
    g += `<text class="axis-label" x="${xx}" y="${H - 10}" text-anchor="middle">${lbl}</text>`;
  }

  const ivLine = iv.map((p, i) => `${i ? "L" : "M"}${px(p[0]).toFixed(1)},${py(p[1]).toFixed(1)}`).join("");
  const area = `M${px(iv[0][0]).toFixed(1)},${py(ymin).toFixed(1)} ` +
    iv.map((p) => `L${px(p[0]).toFixed(1)},${py(p[1]).toFixed(1)}`).join("") +
    ` L${px(iv[iv.length-1][0]).toFixed(1)},${py(ymin).toFixed(1)} Z`;
  let underPath = "";
  if (upy) underPath = `<path class="under-line" d="${under.map((p,i)=>`${i?"L":"M"}${px(p[0]).toFixed(1)},${upy(p[1]).toFixed(1)}`).join("")}"/>`;

  wrap.innerHTML = `<div class="chart-host">
    <svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" id="hist-svg">
      <defs>
        <linearGradient id="lg" x1="0" y1="0" x2="1" y2="0"><stop offset="0%" stop-color="#2DD4A7"/><stop offset="100%" stop-color="#34C7F0"/></linearGradient>
        <linearGradient id="ag" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#2DD4A7" stop-opacity="0.30"/><stop offset="100%" stop-color="#2DD4A7" stop-opacity="0"/></linearGradient>
      </defs>
      ${g}
      <path class="series-area" d="${area}"/>
      <path class="series-line" d="${ivLine}"/>
      ${underPath}
      <g id="cross" style="opacity:0">
        <line class="crosshair" id="cx" y1="${m.t}" y2="${H - m.b}"/>
        <circle class="cross-dot" id="cd" r="4"/>
        ${upy ? '<circle class="cross-dot-u" id="cdu" r="4"/>' : ''}
      </g>
    </svg>
    <div class="chart-tip" id="tip"></div>
  </div>`;

  // --- crosshair interaction (TradingView-style) ---
  const svg = document.getElementById("hist-svg");
  const cross = document.getElementById("cross");
  const tip = document.getElementById("tip");
  const host = wrap.querySelector(".chart-host");

  function nearest(arr, t) {
    let lo = 0, hi = arr.length - 1;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (arr[mid][0] < t) lo = mid + 1; else hi = mid; }
    if (lo > 0 && Math.abs(arr[lo-1][0]-t) < Math.abs(arr[lo][0]-t)) lo--;
    return arr[lo];
  }

  svg.addEventListener("mousemove", (e) => {
    const rect = svg.getBoundingClientRect();
    const sx = (e.clientX - rect.left) / rect.width * W;
    if (sx < m.l || sx > W - m.r) { cross.style.opacity = 0; tip.style.opacity = 0; return; }
    const t = xmin + ((sx - m.l) / iw) * (xmax - xmin);
    const ivPt = nearest(iv, t);
    cross.style.opacity = 1;
    document.getElementById("cx").setAttribute("x1", px(ivPt[0]));
    document.getElementById("cx").setAttribute("x2", px(ivPt[0]));
    document.getElementById("cd").setAttribute("cx", px(ivPt[0]));
    document.getElementById("cd").setAttribute("cy", py(ivPt[1]));
    let underRow = "";
    if (upy && under.length) {
      const uPt = nearest(under, ivPt[0]);
      document.getElementById("cdu").setAttribute("cx", px(uPt[0]));
      document.getElementById("cdu").setAttribute("cy", upy(uPt[1]));
      underRow = `<div class="tip-row"><span class="tip-sw" style="background:#FF9F0A"></span>Spot <b>${uPt[1].toFixed(2)}</b></div>`;
    }
    const d = new Date(ivPt[0]);
    tip.innerHTML = `<div class="tip-time">${d.toLocaleString([], {month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"})}</div>
      <div class="tip-row"><span class="tip-sw" style="background:#2DD4A7"></span>IV <b>${ivPt[1].toFixed(2)}%</b></div>${underRow}`;
    // position tooltip, flipping side near the right edge
    const hostRect = host.getBoundingClientRect();
    const tipX = (px(ivPt[0]) / W) * hostRect.width;
    tip.style.opacity = 1;
    const flip = tipX > hostRect.width - 160;
    tip.style.left = (flip ? tipX - tip.offsetWidth - 14 : tipX + 14) + "px";
    tip.style.top = "18px";
  });
  svg.addEventListener("mouseleave", () => { cross.style.opacity = 0; tip.style.opacity = 0; });
}

/* ---------- Spike Log view ---------- */
async function loadSpikes() {
  const sid = $("spike-series").value;
  const f = state.filters;
  const list = $("spike-list");
  list.innerHTML = `<div class="loading">Loading…</div>`;
  const since = new Date(Date.now() - 7 * 24 * 3600 * 1000).toISOString();
  let q = `spike_events?detected_at=gte.${since}&select=*&order=detected_at.desc&limit=500`;
  if (sid) q += `&series_id=eq.${sid}`;
  if (f.rule) q += `&rule=eq.${f.rule}`;
  let rows = await rest(q);

  // Client-side filters (kept here so combining them stays simple and
  // instant — the dataset per week is small enough that this is free).
  if (f.side !== "all") rows = rows.filter((r) => r.side === f.side);
  if (f.type === "real") rows = rows.filter((r) => !r.would_suppress);
  else if (f.type === "smile") rows = rows.filter((r) => r.would_suppress);
  if (f.roundOnly) rows = rows.filter((r) => isRound(r.strike));
  if (f.minRaw != null) rows = rows.filter((r) => r.raw_pct_change * 100 >= f.minRaw);
  if (f.maxRaw != null) rows = rows.filter((r) => r.raw_pct_change * 100 <= f.maxRaw);

  $("spike-count").textContent = `${rows.length} shown`;
  list.innerHTML = rows.length
    ? rows.map(spikeHtml).join("")
    : `<div class="empty">No detections match these filters. Try widening them.</div>`;
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
  ["hist-series", "hist-strike", "hist-side", "hist-window", "show-underlying"].forEach((id) =>
    $(id).addEventListener("change", () => {
      if (id === "hist-series") $("hist-strike").dataset.sid = "";
      loadHistory();
    }));
  $("spike-series").addEventListener("change", loadSpikes);

  // Side chips (all / calls / puts) and type chips (any / real / smile)
  // are two independent single-select groups.
  function bindChipGroup(groupId, key, attr) {
    document.querySelectorAll(`#${groupId} .chip`).forEach((c) =>
      c.addEventListener("click", () => {
        document.querySelectorAll(`#${groupId} .chip`).forEach((x) => x.classList.remove("on"));
        c.classList.add("on");
        state.filters[key] = c.dataset[attr];
        loadSpikes();
      }));
  }
  bindChipGroup("side-chips", "side", "side");
  bindChipGroup("type-chips", "type", "type");

  $("round-only").addEventListener("change", (e) => { state.filters.roundOnly = e.target.checked; loadSpikes(); });
  $("spike-rule").addEventListener("change", (e) => { state.filters.rule = e.target.value; loadSpikes(); });
  const num = (v) => (v === "" || isNaN(+v) ? null : +v);
  $("min-raw").addEventListener("input", (e) => { state.filters.minRaw = num(e.target.value); debouncedSpikes(); });
  $("max-raw").addEventListener("input", (e) => { state.filters.maxRaw = num(e.target.value); debouncedSpikes(); });
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
