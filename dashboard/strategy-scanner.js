/* ============================================================
   strategy-scanner.js — Strategy Scanner tab.

   Scans the live chain for the same strategies the reference project's
   strategy_scanner.py supported (straddles, strangles, the four
   vertical spreads, iron condor, iron butterfly), applying the same
   filters (min R:R, min premium, min OI, round-strikes, delta target),
   and shows results as cards. "Load into builder" hands a result's
   legs straight to the Payoff Builder.

   Same conventions as the reference, kept so results are comparable:
     - exec price: SELL fills at bid, BUY at ask
     - net premium: credit positive, debit negative
     - max loss / breakevens computed per strategy shape
   Math uses strategy-engine.js so the numbers match the builder.
   ============================================================ */

let rest, state, fmt, loadLegsIntoBuilder;
let chain = [], spot = null, instrument = "GOLD";

const CFG = {
  min_rr: 0.3, min_premium: 0.5, min_oi: 0,
  round_only: false, delta_active: false, target_delta: 50, delta_tol: 10,
  spread_width_min: 25, spread_width_max: 200,
  strangle_gap_min: 25, strangle_gap_max: 300,
  atm_band: 60, max_per_strategy: 5,
};

const STRATEGIES = [
  ["short_straddle", "Short Straddle"], ["long_straddle", "Long Straddle"],
  ["short_strangle", "Short Strangle"], ["long_strangle", "Long Strangle"],
  ["bull_put", "Bull Put Spread"], ["bear_call", "Bear Call Spread"],
  ["bull_call", "Bull Call Spread"], ["bear_put", "Bear Put Spread"],
  ["iron_condor", "Iron Condor"], ["iron_butterfly", "Iron Butterfly"],
];
let selected = new Set(["short_straddle", "short_strangle", "iron_condor"]);

export function initScanner(deps) {
  rest = deps.rest; state = deps.state; fmt = deps.fmt;
  loadLegsIntoBuilder = deps.loadLegsIntoBuilder;
  renderScannerControls();
  wireScanner();
}

/* ---------- chain helpers (exec-price convention) ---------- */
const byStrike = () => { const m = {}; for (const r of chain) m[r.strike] = r; return m; };
function px(row, side, action) {
  if (!row) return null;
  const bid = row[`${side}_bid`], ask = row[`${side}_ask`];
  return action === "sell" ? (bid ?? ask ?? null) : (ask ?? bid ?? null);
}
function legOk(row, side) {
  if (!row) return false;
  // Require BOTH sides quoted. The old mid was (buy + sell) / 2, but
  // null + null === 0 in JS, so a completely unquoted strike scored a
  // premium of 0 — rejected only incidentally because 0 < min_premium.
  // With min_premium set to 0 (which the UI allows) an unquoted strike
  // slipped through and got built into a tradeable-looking spread with
  // a null entry price. Mirror chain_loader.both_sided: no bid or no ask
  // means not liquid, full stop.
  const bid = row[`${side}_bid`], ask = row[`${side}_ask`];
  if (bid == null || ask == null) return false;
  const mid = (bid + ask) / 2;
  if (mid < CFG.min_premium) return false;
  if (CFG.min_oi > 0 && (row[`${side}_oi`] ?? 0) < CFG.min_oi) return false;
  return true;
}

// Per-instrument strike grid, matching chain_loader.INSTRUMENT_PROFILES'
// strike_round. The old version hardcoded 25 (gold), so on NASDAQ it
// passed non-round strikes and on silver — sub-dollar strikes — EVERY
// strike failed and the filter silently returned nothing.
const ROUND_STEP = { GOLD: 25, SP500: 25, NASDAQ: 50, SILVER: 0.25 };
function roundStep() { return ROUND_STEP[instrument] || 25; }
function isRound(s) {
  const step = roundStep();
  return Math.abs(s / step - Math.round(s / step)) < 1e-6;
}
function passOI(rows) {
  if (CFG.min_oi <= 0) return true;
  return rows.every((r) => (r.row[`${r.side}_oi`] ?? 0) >= CFG.min_oi);
}

