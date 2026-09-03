"""
test_ema.py — time_weighted_ema.

The whole reason this function replaced a sample-count EMA is that the
ingest cadence is irregular (a browser downloads the files). So the
tests are mostly invariance properties under cadence perturbation,
which is exactly what a sample-count span fails.
"""
import datetime
import math

import pytest

from detector import time_weighted_ema
from conftest import ts, UTC


class TestBasics:
    def test_too_few_points(self):
        assert time_weighted_ema([], 90) is None
        assert time_weighted_ema([(ts(0), 18.0)], 90) is None

    def test_two_points_returns_the_older_one(self):
        """Baseline is everything except the latest, so with two points
        the baseline is a single reading."""
        assert time_weighted_ema([(ts(0), 18.0), (ts(15), 25.0)], 90) == 18.0

    def test_flat_series_returns_the_flat_value(self):
        pts = [(ts(i), 18.0) for i in range(0, 300, 15)]
        assert time_weighted_ema(pts, 90) == pytest.approx(18.0)

    def test_latest_point_excluded(self):
        """A spike must not pull the baseline toward itself, or every
        spike partially hides itself."""
        flat = [(ts(i), 18.0) for i in range(0, 300, 15)]
        spiked = flat[:-1] + [(flat[-1][0], 25.0)]
        assert time_weighted_ema(spiked, 90) == 18.0


class TestCadenceIndependence:
    def _sampled(self, step_min, f=lambda i: 18.0 + 0.5 * math.sin(i / 120)):
        return [(ts(i), f(i)) for i in range(0, 361, step_min)]

    @pytest.mark.parametrize("a,b", [(15, 20), (10, 30), (5, 25)])
    def test_same_trajectory_different_sampling(self, a, b):
        ea, eb = time_weighted_ema(self._sampled(a), 90), time_weighted_ema(self._sampled(b), 90)
        assert abs(ea - eb) < 0.1, (ea, eb)

    def test_a_missed_cycle_barely_moves_the_baseline(self):
        """A skipped download is normal. It must not shift the baseline
        the way dropping a sample from a fixed-span EMA would."""
        full = [(ts(i), 18.0 + i / 1000) for i in range(0, 361, 15)]
        gapped = [p for j, p in enumerate(full) if j != 5]
        assert time_weighted_ema(full, 90) == pytest.approx(
            time_weighted_ema(gapped, 90), abs=0.05)

    def test_duplicate_timestamps_are_skipped_not_crashed(self):
        """Two rows at the same instant would give dt=0 and a divide
        issue in a naive implementation."""
        pts = [(ts(0), 18.0), (ts(0), 19.0), (ts(15), 18.0), (ts(30), 20.0)]
        assert time_weighted_ema(pts, 90) is not None

    def test_out_of_order_input_is_not_silently_reweighted(self):
        """The docstring requires oldest-first. Unsorted input yields a
        negative dt, which the implementation skips — so the result
        differs from the sorted answer. Pinning this documents that
        callers MUST sort (detector.run_for_series does)."""
        ordered = [(ts(0), 18.0), (ts(15), 19.0), (ts(30), 20.0), (ts(45), 21.0)]
        shuffled = [ordered[1], ordered[0], ordered[2], ordered[3]]
        assert time_weighted_ema(ordered, 90) != time_weighted_ema(shuffled, 90)


class TestHalflifeSemantics:
    def test_shorter_halflife_tracks_recent_values_more_closely(self):
        pts = [(ts(0), 10.0)] + [(ts(i), 20.0) for i in range(15, 300, 15)]
        fast = time_weighted_ema(pts, 15)
        slow = time_weighted_ema(pts, 480)
        assert abs(fast - 20.0) < abs(slow - 20.0)

    def test_one_halflife_moves_about_halfway(self):
        """Two baseline points, 90 minutes apart, 90-minute half-life:
        alpha = 0.5, so the EMA should land midway."""
        pts = [(ts(0), 10.0), (ts(90), 20.0), (ts(105), 20.0)]
        assert time_weighted_ema(pts, 90) == pytest.approx(15.0)

    def test_zero_halflife_does_not_raise(self):
        """Guard against a misconfigured EMA_HALFLIFE_MINUTES=0 taking
        down the ingest loop. Currently 0.5**inf -> 0 so alpha -> 1."""
        pts = [(ts(0), 10.0), (ts(15), 20.0), (ts(30), 20.0)]
        assert time_weighted_ema(pts, 1e-9) == pytest.approx(20.0)


class TestDetectionArithmetic:
    def test_sharp_jump_clears_threshold(self):
        pts = [(ts(i), 18.0) for i in range(0, 300, 15)]
        pts[-1] = (pts[-1][0], 21.5)
        ema = time_weighted_ema(pts, 90)
        assert (pts[-1][1] - ema) / ema > 0.10

    def test_slow_drift_is_absorbed_by_the_ema(self):
        """The reason drift windows exist alongside the EMA: a gradual
        climb should NOT trip the EMA rule, which is why the
        point-to-point checks are needed to catch it."""
        pts = [(ts(i), 18.0 + 0.0075 * i) for i in range(0, 361, 15)]
        ema = time_weighted_ema(pts, 90)
        assert (pts[-1][1] - ema) / ema < 0.10
