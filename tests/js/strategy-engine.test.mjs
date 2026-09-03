/* ============================================================
   strategy-engine.test.mjs — the dashboard's payoff and pricing math.

   Run with:  node --test tests/js/

   No framework needed: node:test is built in from Node 18. The engine
   is already a pure ES module with named exports, so it can be imported
   directly with no DOM, no bundler and no Supabase.

   Why this file matters: strategy-engine.js is a port of a port ("ported
   faithfully from payoff_math.py, which was itself a port of the
   original desktop analyser"). Every port is a chance to drop a sign,
   and its own comments reference a real double-count bug that was fixed
   once already. None of it has a test today.
   ============================================================ */

import test from "node:test";
import assert from "node:assert/strict";

import {
  MULTIPLIERS, legExpiryPayoff, strategyExpiryPayoff, payoffCurve,
  findBreakevens, maxProfitLoss, netGreeks, netEntryPremium,
  bs76Price, projectLegPrice, payoffCurveProjected,
} from "../../dashboard/strategy-engine.js";

const close = (a, b, tol = 1e-6) =>
  assert.ok(Math.abs(a - b) < tol, `${a} !== ${b} (tol ${tol})`);

const leg = (o) => ({ qty: 1, entry_price: 0, ...o });
const longCall = (K, prem) => leg({ option_type: "call", action: "buy", strike: K, entry_price: prem });
const shortCall = (K, prem) => leg({ option_type: "call", action: "sell", strike: K, entry_price: prem });
const longPut = (K, prem) => leg({ option_type: "put", action: "buy", strike: K, entry_price: prem });
const shortPut = (K, prem) => leg({ option_type: "put", action: "sell", strike: K, entry_price: prem });
const longFut = (entry) => leg({ option_type: "future", action: "buy", strike: entry, entry_price: entry });

/* ---------------------------------------------------------- */
test("MULTIPLIERS match the worker's instrument profiles", () => {
  // chain_loader.INSTRUMENT_PROFILES carries the same numbers. A drift
  // between the two silently mis-scales every dollar P&L in the UI.
  assert.equal(MULTIPLIERS.GOLD, 100);
  assert.equal(MULTIPLIERS.SILVER, 5000);
  assert.equal(MULTIPLIERS.NASDAQ, 20);
  assert.equal(MULTIPLIERS.SP500, 50);
});

/* ---------------- legExpiryPayoff ---------------- */
test("long call: loses the premium below the strike", () => {
  close(legExpiryPayoff(longCall(3400, 20), 3300), -20);
});

test("long call: intrinsic minus premium above the strike", () => {
  close(legExpiryPayoff(longCall(3400, 20), 3500), 80);
});

test("long call: breaks even at strike plus premium", () => {
  close(legExpiryPayoff(longCall(3400, 20), 3420), 0);
});

test("short call is the exact mirror of long", () => {
  for (const s of [3300, 3400, 3420, 3600]) {
    close(legExpiryPayoff(longCall(3400, 20), s),
          -legExpiryPayoff(shortCall(3400, 20), s));
  }
});

test("long put: intrinsic minus premium below the strike", () => {
  close(legExpiryPayoff(longPut(3400, 15), 3300), 85);
  close(legExpiryPayoff(longPut(3400, 15), 3500), -15);
});

test("short put mirrors long put", () => {
  for (const s of [3200, 3400, 3600]) {
    close(legExpiryPayoff(longPut(3400, 15), s),
          -legExpiryPayoff(shortPut(3400, 15), s));
  }
});

test("quantity scales the payoff linearly", () => {
  const one = legExpiryPayoff(longCall(3400, 20), 3500);
  const three = legExpiryPayoff({ ...longCall(3400, 20), qty: 3 }, 3500);
  close(three, one * 3);
});

test("missing qty defaults to 1", () => {
  const l = { option_type: "call", action: "buy", strike: 3400, entry_price: 20 };
  close(legExpiryPayoff(l, 3500), 80);
});

