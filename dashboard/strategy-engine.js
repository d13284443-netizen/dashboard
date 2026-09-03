/* ============================================================
   strategy-engine.js — Option strategy payoff + pricing math.

   Ported faithfully from the reference project's payoff_math.py and
   black_scholes.py, which were themselves ports of the original,
   tested desktop CQG analyser. Kept in the browser (rather than as
   Vercel Python functions like the reference) so the dashboard stays
   a pure static site with no serverless backend to configure — the
   math is light enough that JS handles it instantly, and it avoids
   the exact deploy fragility that broke the earlier project.

   A "leg" is:
     { option_type: "call"|"put"|"future",
       action: "buy"|"sell",
       strike: number (0 for a future),
       qty: integer,
       entry_price: number,          // premium per unit at entry
       iv_decimal: number|null,      // for time projection
       delta/gamma/vega/theta: number|null }  // per-unit, from the chain

   All payoffs are PER UNIT; the caller multiplies by the instrument
   point multiplier and qty for dollar P&L. That scaling is left to
   the caller so this stays instrument-agnostic — same contract as
   the Python original.
   ============================================================ */

export const MULTIPLIERS = { GOLD: 100, SILVER: 5000, NASDAQ: 20, SP500: 50 };

/* ---------- expiry payoff (exact — intrinsic only) ---------- */

export function legExpiryPayoff(leg, spotAtExpiry) {
  const sign = leg.action === "buy" ? 1 : -1;
  const qty = leg.qty ?? 1;
  const entry = leg.entry_price || 0;

  if (leg.option_type === "future") {
    // strike == entry by convention for futures. A leg that set strike
    // but left entry_price at 0 would report spot itself as profit — a
    // ~69x overstatement that looks plausible. Guard it: a real futures
    // fill is never 0. The builder defaults this to spot, so only a
    // hand-built or imported leg can trip this.
    if (!leg.entry_price) {
      throw new Error("future leg needs entry_price (its fill price); got " + leg.entry_price);
    }
    const diff = spotAtExpiry - entry;
    return (sign === 1 ? diff : -diff) * qty;
  }

  let intrinsic;
  if (leg.option_type === "call") intrinsic = Math.max(0, spotAtExpiry - leg.strike);
  else if (leg.option_type === "put") intrinsic = Math.max(0, leg.strike - spotAtExpiry);
  else throw new Error(`Unknown option_type: ${leg.option_type}`);

  const perUnit = sign === 1 ? intrinsic - entry : entry - intrinsic;
  return perUnit * qty;
}

export function strategyExpiryPayoff(legs, spot) {
  return legs.reduce((s, leg) => s + legExpiryPayoff(leg, spot), 0);
}

export function payoffCurve(legs, spotRange) {
  return spotRange.map((s) => ({ spot: s, pnl: strategyExpiryPayoff(legs, s) }));
}

/* ---------- breakevens + max P/L ---------- */

export function findBreakevens(legs, lo, hi, steps = 2000) {
  if (steps < 2) steps = 2;
  const step = (hi - lo) / steps;
  const xs = [], ys = [];
  for (let i = 0; i <= steps; i++) { const x = lo + i * step; xs.push(x); ys.push(strategyExpiryPayoff(legs, x)); }

  // A breakeven is where the payoff genuinely passes THROUGH zero —
  // strictly negative on one side, strictly positive on the other. Two
  // failure modes the test suite pinned, fixed together:
  //
  //   1. Exact-sample miss: when a sample lands exactly on the
  //      breakeven the strict < / > test skipped it. Handled by finding
  //      the nearest strictly-signed samples on each side of any run of
  //      zeros, then checking those for opposite signs.
  //
  //   2. Over-eager grazing guard: the old eps was scaled to the P&L
  //      span, so at fine resolution it ate real crossings. Removed
  //      entirely; "through zero" is decided by strict sign on each
  //      side, which is resolution-independent.
  //
  // This also correctly rejects a curve that only TOUCHES zero and
  // returns (grazes), or LIFTS OFF zero (e.g. a long call bought for 0,
  // flat-zero below the strike then rising) — in both cases the two
  // sides are not strictly opposite in sign, so there is no breakeven.
  const raw = [];
  for (let i = 0; i < xs.length - 1; i++) {
    const y0 = ys[i], y1 = ys[i + 1];
    if (y0 < 0 && y1 > 0 || y0 > 0 && y1 < 0) {
      // Clean crossing between two strictly-signed samples.
      const frac = -y0 / (y1 - y0);
      raw.push(xs[i] + frac * (xs[i + 1] - xs[i]));
    } else if (y0 !== 0 && y1 === 0) {
      // Entering a zero (or run of zeros) from a strict sign. Look ahead
      // to the next strictly-signed sample; if it's on the opposite
      // side, the curve passed through zero → breakeven at this point.
      let j = i + 1;
      while (j < ys.length && ys[j] === 0) j++;
      if (j < ys.length && Math.sign(ys[j]) === -Math.sign(y0)) {
        raw.push(xs[i + 1]);
      }
    }
  }
  // Collapse crossings closer than ~1.5 steps into one (a through-zero
  // run can register more than once).
  const merged = [];
  for (const b of raw.sort((a, z) => a - z)) {
    if (!merged.length || b - merged[merged.length - 1] > step * 1.5) merged.push(b);
  }
  return merged;
}

