/* ============================================================
   strategy-builder.js — Payoff Builder tab.

   Add legs (including buy/sell FUTURES), pick strikes from the live
   chain, and see the expiry P&L curve with a crosshair plus a summary
   (breakevens, max profit/loss, net premium, net greeks). Pure client
   side — uses strategy-engine.js for all math, reads latest_chain via
   the same rest() helper as the rest of the dashboard.

   Exposes initStrategy(deps) so app.js can wire it without a circular
   import: app.js owns rest()/state, this module owns the tab.
   ============================================================ */

import {
  payoffCurve, findBreakevens, maxProfitLoss, netGreeks, netEntryPremium, MULTIPLIERS,
} from "./strategy-engine.js";

let rest, state, fmt;          // injected from app.js
let legs = [];
let chain = [];                // latest_chain rows for the active series
let spot = null;
let instrument = "GOLD";

export function initStrategy(deps) {
  rest = deps.rest; state = deps.state; fmt = deps.fmt;
  wireStrategy();
}

/* ---------- data ---------- */
export async function loadStrategyChain() {
  const sid = document.getElementById("strat-series").value;
  if (!sid) return;
  const [rows, snaps] = await Promise.all([
    rest(`latest_chain?series_id=eq.${sid}&select=*&order=strike`),
    rest(`snapshots?series_id=eq.${sid}&select=spot,snapshot_ts&order=snapshot_ts.desc&limit=1`),
  ]);
  chain = rows;
  spot = snaps[0]?.spot ?? null;
  const sym = (state.series.find((s) => String(s.id) === String(sid)) || {}).symbol || "";
  instrument = /SILVER/i.test(sym) ? "SILVER" : /NAS/i.test(sym) ? "NASDAQ" : /SP|ES/i.test(sym) ? "SP500" : "GOLD";

  document.getElementById("strat-spot-label").textContent = spot ? `spot ${fmt(spot, 2)}` : "";
  populateStrikeSelect();
  render();
}

function populateStrikeSelect() {
  const sel = document.getElementById("new-strike");
  sel.innerHTML = chain.map((r) => `<option value="${r.strike}">${fmt(r.strike, 0)}</option>`).join("");
  // default to the strike nearest spot
  if (spot && chain.length) {
    let best = 0, bd = Infinity;
    chain.forEach((r, i) => { const d = Math.abs(r.strike - spot); if (d < bd) { bd = d; best = i; } });
    sel.selectedIndex = best;
  }
  syncEntrySuggestion();
}

/* Suggest an entry price from the chain using the exec convention the
   reference project used everywhere: BUY fills at ask, SELL at bid. */
function chainPriceFor(strike, type, action) {
  const row = chain.find((r) => Math.abs(r.strike - strike) < 1e-6);
  if (!row) return null;
  if (type === "future") return spot;
  const side = type === "call" ? "call" : "put";
  const bid = row[`${side}_bid`], ask = row[`${side}_ask`];
  if (action === "sell") return bid ?? ask ?? null;
  return ask ?? bid ?? null;
}

function greeksFor(strike, type) {
  const row = chain.find((r) => Math.abs(r.strike - strike) < 1e-6);
  if (!row || type === "future") return {};
  const side = type === "call" ? "call" : "put";
  return {
    iv_decimal: row[`${side}_iv`] != null ? row[`${side}_iv`] / 100 : null,
    delta: row[`${side}_delta`], gamma: row[`${side}_gamma`],
    vega: row[`${side}_vega`], theta: row[`${side}_theta`],
  };
}

function syncEntrySuggestion() {
  const type = document.getElementById("new-type").value;
  const action = document.getElementById("new-side").value;
  const strike = +document.getElementById("new-strike").value;
  const entry = document.getElementById("new-entry");
  const hint = document.getElementById("add-leg-hint");
  document.getElementById("new-strike").disabled = type === "future";
  if (type === "future") {
    entry.value = spot != null ? spot.toFixed(2) : "";
    hint.textContent = "Future leg: entry defaults to current spot. Edit if your fill differs.";
  } else {
    const p = chainPriceFor(strike, type, action);
    entry.value = p != null ? p.toFixed(2) : "";
    hint.textContent = p != null
      ? `Suggested from chain (${action === "sell" ? "bid" : "ask"}). Editable.`
      : "No quote for that strike — enter your price.";
  }
}