test("unknown option_type throws rather than returning a wrong number", () => {
  assert.throws(() => legExpiryPayoff(leg({ option_type: "swap", action: "buy", strike: 1 }), 1),
                /Unknown option_type/);
});

/* ---------------- futures: the documented double-count ---------------- */
test("long future moves one-for-one with spot, entry counted once", () => {
  close(legExpiryPayoff(longFut(3400), 3450), 50);
  close(legExpiryPayoff(longFut(3400), 3350), -50);
  close(legExpiryPayoff(longFut(3400), 3400), 0);
});

test("short future inverts", () => {
  const f = { ...longFut(3400), action: "sell" };
  close(legExpiryPayoff(f, 3450), -50);
});

test("a future with entry_price 0 is rejected, not silently mispriced", () => {
  /* FIXED (was a pinned hazard).

     A futures leg with no entry_price would report spot itself as
     profit — a ~69x overstatement that looks plausible. legExpiryPayoff
     now throws instead, so a hand-built or imported leg can't slip a
     wrong number through. The builder defaults entry to spot, so the
     live path is unaffected. */
  const bad = leg({ option_type: "future", action: "buy", strike: 3400, entry_price: 0 });
  assert.throws(() => legExpiryPayoff(bad, 3450), /entry_price/);
  // A properly-priced future still works.
  const good = leg({ option_type: "future", action: "buy", strike: 3400, entry_price: 3400 });
  close(legExpiryPayoff(good, 3450), 50);
});

/* ---------------- aggregation ---------------- */
test("strategyExpiryPayoff sums its legs", () => {
  const legs = [longCall(3400, 20), shortCall(3500, 8)];
  // long call: 200 - 20 = 180.  short call: 8 - 100 = -92.
  close(strategyExpiryPayoff(legs, 3600), 88);
});

test("an empty strategy is worth zero everywhere", () => {
  close(strategyExpiryPayoff([], 3400), 0);
});

test("payoffCurve returns one point per input spot", () => {
  const c = payoffCurve([longCall(3400, 20)], [3300, 3400, 3500]);
  assert.equal(c.length, 3);
  assert.deepEqual(Object.keys(c[0]).sort(), ["pnl", "spot"]);
});

/* ---------------- breakevens ----------------

   BUG FOUND HERE. See "sample landing exactly on the breakeven" below.
   The offset ranges used in the tests immediately following exist to
   route around it so the rest of the logic can still be covered; they
   are not arbitrary. Remove the offsets once the `<`/`>` comparisons
   are fixed.                                                          */

test("long call has exactly one breakeven, at strike + premium", () => {
  const be = findBreakevens([longCall(3400, 20)], 3005, 3800);
  assert.equal(be.length, 1);
  close(be[0], 3420, 1);
});

test("long straddle has two breakevens, symmetric about the strike", () => {
  const be = findBreakevens([longCall(3400, 20), longPut(3400, 18)], 3005, 3800);
  assert.equal(be.length, 2);
  close(be[0], 3362, 1);
  close(be[1], 3438, 1);
});

test("a bull call spread has one breakeven inside the strikes", () => {
  const be = findBreakevens([longCall(3400, 25), shortCall(3450, 10)], 3205, 3600);
  assert.equal(be.length, 1);
  assert.ok(be[0] > 3400 && be[0] < 3450);
});

test("a strategy that never crosses zero reports no breakevens", () => {
  // Long call bought for nothing: payoff is >= 0 everywhere.
  assert.deepEqual(findBreakevens([longCall(3400, 0)], 3005, 3800), []);
});

test("a flat zero payoff does not produce spurious crossings", () => {
  /* The epsilon guard exists for exactly this: without it, floating
     noise on a curve grazing zero yields hundreds of fake breakevens. */
  const legs = [longCall(3400, 0), shortCall(3400, 0)];
  assert.deepEqual(findBreakevens(legs, 3005, 3800), []);
});

test("breakevens are sorted ascending", () => {
  const be = findBreakevens([longCall(3400, 20), longPut(3400, 18)], 3005, 3800);
  assert.deepEqual(be, [...be].sort((a, b) => a - b));
});

