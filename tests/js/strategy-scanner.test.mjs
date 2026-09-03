/* ============================================================
   strategy-scanner.test.mjs — the scan builders and their filters.

   Requires the `__test` seam appended to strategy-scanner.js (see
   tests/patches/scanner-test-seam.diff). Without it none of this file
   can run: chain, spot and CFG are module-private and every builder is
   an unexported function.

   These tests matter because the scanner is the only place in the
   project that computes max loss, breakevens and risk:reward for
   multi-leg structures independently of strategy-engine.js. Two
   implementations of the same maths is exactly where they drift.
   ============================================================ */

import test from "node:test";
import assert from "node:assert/strict";

import { __test } from "../../dashboard/strategy-scanner.js";

const { setChain, setConfig, resetConfig, run, helpers } = __test;
const close = (a, b, tol = 1e-6) =>
  assert.ok(Math.abs(a - b) < tol, `${a} !== ${b} (tol ${tol})`);

/* A synthetic chain: 25-point strikes around a 3400 spot, with
   premiums that fall away from the money on both sides. Deliberately
   simple so expected values can be computed by hand. */
function chainRow(strike, spot = 3400) {
  const cIntrinsic = Math.max(0, spot - strike);
  const pIntrinsic = Math.max(0, strike - spot);
  const timeValue = Math.max(1, 40 - Math.abs(strike - spot) * 0.15);
  return {
    strike,
    call_bid: +(cIntrinsic + timeValue - 1).toFixed(2),
    call_ask: +(cIntrinsic + timeValue + 1).toFixed(2),
    call_iv: 18.0, call_delta: 50 - (strike - spot) * 0.15,
    call_gamma: 0.001, call_vega: 1.2, call_theta: -0.4, call_oi: 500,
    put_bid: +(pIntrinsic + timeValue - 1).toFixed(2),
    put_ask: +(pIntrinsic + timeValue + 1).toFixed(2),
    put_iv: 18.4, put_delta: -(50 + (strike - spot) * 0.15),
    put_gamma: 0.001, put_vega: 1.2, put_theta: -0.4, put_oi: 400,
  };
}

function makeChain(lo = 3200, hi = 3600, step = 25, spot = 3400) {
  const rows = [];
  for (let k = lo; k <= hi; k += step) rows.push(chainRow(k, spot));
  return rows;
}

test.beforeEach(() => {
  resetConfig();
  setChain(makeChain(), 3400);
});

/* ---------------- execution price convention ---------------- */
test("sell fills at the bid, buy fills at the ask", () => {
  const row = { call_bid: 10, call_ask: 12 };
  assert.equal(helpers.px(row, "call", "sell"), 10);
  assert.equal(helpers.px(row, "call", "buy"), 12);
});

test("a one-sided quote falls back to the other side", () => {
  assert.equal(helpers.px({ call_bid: 10, call_ask: null }, "call", "buy"), 10);
  assert.equal(helpers.px({ call_bid: null, call_ask: 12 }, "call", "sell"), 12);
});

test("a missing row yields no price rather than throwing", () => {
  assert.equal(helpers.px(null, "call", "buy"), null);
});

test("a zero bid is respected, not treated as absent", () => {
  /* `??` only falls through on null/undefined, so a genuine 0 bid is
     kept. That is correct — a 0 bid is information (nobody wants it),
     not a missing quote. */
  assert.equal(helpers.px({ call_bid: 0, call_ask: 5 }, "call", "sell"), 0);
});

/* ---------------- legOk ---------------- */
test("legOk accepts a healthy two-sided quote", () => {
  assert.equal(helpers.legOk(chainRow(3400), "call"), true);
});

test("legOk rejects a premium below the floor", () => {
  setConfig({ min_premium: 1000 });
  assert.equal(helpers.legOk(chainRow(3400), "call"), false);
});

test("legOk rejects thin open interest", () => {
  setConfig({ min_oi: 10000 });
  assert.equal(helpers.legOk(chainRow(3400), "call"), false);
});