/* build a leg object for the engine + display */
function mkLeg(row, side, action) {
  const price = px(row, side, action);
  return {
    option_type: side, action, strike: row.strike, qty: 1, entry_price: price,
    iv_decimal: row[`${side}_iv`] != null ? row[`${side}_iv`] / 100 : null,
    delta: row[`${side}_delta`], gamma: row[`${side}_gamma`],
    vega: row[`${side}_vega`], theta: row[`${side}_theta`],
    _price: price, _oi: row[`${side}_oi`], _iv: row[`${side}_iv`],
  };
}
const rr = (credit, maxLoss) => (credit == null || maxLoss == null || maxLoss <= 0 ? null : credit / maxLoss);

/* ---------- strategies ---------- */
function straddle(long) {
  const out = [];
  for (const r of chain) {
    if (Math.abs(r.strike - spot) > CFG.atm_band) continue;
    if (!legOk(r, "call") || !legOk(r, "put")) continue;
    const act = long ? "buy" : "sell";
    const cp = px(r, "call", act), pp = px(r, "put", act);
    if (cp == null || pp == null) continue;
    const legs = [mkLeg(r, "call", act), mkLeg(r, "put", act)];
    if (!passOI(legs.map((l) => ({ row: r, side: l.option_type })))) continue;
    const net = long ? -(cp + pp) : cp + pp;
    const w = Math.abs(net);
    out.push(result(`${long ? "Long" : "Short"} Straddle @ ${r.strike.toFixed(0)}`, legs, net,
      long ? null : net, long ? w : null, r.strike + w, r.strike - w, null, [r.strike], Math.abs(r.strike - spot)));
  }
  return top(out);
}

function strangle(long) {
  const out = [], m = byStrike(), strikes = Object.keys(m).map(Number).sort((a, b) => a - b);
  const act = long ? "buy" : "sell";
  for (const ps of strikes) {
    if (ps >= spot) continue;
    const pr = m[ps]; if (!legOk(pr, "put")) continue;
    const ppv = px(pr, "put", act); if (ppv == null) continue;
    for (const cs of strikes) {
      if (cs <= spot) continue;
      const gap = cs - ps;
      if (gap < CFG.strangle_gap_min) continue;
      if (gap > CFG.strangle_gap_max) break;
      const cr = m[cs]; if (!legOk(cr, "call")) continue;
      const cpv = px(cr, "call", act); if (cpv == null) continue;
      const legs = [mkLeg(pr, "put", act), mkLeg(cr, "call", act)];
      if (!passOI([{ row: pr, side: "put" }, { row: cr, side: "call" }])) continue;
      const net = long ? -(ppv + cpv) : ppv + cpv;
      const w = Math.abs(net);
      out.push(result(`${long ? "Long" : "Short"} Strangle P${ps.toFixed(0)}/C${cs.toFixed(0)}`, legs, net,
        long ? null : net, long ? w : null, cs + w, ps - w, null, [ps, cs]));
    }
  }
  return top(out);
}

