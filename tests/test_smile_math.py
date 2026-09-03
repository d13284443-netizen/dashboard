"""
test_smile_math.py — smile.py, the geometry that decides whether an IV
move was information or just spot sliding along an unchanged curve.

These are the highest-value automated tests in the project: every
downstream number (adj_pct_change, would_suppress, severity) is derived
from these five functions, and all of them are pure, so they can be
tested exhaustively at near-zero cost.
"""
import math

import pytest

import smile
from conftest import smile_curve


# --------------------------------------------------------------------
# interp
# --------------------------------------------------------------------
class TestInterp:
    def test_midpoint(self):
        assert smile.interp([(0, 0), (10, 100)], 5) == pytest.approx(50)

    def test_at_endpoints_inclusive(self):
        pts = [(0, 1), (10, 2)]
        assert smile.interp(pts, 0) == pytest.approx(1)
        assert smile.interp(pts, 10) == pytest.approx(2)

    @pytest.mark.parametrize("x", [-0.0001, 10.0001, -1e6, 1e6])
    def test_refuses_to_extrapolate(self, x):
        """The documented contract: None outside the observed range. A
        guessed baseline produces confident numbers with nothing behind
        them."""
        assert smile.interp([(0, 1), (10, 2)], x) is None

    def test_unsorted_input_is_sorted(self):
        assert smile.interp([(10, 2), (0, 1)], 5) == pytest.approx(1.5)

    def test_drops_none_pairs(self):
        pts = [(0, 1), (5, None), (None, 9), (10, 2)]
        assert smile.interp(pts, 5) == pytest.approx(1.5)

    def test_too_few_points(self):
        assert smile.interp([(0, 1)], 0) is None
        assert smile.interp([], 0) is None

    def test_duplicate_x_does_not_divide_by_zero(self):
        assert smile.interp([(5, 1), (5, 3), (10, 4)], 5) is not None

    def test_non_monotonic_y_is_still_piecewise(self):
        """Deltas from a real chain are noisy and can be non-monotonic.
        Interp must not silently pick a far-away segment."""
        pts = [(0, 0), (5, 10), (10, 0)]
        assert smile.interp(pts, 2.5) == pytest.approx(5)


# --------------------------------------------------------------------
# moneyness
# --------------------------------------------------------------------
class TestMoneyness:
    def test_at_the_money_is_zero(self):
        assert smile.moneyness(3400, 3400) == pytest.approx(0)

    def test_sign_convention(self):
        assert smile.moneyness(3500, 3400) > 0
        assert smile.moneyness(3300, 3400) < 0

    @pytest.mark.parametrize("k,s", [(None, 3400), (3400, None), (3400, 0),
                                     (3400, -1), (0, 3400)])
    def test_guards(self, k, s):
        assert smile.moneyness(k, s) is None


# --------------------------------------------------------------------
# detect_spot
# --------------------------------------------------------------------
class TestDetectSpot:
    def test_interpolates_between_grid_strikes(self):
        recs = [{"strike": 3400.0, "call_delta": 58.0},
                {"strike": 3425.0, "call_delta": 46.0},
                {"strike": 3450.0, "call_delta": 34.0}]
        spot = smile.detect_spot(recs)
        assert 3400 < spot < 3425

    def test_falls_back_to_nearest_when_50_is_outside_range(self):
        """Every delta above 50 (deep ITM chain) — interp returns None,
        so the nearest-delta strike is used."""
        recs = [{"strike": 3400.0, "call_delta": 80.0},
                {"strike": 3425.0, "call_delta": 70.0}]
        assert smile.detect_spot(recs) == 3425.0

    def test_no_deltas_returns_none(self):
        assert smile.detect_spot([{"strike": 3400.0}]) is None

    def test_empty(self):
        assert smile.detect_spot([]) is None

    def test_diverges_from_chain_loader_implementation(self):
        """chain_loader.detect_spot and smile.detect_spot are two
        different functions with the same name and different answers.
        ingest uses the smile one. This test pins the divergence so it
        is a known, asserted fact rather than a latent surprise if
        someone swaps the import."""
        import chain_loader
        recs = [{"strike": 3400.0, "call_delta": 58.0},
                {"strike": 3425.0, "call_delta": 46.0}]
        assert chain_loader.detect_spot(recs) == 3425.0        # nearest
        assert smile.detect_spot(recs) != 3425.0               # interpolated