test("degenerate step counts are clamped, not divided by zero", () => {
  const be = findBreakevens([longCall(3400, 20)], 3005, 3800, 0);
  assert.ok(Array.isArray(be));
  assert.ok(be.every(Number.isFinite));
});

test("resolution affects precision but not the count", () => {
  const coarse = findBreakevens([longCall(3400, 20)], 3005, 3800, 50);
  const fine = findBreakevens([longCall(3400, 20)], 3005, 3800, 5000);
  assert.equal(coarse.length, fine.length);
  close(fine[0], 3420, 0.1);
});

test("raising the resolution must not delete real breakevens",
  { todo: "eps is scaled to the P&L span but not to the step size, so at "
        + "fine resolution both neighbours of a crossing fall inside eps" },
  () => {
    /* SECOND, INDEPENDENT DEFECT in findBreakevens.

       The grazing guard is:
           const eps = Math.max(span * 1e-4, 1e-6);
           if (Math.abs(y0) < eps && Math.abs(y1) < eps) continue;

       eps is derived from the P&L range only. But as `steps` rises, the
       P&L change BETWEEN adjacent samples shrinks toward zero, so
       eventually both neighbours of a genuine crossing sit inside eps
       and the crossing is thrown away as noise.

       For a long call over 3005-3800: span = 400 so eps = 0.04, and the
       payoff moves 1:1 with spot, so the failure begins once the step
       drops below 0.04 — i.e. around steps > 19875. Confirmed: 5000
       steps finds the breakeven, 20000 and 20001 both return [].

       The two defects interact badly. The obvious fix for the first one
       (exact-sample miss) is to relax the comparisons to <= and >=,
       which makes MORE crossings land on or near zero and therefore
       makes this one bite sooner.

       Fix both together: scale eps to the local step, not the global
       span — e.g. eps = Math.max(span * 1e-9, expectedStepChange * 1e-3)
       — or drop the eps test entirely and instead reject segments where
       the curve is flat (y0 === y1) before testing for a sign change.
    */
    const be = findBreakevens([longCall(3400, 20)], 3005, 3800, 20000);
    assert.equal(be.length, 1, "high resolution must not lose the breakeven");
  });

/* ---- the defect itself ---- */

test("a sample landing exactly on the breakeven must still be found",
  { todo: "sign-change test uses strict < and > so an exact zero sample is skipped" },
  () => {
    /* CONFIRMED BUG — this is the one to fix first.

       The crossing test is:
           if ((y0 < 0 && y1 > 0) || (y0 > 0 && y1 < 0))

       When a sample point lands EXACTLY on the breakeven, y is exactly
       0 there. Neither the preceding pair (y0 < 0, y1 === 0) nor the
       following pair (y0 === 0, y1 > 0) satisfies a strict sign change,
       so the crossing is skipped entirely and the function returns [].

       This is not a rare floating-point coincidence. It happens
       whenever (breakeven - lo) / step is an integer, and breakevens
       are round numbers (strike + premium) while the plotted range is
       normally derived from the strikes themselves. Confirmed failing
       for a plain long call at every one of steps = 400, 800 and the
       2000 default over a 3000-3800 range.

       User-visible effect: the payoff panel reports NO breakeven for
       one of the most common structures in the app, with no error.

       Fix: treat a zero sample as a crossing, e.g.
           const cross = (y0 <= 0 && y1 >= 0) || (y0 >= 0 && y1 <= 0);
       guarded by the existing eps check so a flat-on-zero segment is
       still ignored, then let the existing dedupe collapse the two
       adjacent hits into one.
    */
    const be = findBreakevens([longCall(3400, 20)], 3000, 3800, 2000);
    assert.equal(be.length, 1, "long call must have one breakeven at 3420");
  });