export function maxProfitLoss(legs, lo, hi, steps = 2000) {
  const step = (hi - lo) / steps;
  let sMax = -Infinity, sMin = Infinity;
  for (let i = 0; i <= steps; i++) {
    const y = strategyExpiryPayoff(legs, lo + i * step);
    if (y > sMax) sMax = y;
    if (y < sMin) sMin = y;
  }
  // Unlimited detected structurally: on the upside both calls AND
  // futures move 1:1 with spot, so they net TOGETHER. A covered call
  // (long future + short call) nets to 0 above the strike — capped,
  // not unlimited. Puts don't create upside-unlimited risk.
  const netUpside = legs
    .filter((l) => l.option_type === "call" || l.option_type === "future")
    .reduce((s, l) => s + (l.action === "buy" ? 1 : -1) * (l.qty ?? 1), 0);

  const profitUnlimited = netUpside > 0;
  const lossUnlimited = netUpside < 0;
  return {
    max_profit: profitUnlimited ? null : sMax,
    max_loss: lossUnlimited ? null : sMin,
    profit_unlimited: profitUnlimited,
    loss_unlimited: lossUnlimited,
    sampled_max: sMax,
    sampled_min: sMin,
  };
}

export function netGreeks(legs) {
  const out = { delta: 0, gamma: 0, vega: 0, theta: 0 };
  for (const leg of legs) {
    const sign = leg.action === "buy" ? 1 : -1;
    const qty = leg.qty ?? 1;
    for (const k of Object.keys(out)) {
      const v = leg[k];
      if (v != null) out[k] += sign * qty * v;
    }
  }
  return out;
}

export function netEntryPremium(legs) {
  let total = 0;
  for (const leg of legs) {
    const sign = leg.action === "buy" ? 1 : -1;
    const qty = leg.qty ?? 1;
    const entry = leg.entry_price || 0;
    total += -sign * qty * entry; // buy = debit (−), sell = credit (+)
  }
  return total;
}

/* ---------- Black-76 pricing (for time projection) ---------- */

function normCdf(x) { return 0.5 * (1 + erf(x / Math.sqrt(2))); }
function normPdf(x) { return Math.exp(-0.5 * x * x) / Math.sqrt(2 * Math.PI); }
// Abramowitz & Stegun 7.1.26 — max error ~1.5e-7, ample for pricing.
function erf(x) {
  const s = x < 0 ? -1 : 1; x = Math.abs(x);
  const t = 1 / (1 + 0.3275911 * x);
  const y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-x * x);
  return s * y;
}

export function bs76Price(type, F, K, T, sigma, r = 0) {
  if (!(T > 0) || !(sigma > 0) || !(F > 0) || !(K > 0)) return null;
  const d1 = (Math.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * Math.sqrt(T));
  const d2 = d1 - sigma * Math.sqrt(T);
  const disc = Math.exp(-r * T);
  if (type === "call") return disc * (F * normCdf(d1) - K * normCdf(d2));
  return disc * (K * normCdf(-d2) - F * normCdf(-d1));
}

export function projectLegPrice(type, spotTarget, strike, yearsToExpiryNow, yearsElapsed, ivDecimal, r = 0) {
  const remaining = yearsToExpiryNow - yearsElapsed;
  if (remaining <= 0) {
    return type === "call" ? Math.max(0, spotTarget - strike) : Math.max(0, strike - spotTarget);
  }
  const price = bs76Price(type, spotTarget, strike, remaining, ivDecimal, r);
  if (price == null) {
    return type === "call" ? Math.max(0, spotTarget - strike) : Math.max(0, strike - spotTarget);
  }
  return price;
}

/* Projected P&L curve at a point in TIME before expiry (for the
   "what if it's <date>" slider and calendar spreads). */
export function payoffCurveProjected(legs, spotRange, yearsElapsed) {
  // A leg missing years_to_own_expiry would make remaining time <= 0
  // and silently return the EXPIRY payoff — a wrong "what if it's <date>"
  // curve that looks like it works. Require the field on every option
  // leg so the mistake surfaces instead of misleading.
  for (const leg of legs) {
    if (leg.option_type !== "future"
        && (leg.years_to_own_expiry == null || leg.years_to_own_expiry <= 0)) {
      throw new Error("payoffCurveProjected needs years_to_own_expiry on option leg "
        + `${leg.action} ${leg.option_type} ${leg.strike}`);
    }
  }
  return spotRange.map((s) => {
    let pnl = 0;
    for (const leg of legs) {
      const sign = leg.action === "buy" ? 1 : -1;
      const qty = leg.qty ?? 1;
      const entry = leg.entry_price || 0;
      if (leg.option_type === "future") {
        const diff = s - entry;
        pnl += (sign === 1 ? diff : -diff) * qty;
      } else {
        const val = projectLegPrice(leg.option_type, s, leg.strike,
          leg.years_to_own_expiry, yearsElapsed, leg.iv_decimal);
        pnl += (sign === 1 ? val - entry : entry - val) * qty;
      }
    }
    return { spot: s, pnl };
  });
}
