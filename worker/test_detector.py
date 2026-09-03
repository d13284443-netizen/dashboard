"""
test_detector.py — Offline tests for the pure math. No database.

The important one is test_smile_roll_is_rejected: it builds the exact
scenario described — spot rallies, the volatility surface does not move
at all, and a fixed strike's IV rises purely because it slid along an
unchanged smile. The raw rule must fire (that is the false positive we
have today) and the adjusted rule must not.
"""
import datetime
import math

from detector import time_weighted_ema
import smile

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 8, 19, 6, 0, tzinfo=UTC)


def ts(minutes):
    return T0 + datetime.timedelta(minutes=minutes)


def make_smile(spot, atm_vol=0.18, skew=1.5, curvature=120.0, lo=-0.10, hi=0.10, step=25):
    """A realistic smile: IV as a function of log-moneyness, with a
    downward skew (puts bid) and convexity in the wings. Returns
    [(strike, iv)] on a fixed strike grid, as a real chain would be.
    """
    strikes, k = [], round(spot * math.exp(lo) / step) * step
    while k <= spot * math.exp(hi):
        m = math.log(k / spot)
        iv = atm_vol * (1 - skew * m + curvature * m * m)
        strikes.append((float(k), iv * 100))  # CQG-style percentage points
        k += step
    return strikes


def test_time_weighted_ema_is_cadence_independent():
    """Same real-time trajectory sampled at 15 vs 20 minutes must give
    near-identical baselines. The old sample-count EMA would not."""
    def sampled(step):
        pts = []
        for i in range(0, 361, step):
            pts.append((ts(i), 18.0 + 0.5 * math.sin(i / 120)))
        return pts

    e15 = time_weighted_ema(sampled(15), 90)
    e20 = time_weighted_ema(sampled(20), 90)
    assert abs(e15 - e20) < 0.05, (e15, e20)
    print(f"  cadence independence: 15min EMA {e15:.4f} vs 20min EMA {e20:.4f}  OK")


def test_ema_excludes_latest_point():
    """A spike must not be allowed to pull the baseline toward itself."""
    flat = [(ts(i), 18.0) for i in range(0, 300, 15)]
    spiked = flat[:-1] + [(flat[-1][0], 25.0)]
    assert time_weighted_ema(spiked, 90) == 18.0
    print("  latest point excluded from baseline  OK")


def test_sharp_jump_detected():
    pts = [(ts(i), 18.0) for i in range(0, 300, 15)] 
    pts[-1] = (pts[-1][0], 21.5)
    ema = time_weighted_ema(pts, 90)
    change = (pts[-1][1] - ema) / ema
    assert change > 0.10, change
    print(f"  sharp jump: +{change*100:.1f}% detected  OK")


def test_smile_roll_is_rejected():
    """THE CASE THAT MATTERS.

    Spot rallies 1.5%. The vol surface is completely unchanged — the
    same function of moneyness, re-anchored to the new spot. The 3300
    put, already below the market, is pushed FURTHER out of the money,
    so it slides up the convex put wing and its IV rises sharply. There
    is no vol information in that move at all; it is pure geometry.

    This is the shape of the false positive: any strike moving AWAY from
    the money gains IV mechanically through smile convexity, and the
    raw strike-to-strike rule reads every one of them as a spike.
    """
    spot_before, spot_after = 3400.0, 3451.0
    before = make_smile(spot_before)
    after = make_smile(spot_after)

    strike = 3300.0
    iv_before = smile.interp(before, strike)
    iv_after = smile.interp(after, strike)

    raw = (iv_after - iv_before) / iv_before
    adj, expected = smile.adjusted_change(iv_after, before, spot_before, strike, spot_after)

    assert raw > 0.10, f"raw change {raw:.3%} — test scenario is too weak"
    assert adj is not None and abs(adj) < 0.02, f"adjusted change {adj}"
    print(f"  smile roll: raw +{raw*100:.1f}% (WOULD ALERT), "
          f"adjusted {adj*100:+.2f}% (correctly ignored)  OK")


def test_real_vol_event_survives_adjustment():
    """Control case: the whole surface lifts 15%. The adjustment must
    NOT explain this away, or the filter would be deleting real signal.
    """
    spot_before, spot_after = 3400.0, 3451.0
    before = make_smile(spot_before, atm_vol=0.18)
    after = make_smile(spot_after, atm_vol=0.18 * 1.15)

    strike = 3300.0
    iv_after = smile.interp(after, strike)
    adj, _ = smile.adjusted_change(iv_after, before, spot_before, strike, spot_after)
    assert adj is not None and adj > 0.10, f"adjusted change {adj}"
    print(f"  genuine vol lift: adjusted {adj*100:+.1f}% (still alerts)  OK")


def test_adjustment_declines_outside_baseline_range():
    """Off the edge of the baseline chain must return None, not a
    confident extrapolated number."""
    before = make_smile(3400.0)
    adj, _ = smile.adjusted_change(20.0, before, 3400.0, 9999.0, 3451.0)
    assert adj is None
    print("  out-of-range baseline declines to judge  OK")


def test_spot_interpolation():
    recs = [{"strike": 3400.0, "call_delta": 58.0},
            {"strike": 3425.0, "call_delta": 46.0},
            {"strike": 3450.0, "call_delta": 34.0}]
    spot = smile.detect_spot(recs)
    assert 3400 < spot < 3425, spot
    print(f"  spot interpolated to {spot:.1f} (between grid strikes)  OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            print(f"{name}:")
            fn()
    print("\nAll tests passed.")