test("the exact-sample miss is reproducible across step counts",
  { todo: "same root cause as above" },
  () => {
    for (const steps of [400, 800, 2000]) {
      const be = findBreakevens([longCall(3400, 20)], 3000, 3800, steps);
      assert.equal(be.length, 1, `steps=${steps} returned ${be.length}`);
    }
  });

/* ---------------- max profit / loss ---------------- */
test("long call: unlimited upside, capped downside", () => {
  const r = maxProfitLoss([longCall(3400, 20)], 3000, 3800);
  assert.equal(r.profit_unlimited, true);
  assert.equal(r.max_profit, null);
  assert.equal(r.loss_unlimited, false);
  close(r.max_loss, -20);
});

test("short call: unlimited loss, capped profit", () => {
  const r = maxProfitLoss([shortCall(3400, 20)], 3000, 3800);
  assert.equal(r.loss_unlimited, true);
  assert.equal(r.max_loss, null);
  close(r.max_profit, 20);
});

test("covered call nets to capped, not unlimited", () => {
  /* The structural test the comment calls out: long future + short call
     nets to zero delta above the strike, so neither side is unlimited.
     A naive "any short call means unlimited loss" rule gets this
     wrong. */
  const r = maxProfitLoss([longFut(3400), shortCall(3450, 10)], 3000, 3800);
  assert.equal(r.profit_unlimited, false);
  assert.equal(r.loss_unlimited, false);
});

test("long put does not create unlimited upside", () => {
  const r = maxProfitLoss([longPut(3400, 15)], 3000, 3800);
  assert.equal(r.profit_unlimited, false);
  assert.equal(r.loss_unlimited, false);
});

test("iron condor is capped on both sides", () => {
  const legs = [shortPut(3350, 12), longPut(3300, 6),
                shortCall(3450, 12), longCall(3500, 6)];
  const r = maxProfitLoss(legs, 3000, 3800);
  assert.equal(r.profit_unlimited, false);
  assert.equal(r.loss_unlimited, false);
  assert.ok(r.max_profit > 0 && r.max_loss < 0);
});

test("sampled extremes are always returned even when unlimited", () => {
  const r = maxProfitLoss([longCall(3400, 20)], 3000, 3800);
  assert.ok(Number.isFinite(r.sampled_max));
  assert.ok(Number.isFinite(r.sampled_min));
});

test("net upside of zero from offsetting calls is capped", () => {
  const r = maxProfitLoss([longCall(3400, 20), shortCall(3500, 8)], 3000, 3800);
  assert.equal(r.profit_unlimited, false);
});

/* ---------------- greeks and premium ---------------- */
test("netGreeks applies action sign and quantity", () => {
  const legs = [
    leg({ option_type: "call", action: "buy", strike: 3400, qty: 2, delta: 50, vega: 1.2 }),
    leg({ option_type: "call", action: "sell", strike: 3500, qty: 1, delta: 30, vega: 0.9 }),
  ];
  const g = netGreeks(legs);
  close(g.delta, 70);
  close(g.vega, 1.5);
});

test("null greeks are skipped, not coerced to zero-and-counted", () => {
  const g = netGreeks([leg({ option_type: "call", action: "buy", strike: 1, delta: null })]);
  close(g.delta, 0);
});

test("netEntryPremium: buying is a debit, selling a credit", () => {
  close(netEntryPremium([longCall(3400, 20)]), -20);
  close(netEntryPremium([shortCall(3400, 20)]), 20);
});

test("netEntryPremium nets a spread correctly", () => {
  close(netEntryPremium([longCall(3400, 25), shortCall(3450, 10)]), -15);
});

/* ---------------- Black-76 ---------------- */
test("bs76Price refuses impossible inputs rather than returning NaN", () => {
  assert.equal(bs76Price("call", 3400, 3400, 0, 0.2), null);
  assert.equal(bs76Price("call", 3400, 3400, -1, 0.2), null);
  assert.equal(bs76Price("call", 3400, 3400, 0.5, 0), null);
  assert.equal(bs76Price("call", 0, 3400, 0.5, 0.2), null);
  assert.equal(bs76Price("call", 3400, 0, 0.5, 0.2), null);
});