function verticalCredit(side, bull) {
  // bull_put (sell higher put, buy lower put) or bear_call (sell lower
  // call, buy higher call) — both credit spreads.
  const out = [], m = byStrike(), strikes = Object.keys(m).map(Number).sort((a, b) => a - b);
  for (const sl of strikes) {
    for (const sh of strikes) {
      if (sh <= sl) continue;
      const w = sh - sl;
      if (w < CFG.spread_width_min) continue;
      if (w > CFG.spread_width_max) break;
      let sellRow, buyRow, legs;
      if (bull) { // bull put: sell sh, buy sl
        if (sl >= spot || sh >= spot) continue;
        sellRow = m[sh]; buyRow = m[sl];
        if (!legOk(sellRow, "put") || !legOk(buyRow, "put")) continue;
        legs = [mkLeg(sellRow, "put", "sell"), mkLeg(buyRow, "put", "buy")];
      } else { // bear call: sell sl, buy sh
        if (sl <= spot) continue;
        sellRow = m[sl]; buyRow = m[sh];
        if (!legOk(sellRow, "call") || !legOk(buyRow, "call")) continue;
        legs = [mkLeg(sellRow, "call", "sell"), mkLeg(buyRow, "call", "buy")];
      }
      const net = legs[0]._price - legs[1]._price;
      if (net < CFG.min_premium) continue;
      const ml = w - net;
      const rv = rr(net, ml);
      if (rv != null && rv < CFG.min_rr) continue;
      if (!passOI(legs.map((l) => ({ row: l.option_type === "put" ? (l.action === "sell" ? sellRow : buyRow) : (l.action === "sell" ? sellRow : buyRow), side: l.option_type })))) continue;
      const be = bull ? sh - net : sl + net;
      out.push(result(`${bull ? "Bull Put" : "Bear Call"} Spread ${sl.toFixed(0)}/${sh.toFixed(0)}`,
        legs, net, net, ml, bull ? null : be, bull ? be : null, rv, [sl, sh]));
    }
  }
  return top(out);
}

function verticalDebit(bull) {
  // bull_call (buy lower, sell higher) / bear_put (buy higher, sell lower)
  const out = [], m = byStrike(), strikes = Object.keys(m).map(Number).sort((a, b) => a - b);
  for (const sl of strikes) {
    for (const sh of strikes) {
      if (sh <= sl) continue;
      const w = sh - sl;
      if (w < CFG.spread_width_min) continue;
      if (w > CFG.spread_width_max) break;
      let legs, net;
      if (bull) { // bull call: buy sl, sell sh
        const bl = m[sl], slr = m[sh];
        if (!legOk(bl, "call") || !legOk(slr, "call")) continue;
        legs = [mkLeg(bl, "call", "buy"), mkLeg(slr, "call", "sell")];
        net = -(legs[0]._price - legs[1]._price); // debit (negative)
      } else { // bear put: buy sh, sell sl
        const bh = m[sh], sll = m[sl];
        if (!legOk(bh, "put") || !legOk(sll, "put")) continue;
        legs = [mkLeg(bh, "put", "buy"), mkLeg(sll, "put", "sell")];
        net = -(legs[0]._price - legs[1]._price);
      }
      const debit = Math.abs(net);
      if (debit < CFG.min_premium) continue;
      const maxProfit = w - debit;
      const rv = rr(maxProfit, debit);
      if (rv != null && rv < CFG.min_rr) continue;
      const be = bull ? sl + debit : sh - debit;
      out.push(result(`${bull ? "Bull Call" : "Bear Put"} Spread ${sl.toFixed(0)}/${sh.toFixed(0)}`,
        legs, net, maxProfit, debit, bull ? null : be, bull ? be : null, rv, [sl, sh]));
    }
  }
  return top(out);
}