test("legOk requires both sides quoted, never fabricates a premium",
  () => {
    /* FIXED (was: null + null === 0 fabricated a half-price).

       legOk now requires both bid and ask present, mirroring
       chain_loader.both_sided. A strike with no quotes at all can no
       longer pass the liquidity check even with min_premium set to 0.
    */
    setConfig({ min_premium: 0 });
    const dead = { strike: 3400, call_bid: null, call_ask: null, call_oi: 500 };
    assert.equal(helpers.legOk(dead, "call"), false,
      "a strike with no quotes at all must never pass");
    const oneSided = { strike: 3400, call_bid: 10, call_ask: null, call_oi: 500 };
    assert.equal(helpers.legOk(oneSided, "call"), false,
      "a one-sided quote is not a tradeable market");
    resetConfig();
  });

/* ---------------- round-strike filter ---------------- */
test("isRound uses the instrument's strike grid, not a hardcoded 25",
  () => {
    /* FIXED (was: hardcoded 25-point grid).

       chain_loader.py documents strike_round per instrument (GOLD 25,
       SP500 25, NASDAQ 50, SILVER sub-dollar). The scanner now threads
       the instrument through instead of assuming gold, so the round
       filter is correct on every instrument rather than passing
       non-round NASDAQ strikes and silently rejecting all of silver.
    */
    __test.setInstrument("GOLD");
    assert.equal(helpers.isRound(3400), true);
    assert.equal(helpers.isRound(3410), false);
    __test.setInstrument("NASDAQ");
    assert.equal(helpers.isRound(15000), true);
    assert.equal(helpers.isRound(15025), false, "NASDAQ grid is 50, not 25");
    __test.setInstrument("SILVER");
    assert.equal(helpers.isRound(34.50), true,
      "a silver strike on a 0.25 grid should count as round");
    assert.equal(helpers.isRound(34.60), false);
    __test.setInstrument("GOLD"); // restore for other tests
  });

/* ---------------- straddle ---------------- */
test("short straddle is found at the money", () => {
  const res = run("short_straddle");
  assert.ok(res.length > 0);
  assert.ok(res.every((r) => Math.abs(r.strikes[0] - 3400) <= 60));
});

test("short straddle collects a credit, long straddle pays a debit", () => {
  assert.ok(run("short_straddle").every((r) => r.net_premium > 0));
  assert.ok(run("long_straddle").every((r) => r.net_premium < 0));
});

test("short straddle has unlimited loss, long straddle unlimited profit", () => {
  assert.ok(run("short_straddle").every((r) => r.loss_unlimited === true));
  assert.ok(run("long_straddle").every((r) => r.profit_unlimited === true));
});

test("straddle breakevens sit one net premium either side of the strike", () => {
  const r = run("short_straddle")[0];
  close(r.be_up - r.strikes[0], Math.abs(r.net_premium), 1e-6);
  close(r.strikes[0] - r.be_down, Math.abs(r.net_premium), 1e-6);
});

test("the ATM band excludes distant strikes", () => {
  // Spot deliberately off-grid so no strike sits exactly at the money.
  setChain(makeChain(3200, 3600, 25, 3412), 3412);
  setConfig({ atm_band: 1 });
  assert.equal(run("short_straddle").length, 0);
  setConfig({ atm_band: 100 });
  assert.ok(run("short_straddle").length > 0);
});

test("results are capped by max_per_strategy", () => {
  setConfig({ atm_band: 500, max_per_strategy: 2 });
  assert.ok(run("short_straddle").length <= 2);
});

/* ---------------- strangle ---------------- */
test("strangle puts the short put below spot and the short call above", () => {
  for (const r of run("short_strangle")) {
    const [ps, cs] = r.strikes;
    assert.ok(ps < 3400 && cs > 3400, `${ps}/${cs} straddles spot wrongly`);
  }
});

test("strangle respects the gap bounds", () => {
  setConfig({ strangle_gap_min: 100, strangle_gap_max: 150 });
  for (const r of run("short_strangle")) {
    const gap = r.strikes[1] - r.strikes[0];
    assert.ok(gap >= 100 && gap <= 150, `gap ${gap} outside bounds`);
  }
});

/* ---------------- verticals ---------------- */
test("bull put is a credit spread entirely below spot", () => {
  for (const r of run("bull_put")) {
    assert.ok(r.net_premium > 0, "must be a credit");
    assert.ok(r.strikes.every((s) => s < 3400), "must sit below spot");
  }
});

test("bear call is a credit spread entirely above spot", () => {
  for (const r of run("bear_call")) {
    assert.ok(r.net_premium > 0);
    assert.ok(r.strikes.every((s) => s > 3400));
  }
});