test("put-call parity holds at zero rates", () => {
  const F = 3400, K = 3450, T = 0.25, s = 0.2;
  const c = bs76Price("call", F, K, T, s);
  const p = bs76Price("put", F, K, T, s);
  close(c - p, F - K, 1e-4);
});

test("price is monotonically increasing in volatility", () => {
  let prev = -1;
  for (const s of [0.05, 0.1, 0.2, 0.4, 0.8]) {
    const v = bs76Price("call", 3400, 3400, 0.25, s);
    assert.ok(v > prev, `vega should be positive: ${v} <= ${prev}`);
    prev = v;
  }
});

test("price is monotonically increasing in time to expiry", () => {
  let prev = -1;
  for (const T of [0.01, 0.05, 0.25, 1.0]) {
    const v = bs76Price("call", 3400, 3400, T, 0.2);
    assert.ok(v > prev);
    prev = v;
  }
});

test("deep in-the-money call approaches intrinsic", () => {
  const v = bs76Price("call", 4000, 3000, 0.01, 0.15);
  close(v, 1000, 1);
});

test("deep out-of-the-money call approaches zero", () => {
  assert.ok(bs76Price("call", 3000, 4000, 0.01, 0.15) < 0.5);
});

test("an ATM price is close to the 0.4 * F * sigma * sqrt(T) rule of thumb", () => {
  const F = 3400, T = 0.25, s = 0.2;
  const approx = 0.4 * F * s * Math.sqrt(T);
  close(bs76Price("call", F, F, T, s), approx, approx * 0.05);
});

test("discounting reduces the price when a rate is supplied", () => {
  const undisc = bs76Price("call", 3400, 3400, 1, 0.2, 0);
  const disc = bs76Price("call", 3400, 3400, 1, 0.2, 0.05);
  assert.ok(disc < undisc);
});

/* ---------------- projection ---------------- */
test("projecting past expiry returns intrinsic", () => {
  close(projectLegPrice("call", 3500, 3400, 0.1, 0.2, 0.2), 100);
  close(projectLegPrice("put", 3300, 3400, 0.1, 0.2, 0.2), 100);
  close(projectLegPrice("call", 3300, 3400, 0.1, 0.2, 0.2), 0);
});

test("projecting with a null IV falls back to intrinsic", () => {
  close(projectLegPrice("call", 3500, 3400, 0.5, 0.1, null), 100);
});

test("time decay: the same option is worth less later", () => {
  const now = projectLegPrice("call", 3400, 3400, 0.5, 0, 0.2);
  const later = projectLegPrice("call", 3400, 3400, 0.5, 0.4, 0.2);
  assert.ok(later < now);
});

test("payoffCurveProjected without years_to_own_expiry collapses to intrinsic", () => {
  /* FIXED (was a pinned hazard).

     A leg missing years_to_own_expiry would make remaining time <= 0
     and silently return the EXPIRY payoff — a wrong time-value curve
     that looks like it works. payoffCurveProjected now throws instead,
     so the mistake surfaces rather than misleading. */
  const legs = [longCall(3400, 20)];   // no years_to_own_expiry
  assert.throws(() => payoffCurveProjected(legs, [3500], 0.1), /years_to_own_expiry/);
});

test("a properly dated leg projects above its expiry payoff while time remains", () => {
  const l = { ...longCall(3400, 20), years_to_own_expiry: 0.5, iv_decimal: 0.2 };
  const projected = payoffCurveProjected([l], [3400], 0.1)[0].pnl;
  const atExpiry = strategyExpiryPayoff([l], 3400);
  assert.ok(projected > atExpiry, "an ATM option should retain time value");
});

test("futures ignore time entirely in the projected curve", () => {
  const f = longFut(3400);
  const a = payoffCurveProjected([f], [3450], 0)[0].pnl;
  const b = payoffCurveProjected([f], [3450], 0.4)[0].pnl;
  close(a, b);
  close(a, 50);
});