/* ---------- legs ---------- */
function addLeg() {
  const type = document.getElementById("new-type").value;
  const action = document.getElementById("new-side").value;
  const strike = type === "future" ? (spot || 0) : +document.getElementById("new-strike").value;
  const qty = parseInt(document.getElementById("new-qty").value, 10) || 1;
  const entryRaw = document.getElementById("new-entry").value;
  const entry = parseFloat(entryRaw);
  const hint = document.getElementById("add-leg-hint");
  // A leg with no entry price would sit its payoff line flat on/near
  // zero, producing a meaningless curve and hundreds of false
  // breakevens. Require a real number instead of silently using 0.
  if (!entryRaw || isNaN(entry) || entry <= 0) {
    hint.textContent = "Enter an entry price before adding this leg (no chain quote was available).";
    hint.style.color = "var(--warn)";
    document.getElementById("new-entry").focus();
    return;
  }
  hint.style.color = "";
  legs.push({ option_type: type, action, strike, qty, entry_price: entry, ...greeksFor(strike, type) });
  render();
}

function legRow(leg, i) {
  const sideCls = leg.action === "buy" ? "leg-side-buy" : "leg-side-sell";
  const strikeCell = leg.option_type === "future" ? "—" : fmt(leg.strike, 0);
  return `<tr>
    <td class="l ${sideCls}">${leg.action.toUpperCase()}</td>
    <td class="l">${leg.option_type}</td>
    <td>${strikeCell}</td>
    <td><input type="number" min="1" value="${leg.qty}" data-i="${i}" data-f="qty"></td>
    <td><input type="number" step="0.01" value="${leg.entry_price}" data-i="${i}" data-f="entry_price"></td>
    <td><button class="leg-remove" data-i="${i}">✕</button></td>
  </tr>`;
}

function render() {
  const body = document.getElementById("legs-body");
  const empty = document.getElementById("legs-empty");
  body.innerHTML = legs.map(legRow).join("");
  empty.style.display = legs.length ? "none" : "block";

  body.querySelectorAll("input").forEach((el) =>
    el.addEventListener("change", (e) => {
      const i = +e.target.dataset.i, f = e.target.dataset.f;
      legs[i][f] = f === "qty" ? (parseInt(e.target.value, 10) || 1) : (parseFloat(e.target.value) || 0);
      render();
    }));
  body.querySelectorAll(".leg-remove").forEach((el) =>
    el.addEventListener("click", (e) => { legs.splice(+e.target.dataset.i, 1); render(); }));

  drawPayoff();
  renderSummary();
}