function ironCondor() {
  // An iron condor is a put credit spread + a call credit spread. The
  // old version searched all four legs in four nested loops with
  // continue-based width filters — O(puts^2 * calls^2), ~10s and a
  // frozen tab on a 250-strike chain. Here each spread side is built
  // ONCE (O(n^2) each), pre-filtered for liquidity and width, then the
  // two lists are paired. Same results, but the pairing loop only ever
  // sees combinations that already passed every per-side check.
  const out = [], m = byStrike(), strikes = Object.keys(m).map(Number).sort((a, b) => a - b);
  const puts = strikes.filter((s) => s < spot), calls = strikes.filter((s) => s > spot);

  // Valid put spreads: short higher put (sp), long lower put (lp).
  const putSpreads = [];
  for (let a = 0; a < puts.length; a++) {
    const sp = puts[a];
    if (!legOk(m[sp], "put")) continue;
    for (let b = 0; b < a; b++) {
      const lp = puts[b];
      const pw = sp - lp;
      if (pw < CFG.spread_width_min) break;      // puts ascending: wider only as b decreases
      if (pw > CFG.spread_width_max) continue;
      if (!legOk(m[lp], "put")) continue;
      putSpreads.push({ sp, lp, pw, credit: px(m[sp], "put", "sell") - px(m[lp], "put", "buy") });
    }
  }
  // Valid call spreads: short lower call (sc), long higher call (lc).
  const callSpreads = [];
  for (let a = 0; a < calls.length; a++) {
    const sc = calls[a];
    if (!legOk(m[sc], "call")) continue;
    for (let b = a + 1; b < calls.length; b++) {
      const lc = calls[b];
      const cw = lc - sc;
      if (cw < CFG.spread_width_min) continue;
      if (cw > CFG.spread_width_max) break;      // calls ascending: only wider from here
      if (!legOk(m[lc], "call")) continue;
      callSpreads.push({ sc, lc, cw, credit: px(m[sc], "call", "sell") - px(m[lc], "call", "buy") });
    }
  }

  for (const ps of putSpreads) {
    for (const cs of callSpreads) {
      const net = ps.credit + cs.credit;
      if (net < CFG.min_premium) continue;
      const ml = Math.max(ps.pw, cs.cw) - net;
      const rv = rr(net, ml);
      if (rv != null && rv < CFG.min_rr) continue;
      const legs = [mkLeg(m[ps.sp], "put", "sell"), mkLeg(m[ps.lp], "put", "buy"),
                    mkLeg(m[cs.sc], "call", "sell"), mkLeg(m[cs.lc], "call", "buy")];
      out.push(result(`Iron Condor ${ps.lp.toFixed(0)}/${ps.sp.toFixed(0)}/${cs.sc.toFixed(0)}/${cs.lc.toFixed(0)}`,
        legs, net, net, ml, cs.sc + net, ps.sp - net, rv, [ps.lp, ps.sp, cs.sc, cs.lc]));
    }
  }
  return top(out);
}

function ironButterfly() {
  const out = [], m = byStrike(), strikes = Object.keys(m).map(Number).sort((a, b) => a - b);
  for (const body of strikes) {
    if (Math.abs(body - spot) > CFG.atm_band) continue;
    for (const w of [CFG.spread_width_min, 50, 100, 150, 200].filter((x) => x >= CFG.spread_width_min && x <= CFG.spread_width_max)) {
      const lp = body - w, lc = body + w;
      if (!m[lp] || !m[lc]) continue;
      if (!legOk(m[body], "put") || !legOk(m[body], "call") || !legOk(m[lp], "put") || !legOk(m[lc], "call")) continue;
      const legs = [mkLeg(m[body], "put", "sell"), mkLeg(m[body], "call", "sell"),
                    mkLeg(m[lp], "put", "buy"), mkLeg(m[lc], "call", "buy")];
      const net = legs[0]._price + legs[1]._price - legs[2]._price - legs[3]._price;
      if (net < CFG.min_premium) continue;
      const ml = w - net;
      const rv = rr(net, ml);
      if (rv != null && rv < CFG.min_rr) continue;
      out.push(result(`Iron Butterfly ${lp.toFixed(0)}/${body.toFixed(0)}/${lc.toFixed(0)}`,
        legs, net, net, ml, body + net, body - net, rv, [lp, body, lc], Math.abs(body - spot)));
    }
  }
  return top(out);
}

function result(desc, legs, net, maxProfit, maxLoss, beUp, beDown, rv, strikes, atmDist = 0) {
  return { desc, legs, net_premium: net, max_profit: maxProfit, max_loss: maxLoss,
    profit_unlimited: maxProfit == null, loss_unlimited: maxLoss == null,
    be_up: beUp, be_down: beDown, be_width: (beUp != null && beDown != null) ? beUp - beDown : null,
    rr: rv, strikes, _atmDist: atmDist };
}