# --------------------------------------------------------------------
# atm_iv
# --------------------------------------------------------------------
class TestAtmIv:
    def test_averages_both_sides(self):
        recs = [{"strike": 3400, "call_impvlt": 18.0, "put_impvlt": 20.0},
                {"strike": 3500, "call_impvlt": 18.0, "put_impvlt": 20.0}]
        assert smile.atm_iv(recs, 3450) == pytest.approx(19.0)

    def test_single_side_when_other_missing(self):
        recs = [{"strike": 3400, "call_impvlt": 18.0, "put_impvlt": None},
                {"strike": 3500, "call_impvlt": 18.0, "put_impvlt": None}]
        assert smile.atm_iv(recs, 3450) == pytest.approx(18.0)

    def test_none_spot(self):
        assert smile.atm_iv([], None) is None

    def test_zero_iv_in_the_chain_corrupts_the_interpolation(self):
        """FIXED (was a known defect).

        build_smile filters `iv > 0`; atm_iv now does too, filtering its
        inputs BEFORE interpolating rather than only dropping zeros from
        the result. A single strike quoting 0.0 IV (common on a dead far
        strike) no longer drags the ATM number toward zero.

        atm_iv is the denominator of atm_iv_change_pct and the
        subtrahend in every skew calculation, so this keeps a corrupted
        value out of stored event evidence.
        """
        clean = [{"strike": 3400, "call_impvlt": 18.0, "put_impvlt": 18.0},
                 {"strike": 3500, "call_impvlt": 18.0, "put_impvlt": 18.0}]
        dirty = [{"strike": 3400, "call_impvlt": 0.0, "put_impvlt": 0.0},
                 {"strike": 3500, "call_impvlt": 18.0, "put_impvlt": 18.0}]
        assert smile.atm_iv(clean, 3450) == pytest.approx(18.0)
        # The dead 3400 strike is filtered out; the one valid 18.0 quote
        # at 3500 carries the estimate instead of being dragged to ~9.
        assert smile.atm_iv(dirty, 3450) == pytest.approx(18.0)