/* ---------- summary ---------- */
function renderSummary() {
  const box = document.getElementById("payoff-summary");
  if (!legs.length) { box.innerHTML = ""; return; }
  const mult = MULTIPLIERS[instrument] || 1;
  const center = spot || (legs.find((l) => l.strike)?.strike) || 4000;
  const lo = center * 0.7, hi = center * 1.3;

  const beList = findBreakevens(legs, lo, hi);
  const bes = beList.length
    ? (beList.length > 4
        ? beList.slice(0, 4).map((b) => fmt(b, 1)).join(", ") + ` +${beList.length - 4} more`
        : beList.map((b) => fmt(b, 1)).join(", "))
    : "None";
  const mpl = maxProfitLoss(legs, lo, hi);
  const prem = netEntryPremium(legs);
  const g = netGreeks(legs);

  const money = (perUnit) => perUnit == null ? null : perUnit * mult;
  const cell = (k, v, cls = "") => `<div class="cell"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`;
  const dollar = (v) => v == null ? "Unlimited" : `$${(v).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;

  box.innerHTML =
    cell("Breakeven", bes) +
    cell("Max profit", mpl.profit_unlimited ? "Unlimited" : dollar(money(mpl.max_profit)), mpl.profit_unlimited || (mpl.max_profit > 0) ? "pos" : "") +
    cell("Max loss", mpl.loss_unlimited ? "Unlimited" : dollar(money(mpl.max_loss)), "neg") +
    cell("Net premium", `${prem >= 0 ? "+" : ""}${dollar(Math.abs(money(prem))).replace("$", prem >= 0 ? "$" : "-$")}`, prem >= 0 ? "pos" : "neg") +
    cell("Net delta", fmt(g.delta, 1)) +
    cell("Net theta", fmt(g.theta, 1));
}

/* ---------- chart ---------- */
function drawPayoff() {
  const wrap = document.getElementById("payoff-chart");
  if (!legs.length) { wrap.innerHTML = `<div class="empty">Add legs to see the payoff.</div>`; return; }
  const mult = MULTIPLIERS[instrument] || 1;
  const center = spot || legs.find((l) => l.strike)?.strike || 4000;
  const lo = center * 0.82, hi = center * 1.18;
  const N = 240;
  const curve = payoffCurve(legs, Array.from({ length: N + 1 }, (_, i) => lo + (i / N) * (hi - lo)))
    .map((p) => ({ spot: p.spot, pnl: p.pnl * mult }));
  const bes = findBreakevens(legs, lo, hi);

  const W = wrap.clientWidth || 800, H = 340, m = { t: 16, r: 16, b: 28, l: 60 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const xmin = lo, xmax = hi;
  let ymin = Math.min(...curve.map((p) => p.pnl)), ymax = Math.max(...curve.map((p) => p.pnl));
  const pad = (ymax - ymin) * 0.12 || 1; ymin -= pad; ymax += pad;
  const px = (x) => m.l + ((x - xmin) / (xmax - xmin)) * iw;
  const py = (y) => m.t + (1 - (y - ymin) / (ymax - ymin)) * ih;

  let g = "";
  for (let i = 0; i <= 4; i++) {
    const yv = ymin + (i / 4) * (ymax - ymin), yy = py(yv);
    g += `<line class="grid" x1="${m.l}" y1="${yy}" x2="${W - m.r}" y2="${yy}"/>`;
    g += `<text class="axis-label" x="${m.l - 8}" y="${yy + 3}" text-anchor="end">$${Math.round(yv).toLocaleString()}</text>`;
  }
  for (let i = 0; i <= 4; i++) {
    const xv = xmin + (i / 4) * (xmax - xmin);
    g += `<text class="axis-label" x="${px(xv)}" y="${H - 9}" text-anchor="middle">${Math.round(xv)}</text>`;
  }

  // profit/loss shaded regions split at zero
  const zeroY = py(0);
  let posArea = `M${px(curve[0].spot)},${zeroY}`, negArea = `M${px(curve[0].spot)},${zeroY}`;
  curve.forEach((p) => { const x = px(p.spot); posArea += ` L${x},${p.pnl >= 0 ? py(p.pnl) : zeroY}`; negArea += ` L${x},${p.pnl < 0 ? py(p.pnl) : zeroY}`; });
  posArea += ` L${px(curve[curve.length - 1].spot)},${zeroY} Z`;
  negArea += ` L${px(curve[curve.length - 1].spot)},${zeroY} Z`;

  const line = curve.map((p, i) => `${i ? "L" : "M"}${px(p.spot).toFixed(1)},${py(p.pnl).toFixed(1)}`).join("");
  const beLines = bes.map((b) => `<line class="be-line" x1="${px(b)}" y1="${m.t}" x2="${px(b)}" y2="${H - m.b}"/>`).join("");
  const spotLine = spot ? `<line class="spot-line" x1="${px(spot)}" y1="${m.t}" x2="${px(spot)}" y2="${H - m.b}"/>` : "";

  wrap.innerHTML = `<div class="chart-host">
    <svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" id="payoff-svg">
      ${g}
      <path class="payoff-pos" d="${posArea}"/>
      <path class="payoff-neg" d="${negArea}"/>
      <line class="zero-line" x1="${m.l}" y1="${zeroY}" x2="${W - m.r}" y2="${zeroY}"/>
      ${beLines}${spotLine}
      <path class="payoff-line" d="${line}"/>
      <g id="pcross" style="opacity:0">
        <line class="crosshair" id="pcx" y1="${m.t}" y2="${H - m.b}"/>
        <circle class="cross-dot" id="pcd" r="4"/>
      </g>
    </svg>
    <div class="chart-tip" id="ptip"></div>
  </div>`;

  const svg = document.getElementById("payoff-svg");
  const cross = document.getElementById("pcross");
  const tip = document.getElementById("ptip");
  const host = wrap.querySelector(".chart-host");
  svg.addEventListener("mousemove", (e) => {
    const rect = svg.getBoundingClientRect();
    const sx = (e.clientX - rect.left) / rect.width * W;
    if (sx < m.l || sx > W - m.r) { cross.style.opacity = 0; tip.style.opacity = 0; return; }
    const sv = xmin + ((sx - m.l) / iw) * (xmax - xmin);
    let near = curve[0], bd = Infinity;
    for (const p of curve) { const d = Math.abs(p.spot - sv); if (d < bd) { bd = d; near = p; } }
    cross.style.opacity = 1;
    document.getElementById("pcx").setAttribute("x1", px(near.spot));
    document.getElementById("pcx").setAttribute("x2", px(near.spot));
    document.getElementById("pcd").setAttribute("cx", px(near.spot));
    document.getElementById("pcd").setAttribute("cy", py(near.pnl));
    tip.innerHTML = `<div class="tip-time">Spot ${near.spot.toFixed(1)}</div>
      <div class="tip-row"><span class="tip-sw" style="background:${near.pnl >= 0 ? "#30D158" : "#FF453A"}"></span>P&L <b>${near.pnl >= 0 ? "+" : ""}$${Math.round(near.pnl).toLocaleString()}</b></div>`;
    tip.style.opacity = 1;
    const hostRect = host.getBoundingClientRect();
    const tx = (px(near.spot) / W) * hostRect.width;
    const flip = tx > hostRect.width - 150;
    tip.style.left = (flip ? tx - tip.offsetWidth - 14 : tx + 14) + "px";
    tip.style.top = "14px";
  });
  svg.addEventListener("mouseleave", () => { cross.style.opacity = 0; tip.style.opacity = 0; });
}