/* filter (round/delta), dedupe by strike set, sort, cap */
function top(results) {
  let r = results;
  if (CFG.round_only) r = r.filter((x) => x.strikes.every(isRound));
  if (CFG.delta_active) {
    r = r.filter((x) => {
      const nd = x.legs.reduce((s, l) => s + (l.action === "buy" ? 1 : -1) * (l.delta || 0), 0);
      return Math.abs(Math.abs(nd) - CFG.target_delta) <= CFG.delta_tol;
    });
  }
  const seen = new Set(), uniq = [];
  for (const x of r) { const k = x.strikes.join(","); if (!seen.has(k)) { seen.add(k); uniq.push(x); } }
  uniq.sort((a, b) => {
    if (a._atmDist !== b._atmDist && (a._atmDist || b._atmDist)) return a._atmDist - b._atmDist;
    return (b.rr || 0) - (a.rr || 0) || Math.abs(b.net_premium) - Math.abs(a.net_premium);
  });
  return uniq.slice(0, CFG.max_per_strategy);
}

/* ---------- run + render ---------- */
const RUNNERS = {
  short_straddle: () => straddle(false), long_straddle: () => straddle(true),
  short_strangle: () => strangle(false), long_strangle: () => strangle(true),
  bull_put: () => verticalCredit("put", true), bear_call: () => verticalCredit("call", false),
  bull_call: () => verticalDebit(true), bear_put: () => verticalDebit(false),
  iron_condor: ironCondor, iron_butterfly: ironButterfly,
};

export async function runScan() {
  const sid = document.getElementById("strat-series").value;
  const listEl = document.getElementById("scan-results");
  if (!sid) return;
  listEl.innerHTML = `<div class="loading">Scanning…</div>`;
  const [rows, snaps] = await Promise.all([
    rest(`latest_chain?series_id=eq.${sid}&select=*&order=strike`),
    rest(`snapshots?series_id=eq.${sid}&select=spot&order=snapshot_ts.desc&limit=1`),
  ]);
  chain = rows; spot = snaps[0]?.spot ?? null;
  const sym = (state.series.find((s) => String(s.id) === String(sid)) || {}).symbol || "";
  instrument = /SILVER|\bSI\b/i.test(sym) ? "SILVER" : /NAS|NQ/i.test(sym) ? "NASDAQ"
             : /SP|ES/i.test(sym) ? "SP500" : "GOLD";
  if (!spot) { listEl.innerHTML = `<div class="empty">No spot price yet — can't scan.</div>`; return; }

  const sections = [];
  for (const [key, name] of STRATEGIES) {
    if (!selected.has(key)) continue;
    let res = [];
    try { res = RUNNERS[key](); } catch (e) { console.error(key, e); }
    if (res.length) sections.push({ name, res });
  }
  if (!sections.length) { listEl.innerHTML = `<div class="empty">No strategies matched your filters. Try lowering Min R:R or Min premium.</div>`; return; }
  listEl.innerHTML = sections.map(sectionHtml).join("");
  wireResultButtons();
}

function sectionHtml(sec) {
  return `<div class="scan-section">
    <div class="scan-section-head">${sec.name} · ${sec.res.length}</div>
    ${sec.res.map(cardHtml).join("")}
  </div>`;
}

