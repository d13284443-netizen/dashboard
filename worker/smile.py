"""
smile.py — Volatility-smile geometry. This is what separates "the vol
surface actually moved" from "spot moved and this strike slid along an
unchanged smile".

THE PROBLEM
-----------
The original detector compared strike 3450's IV now against strike
3450's IV N hours ago. But a fixed strike does not stay in the same
place on the volatility curve. When gold rallies, a 3450 call that was
2% out-of-the-money becomes at-the-money, and it picks up whatever IV
the existing smile already assigned to that position. Nothing about the
vol surface changed — you slid along it.

That single effect explains the "calls spike whenever the market rises"
pattern: it is not a vol event being detected, it is a spot move being
misread as one. And because the smile is usually steeper on the wings,
the misreading is worst exactly where thin quotes already make IV noisy.

THE CORRECTION
--------------
Compare like for like, at constant MONEYNESS rather than constant
strike:

    m       = ln(K / S_now)              current position on the curve
    K_base  = S_base * exp(m)            the strike that occupied that
                                         same position at baseline time
    iv_exp  = interp(baseline_smile, K_base)
    adjusted_change = (iv_now - iv_exp) / iv_exp

If the smile simply rolled with spot, iv_exp lands right on iv_now and
the adjusted change is ~0, even when the raw strike-to-strike change is
large. If the surface genuinely lifted or twisted, the adjusted change
survives.

Two supporting signals fall out of the same data, and are recorded on
every event so you can see which is carrying the information:

  ATM IV change — did the whole surface lift, or only this strike?
  Skew change   — (strike IV - ATM IV) then vs now. A real vol event
                  usually twists the skew; a pure spot move mostly
                  leaves its shape intact.

NOTE ON CONSERVATISM: every function here returns None rather than
extrapolating past the edge of the observed strike range. A guessed
baseline is worse than no baseline — it would produce confident
adjusted numbers with nothing behind them.
"""
import math


def interp(points, x):
    """Linear interpolation over (x, y) pairs, sorted by x. Returns None
    outside the observed range instead of extrapolating."""
    pts = sorted((p for p in points if p[0] is not None and p[1] is not None),
                 key=lambda p: p[0])
    if len(pts) < 2:
        return None
    if x < pts[0][0] or x > pts[-1][0]:
        return None
    for i in range(len(pts) - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        if x0 <= x <= x1:
            if x1 == x0:
                return y0
            t = (x - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return None


def detect_spot(records):
    """Spot proxy: the strike whose call delta is closest to 50. Same
    convention the original desktop app used, kept deliberately so
    historical comparisons stay consistent.

    Refined by interpolation where possible — with strikes 25 apart, the
    nearest-delta-50 strike can be up to ~12 points off, which at these
    price levels is a meaningful moneyness error when we're trying to
    measure sub-1% smile roll.
    """
    pts = [(r["call_delta"], r["strike"]) for r in records
           if r.get("call_delta") is not None]
    if not pts:
        return None
    exact = interp(pts, 50.0)
    if exact is not None:
        return exact
    return min(pts, key=lambda p: abs(p[0] - 50.0))[1]


def atm_iv(records, spot):
    """IV interpolated at spot. Averages call and put where both exist —
    put-call parity says they should agree, so averaging halves the
    quote noise in the number every skew calculation is measured against.
    """
    if spot is None:
        return None

    # Filter dead strikes (iv <= 0 or missing) BEFORE interpolating, the
    # same way build_smile does. Feeding a 0.0 from an illiquid strike
    # into interp drags the interpolated ATM value down, and that value
    # is the denominator of atm_iv_change_pct and the baseline of every
    # skew calculation — so the error propagates into stored evidence.
    def side_iv(field):
        pts = [(r["strike"], r[field]) for r in records if (r.get(field) or 0) > 0]
        if not pts:
            return None
        v = interp(pts, spot)
        if v is not None:
            return v
        # interp needs two points and won't extrapolate. With exactly one
        # clean quote, use the nearest (only) one rather than discarding
        # the whole side — a single good strike beats no ATM estimate.
        if len(pts) == 1:
            return pts[0][1]
        # Multiple points but spot is outside their range: take the
        # closest strike's IV rather than nothing.
        return min(pts, key=lambda p: abs(p[0] - spot))[1]

    vals = [v for v in (side_iv("call_impvlt"), side_iv("put_impvlt")) if v is not None and v > 0]
    if not vals:
        return None
    return sum(vals) / len(vals)


def moneyness(strike, spot):
    if not strike or not spot or spot <= 0:
        return None
    return math.log(strike / spot)


def expected_iv_at_constant_moneyness(baseline_smile, baseline_spot, strike, current_spot):
    """The core correction.

    baseline_smile: [(strike, iv)] from the baseline snapshot, one side.
    Returns the IV the baseline smile assigned to this strike's CURRENT
    position on the curve, or None if that position falls outside the
    strikes actually quoted at baseline.
    """
    if not baseline_spot or not current_spot or baseline_spot <= 0 or current_spot <= 0:
        return None
    m = moneyness(strike, current_spot)
    if m is None:
        return None
    equivalent_strike = baseline_spot * math.exp(m)
    return interp(baseline_smile, equivalent_strike)


def adjusted_change(iv_now, baseline_smile, baseline_spot, strike, current_spot):
    """Returns (adjusted_pct_change, expected_iv) or (None, None).

    None means "cannot be judged" — the equivalent strike fell off the
    edge of the baseline chain. Callers must not treat that as zero;
    an unjudgeable event should stay visible, not be silently dropped.
    """
    iv_exp = expected_iv_at_constant_moneyness(
        baseline_smile, baseline_spot, strike, current_spot)
    if iv_exp is None or iv_exp <= 0 or iv_now is None:
        return None, None
    return (iv_now - iv_exp) / iv_exp, iv_exp


def build_smile(ticks, side):
    """Extracts [(strike, iv)] for one side from a snapshot's ticks,
    keeping only liquid strikes — an illiquid quote in the baseline
    corrupts the interpolation just as badly as one in the current
    reading."""
    return [(t["strike"], t["iv"]) for t in ticks
            if t["side"] == side and t.get("iv") and t["iv"] > 0 and t.get("liquid")]