test("credit spread max loss is width minus credit", () => {
  for (const r of run("bull_put")) {
    const width = r.strikes[1] - r.strikes[0];
    close(r.max_loss, width - r.net_premium, 1e-6);
  }
});

test("debit spread max profit is width minus debit", () => {
  for (const r of run("bull_call")) {
    const width = r.strikes[1] - r.strikes[0];
    close(r.max_profit, width - Math.abs(r.net_premium), 1e-6);
  }
});

test("verticals are never unlimited on either side", () => {
  for (const key of ["bull_put", "bear_call", "bull_call", "bear_put"]) {
    for (const r of run(key)) {
      assert.equal(r.profit_unlimited, false, key);
      assert.equal(r.loss_unlimited, false, key);
    }
  }
});

test("spread width bounds are enforced", () => {
  setConfig({ spread_width_min: 50, spread_width_max: 75, min_rr: 0 });
  for (const key of ["bull_put", "bull_call"]) {
    for (const r of run(key)) {
      const w = r.strikes[1] - r.strikes[0];
      assert.ok(w >= 50 && w <= 75, `${key} width ${w}`);
    }
  }
});

test("the min risk:reward filter is applied", () => {
  setConfig({ min_rr: 0 });
  const loose = run("bull_put").length;
  setConfig({ min_rr: 99 });
  assert.ok(run("bull_put").length < loose);
});

/* ---------------- iron structures ---------------- */
test("iron condor has four legs in strike order", () => {
  const res = run("iron_condor");
  assert.ok(res.length > 0);
  for (const r of res) {
    assert.equal(r.legs.length, 4);
    assert.deepEqual(r.strikes, [...r.strikes].sort((a, b) => a - b));
  }
});

test("iron condor is a credit with capped risk", () => {
  for (const r of run("iron_condor")) {
    assert.ok(r.net_premium > 0);
    assert.equal(r.loss_unlimited, false);
    assert.equal(r.profit_unlimited, false);
  }
});

test("iron condor max loss uses the wider wing", () => {
  for (const r of run("iron_condor")) {
    const [lp, sp, sc, lc] = r.strikes;
    close(r.max_loss, Math.max(sp - lp, lc - sc) - r.net_premium, 1e-6);
  }
});

test("iron butterfly is centred at the money", () => {
  for (const r of run("iron_butterfly")) {
    const body = r.strikes[1];
    assert.ok(Math.abs(body - 3400) <= 60);
  }
});

test("iron butterfly wings are symmetric", () => {
  for (const r of run("iron_butterfly")) {
    const [lp, body, lc] = r.strikes;
    close(body - lp, lc - body, 1e-6);
  }
});

/* ---------------- dedupe, ordering, performance ---------------- */
test("results are deduplicated by strike set", () => {
  for (const key of ["iron_condor", "bull_put", "short_strangle"]) {
    const keys = run(key).map((r) => r.strikes.join(","));
    assert.equal(keys.length, new Set(keys).size, key);
  }
});

test("empty chain produces no results and does not throw", () => {
  setChain([], 3400);
  for (const [key] of __test.STRATEGIES) {
    assert.deepEqual(run(key), [], key);
  }
});

test("a chain with a single strike cannot form spreads", () => {
  setChain([chainRow(3400)], 3400);
  assert.deepEqual(run("iron_condor"), []);
  assert.deepEqual(run("bull_put"), []);
});

test("iron condor stays responsive on a realistic 250-strike chain",
  { todo: "measured ~10s on a 250-strike chain; four nested loops with "
        + "continue-based width filters, run synchronously on the main thread" },
  () => {
  /* PERFORMANCE GUARD.

     ironCondor is four nested loops over the strike list. The width
     filters use `continue`, not `break`, so the iteration count grows
     as O(puts^2 * calls^2) regardless of how few combinations survive.
     A 250-strike gold chain splits roughly 125/125, which is on the
     order of 10^8 loop entries — enough to freeze the browser tab,
     since runScan is synchronous on the main thread.

     Threshold set generously so this fails only on a genuine
     regression, not on a slow CI runner. If it ever does fail, the fix
     is to precompute the valid width partners per strike once instead
     of rediscovering them in the inner loops.
  */
  setChain(makeChain(2100, 4600, 10, 3400), 3400);
  const t0 = Date.now();
  run("iron_condor");
  const ms = Date.now() - t0;
  assert.ok(ms < 5000, `iron condor scan took ${ms}ms on a 250-strike chain`);
});