function cardHtml(r, i) {
  const dollar = (v) => v == null ? "Unlimited" : `$${Math.round(Math.abs(v) * 100).toLocaleString()}`;
  const rrTxt = r.rr != null ? r.rr.toFixed(2) : "—";
  const legsTxt = r.legs.map((l) =>
    `<div class="scan-leg"><span class="${l.action === "buy" ? "leg-side-buy" : "leg-side-sell"}">${l.action.toUpperCase()}</span> ${l.option_type} ${l.strike.toFixed(0)} @ ${fmt(l._price, 2)}</div>`).join("");
  const key = btoa(JSON.stringify(r.legs.map((l) => ({ option_type: l.option_type, action: l.action, strike: l.strike, qty: 1, entry_price: l._price, iv_decimal: l.iv_decimal, delta: l.delta, gamma: l.gamma, vega: l.vega, theta: l.theta }))));
  return `<div class="scan-card">
    <div class="scan-card-top">
      <span class="scan-desc">${r.desc}</span>
      <button class="chip scan-load" data-legs="${key}">Load →</button>
    </div>
    <div class="scan-legs">${legsTxt}</div>
    <div class="scan-metrics">
      <span>Credit/Debit <b class="${r.net_premium >= 0 ? "chg-up" : "chg-down"}">${r.net_premium >= 0 ? "+" : "−"}${dollar(r.net_premium)}</b></span>
      <span>Max profit <b>${r.profit_unlimited ? "Unlimited" : dollar(r.max_profit)}</b></span>
      <span>Max loss <b>${r.loss_unlimited ? "Unlimited" : dollar(r.max_loss)}</b></span>
      <span>R:R <b>${rrTxt}</b></span>
    </div>
  </div>`;
}

function wireResultButtons() {
  document.querySelectorAll(".scan-load").forEach((b) =>
    b.addEventListener("click", () => {
      const legs = JSON.parse(atob(b.dataset.legs));
      loadLegsIntoBuilder(legs);
    }));
}

/* ---------- controls ---------- */
function renderScannerControls() {
  const box = document.getElementById("scan-strategies");
  if (box) box.innerHTML = STRATEGIES.map(([k, n]) =>
    `<button class="chip ${selected.has(k) ? "on" : ""}" data-strat="${k}">${n}</button>`).join("");
}

function wireScanner() {
  document.getElementById("scan-run").addEventListener("click", runScan);
  document.querySelectorAll("#scan-strategies .chip").forEach((c) =>
    c.addEventListener("click", () => {
      const k = c.dataset.strat;
      if (selected.has(k)) { selected.delete(k); c.classList.remove("on"); }
      else { selected.add(k); c.classList.add("on"); }
    }));
  const bind = (id, key, isNum = true) => {
    const el = document.getElementById(id); if (!el) return;
    el.addEventListener("input", (e) => { CFG[key] = isNum ? (parseFloat(e.target.value) || 0) : e.target.value; });
  };
  bind("scan-min-rr", "min_rr"); bind("scan-min-prem", "min_premium"); bind("scan-min-oi", "min_oi");
  bind("scan-target-delta", "target_delta"); bind("scan-delta-tol", "delta_tol");
  document.getElementById("scan-round").addEventListener("change", (e) => { CFG.round_only = e.target.checked; });
  document.getElementById("scan-delta-active").addEventListener("change", (e) => { CFG.delta_active = e.target.checked; });
}

/* ============================================================
   TEST SEAM — added to make the strategy builders testable.

   Everything above keeps its module-private state (chain, spot, CFG,
   selected) exactly as before; this block only exposes a controlled
   way to set that state and invoke the builders directly, without a
   DOM, a Supabase round trip, or a user click.

   Nothing in the application imports this. It exists so the pure
   scanning logic — which is where the money maths lives — can be
   covered by tests. If you would rather not ship a test hook in
   production code, the alternative is to lift chain/spot/CFG into a
   context object passed to each builder; that is a larger change with
   the same testing benefit.
   ============================================================ */
export const __test = {
  setChain(rows, spotPrice) { chain = rows; spot = spotPrice; },
  setInstrument(name) { instrument = name; },
  setConfig(over) { Object.assign(CFG, over); },
  resetConfig() {
    Object.assign(CFG, {
      min_rr: 0.3, min_premium: 0.5, min_oi: 0,
      round_only: false, delta_active: false, target_delta: 50, delta_tol: 10,
      spread_width_min: 25, spread_width_max: 200,
      strangle_gap_min: 25, strangle_gap_max: 300,
      atm_band: 60, max_per_strategy: 5,
    });
  },
  get config() { return CFG; },
  run(key) { return RUNNERS[key](); },
  helpers: { px, legOk, mkLeg, passOI, isRound, top, result },
  STRATEGIES,
};