# --------------------------------------------------------------------
# The core correction
# --------------------------------------------------------------------
class TestAdjustedChange:
    def test_pure_smile_roll_is_neutralised(self):
        """Spot rallies 1.5%, the surface does not move at all. The raw
        strike-to-strike rule must fire and the adjusted rule must not.
        This is the false positive the whole module exists to kill."""
        before, after = smile_curve(3400.0), smile_curve(3451.0)
        strike = 3300.0
        iv_before = smile.interp(before, strike)
        iv_after = smile.interp(after, strike)

        raw = (iv_after - iv_before) / iv_before
        adj, expected = smile.adjusted_change(iv_after, before, 3400.0,
                                              strike, 3451.0)
        assert raw > 0.10, "scenario too weak to be a false positive"
        assert adj is not None
        assert abs(adj) < 0.02
        assert expected == pytest.approx(iv_after, rel=0.02)

    @pytest.mark.parametrize("lift", [1.10, 1.15, 1.30])
    def test_genuine_surface_lift_survives(self, lift):
        before = smile_curve(3400.0, atm_vol=0.18)
        after = smile_curve(3451.0, atm_vol=0.18 * lift)
        iv_after = smile.interp(after, 3300.0)
        adj, _ = smile.adjusted_change(iv_after, before, 3400.0, 3300.0, 3451.0)
        assert adj == pytest.approx(lift - 1, abs=0.01)

    def test_surface_drop_is_reported_negative(self):
        before = smile_curve(3400.0, atm_vol=0.18)
        after = smile_curve(3400.0, atm_vol=0.18 * 0.8)
        iv_after = smile.interp(after, 3300.0)
        adj, _ = smile.adjusted_change(iv_after, before, 3400.0, 3300.0, 3400.0)
        assert adj == pytest.approx(-0.2, abs=0.01)

    def test_declines_outside_baseline_strike_range(self):
        adj, exp = smile.adjusted_change(20.0, smile_curve(3400.0), 3400.0,
                                         9999.0, 3451.0)
        assert adj is None and exp is None

    @pytest.mark.parametrize("baseline_spot,current_spot",
                             [(0, 3400), (3400, 0), (-1, 3400), (None, 3400),
                              (3400, None)])
    def test_bad_spots_decline(self, baseline_spot, current_spot):
        adj, _ = smile.adjusted_change(20.0, smile_curve(3400.0), baseline_spot,
                                       3400.0, current_spot)
        assert adj is None

    def test_none_iv_now_declines(self):
        adj, _ = smile.adjusted_change(None, smile_curve(3400.0), 3400.0,
                                       3400.0, 3400.0)
        assert adj is None

    def test_large_spot_move_pushes_equivalent_strike_off_the_grid(self):
        """A 10% gap plus an already-OTM strike puts the equivalent
        strike outside the baseline chain (which spans +/-10% of the
        baseline spot). The correct answer is None ("cannot judge"),
        NOT zero — callers must keep the event visible.

        Note the near miss this pins down: a 10% spot move alone is NOT
        enough to fall off the grid for an ATM strike, because the
        equivalent strike moves in the same direction as the range. It
        takes moneyness plus the gap.
        """
        before = smile_curve(3400.0)          # covers ~3075 to ~3750
        adj, _ = smile.adjusted_change(25.0, before, 3400.0, 3100.0, 3740.0)
        assert adj is None

    def test_moderate_gap_on_an_atm_strike_is_still_judgeable(self):
        """Companion to the case above — guards against 'fix' the
        out-of-range check by widening it until real events stop being
        judged."""
        before = smile_curve(3400.0)
        adj, _ = smile.adjusted_change(25.0, before, 3400.0, 3400.0, 3740.0)
        assert adj is not None

    def test_symmetry_no_spot_move_equals_raw(self):
        """With spot unchanged the adjustment must be a no-op: the
        equivalent strike is the strike itself."""
        before = smile_curve(3400.0)
        iv_before = smile.interp(before, 3375.0)
        adj, _ = smile.adjusted_change(iv_before * 1.2, before, 3400.0,
                                       3375.0, 3400.0)
        assert adj == pytest.approx(0.2, abs=1e-9)


# --------------------------------------------------------------------
# build_smile
# --------------------------------------------------------------------
class TestBuildSmile:
    def _ticks(self, **over):
        base = {"strike": 3400.0, "side": "c", "iv": 18.0, "liquid": True}
        base.update(over)
        return base

    def test_keeps_only_requested_side(self):
        ticks = [self._ticks(), self._ticks(side="p", strike=3425.0)]
        assert smile.build_smile(ticks, "c") == [(3400.0, 18.0)]

    @pytest.mark.parametrize("bad", [{"iv": 0}, {"iv": None}, {"liquid": False}])
    def test_drops_unusable_rows(self, bad):
        assert smile.build_smile([self._ticks(**bad)], "c") == []

    def test_negative_iv_is_dropped(self):
        assert smile.build_smile([self._ticks(iv=-1.0)], "c") == []

    def test_illiquid_baseline_cannot_poison_interpolation(self):
        """An illiquid strike between two good ones must not appear in
        the curve the adjustment interpolates over."""
        ticks = [self._ticks(strike=3400.0, iv=18.0),
                 self._ticks(strike=3425.0, iv=99.0, liquid=False),
                 self._ticks(strike=3450.0, iv=18.0)]
        curve = smile.build_smile(ticks, "c")
        assert smile.interp(curve, 3425.0) == pytest.approx(18.0)