/* ---------- wiring ---------- */
function wireStrategy() {
  document.getElementById("add-leg-btn").addEventListener("click", addLeg);
  document.getElementById("clear-legs").addEventListener("click", () => { legs = []; render(); });
  ["new-type", "new-side", "new-strike"].forEach((id) =>
    document.getElementById(id).addEventListener("change", syncEntrySuggestion));
  document.getElementById("strat-series").addEventListener("change", loadStrategyChain);

  // sub-tab toggle
  document.querySelectorAll("[data-strat-tab]").forEach((b) =>
    b.addEventListener("click", () => {
      document.querySelectorAll("[data-strat-tab]").forEach((x) => x.classList.remove("on"));
      b.classList.add("on");
      const isScanner = b.dataset.stratTab === "scanner";
      document.getElementById("strat-builder").style.display = isScanner ? "none" : "block";
      document.getElementById("strat-scanner").style.display = isScanner ? "block" : "none";
    }));
  document.getElementById("strat-tab-builder").classList.add("on");
}

export function getStrategyLegs() { return legs; }

/* Called by the scanner's "Load →" button: replace the builder's legs
   with a scanned strategy, switch to the Builder sub-tab, and render. */
export function loadLegsIntoBuilder(newLegs) {
  legs = newLegs.map((l) => ({ ...l, qty: l.qty || 1 }));
  document.getElementById("strat-tab-builder").click();
  render();
}
