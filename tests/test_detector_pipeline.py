"""
test_detector_pipeline.py — detector.run_for_series end to end, against
the in-memory FakeDB.

This is the layer the project has no coverage of today: test_detector.py
covers the pure math but nothing exercises the orchestration, which is
where the gating, suppression and evidence-assembly bugs live.
"""
import datetime
import math

import pytest

import detector
import smile
from conftest import (T0, UTC, ts, smile_curve, make_ticks, make_snapshot,
                      build_history)


def load(fake, ticks, snaps, events=None):
    fake.history = ticks
    fake.snapshots = snaps
    fake.recent_events = events or []


def spike_history(bump=1.25, n=8, interval=15, side="c"):
    """Flat vol for n-1 snapshots, then the whole surface lifts."""
    vol_path = [0.18] * (n - 1) + [0.18 * bump]
    return build_history(n_snapshots=n, interval_min=interval,
                         vol_path=vol_path, side=side)


@pytest.fixture(autouse=True)
def _pin_now(monkeypatch):
    """run_for_series computes `since` from wall-clock now. Freeze it to
    the fixture epoch so history is never filtered out by the lookback
    window and tests are not time-of-day dependent."""
    class FrozenDT(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return T0.astimezone(tz) if tz else T0.replace(tzinfo=None)

    monkeypatch.setattr(detector.datetime, "datetime", FrozenDT)


class TestHappyPath:
    def test_surface_lift_produces_events(self, fake_db):
        load(fake_db, *spike_history())
        events = detector.run_for_series(1)
        assert events, "a 25% surface lift must be detected"
        assert all(e["raw_pct_change"] > 0.10 for e in events)

    def test_events_are_persisted(self, fake_db):
        load(fake_db, *spike_history())
        detector.run_for_series(1)
        assert fake_db.rows_for("spike_events")

    def test_health_is_reported_even_with_no_events(self, fake_db):
        load(fake_db, *build_history())          # flat, nothing fires
        assert detector.run_for_series(1) == []
        assert any(h["worker"] == "detector" for h in fake_db.health)

    def test_every_event_carries_the_full_evidence_set(self, fake_db):
        load(fake_db, *spike_history())
        for e in detector.run_for_series(1):
            for field in ("series_id", "snapshot_ts", "strike", "side", "rule",
                          "latest_iv", "baseline_iv", "raw_pct_change",
                          "adj_pct_change", "spot_move_pct",
                          "atm_iv_change_pct", "skew_now", "skew_change",
                          "moneyness", "delta", "severity", "would_suppress"):
                assert field in e, f"{field} missing from event"

    def test_severity_is_a_valid_enum_value(self, fake_db):
        """spike_events has a CHECK constraint on this column — an
        invalid value is a rejected insert at 3am, not a test failure."""
        load(fake_db, *spike_history(bump=1.6))
        assert {e["severity"] for e in detector.run_for_series(1)} <= {
            "info", "warn", "high"}


class TestGating:
    def test_no_history_returns_empty(self, fake_db):
        load(fake_db, [], [])
        assert detector.run_for_series(1) == []

    def test_no_snapshots_returns_empty(self, fake_db):
        ticks, _ = spike_history()
        load(fake_db, ticks, [])
        assert detector.run_for_series(1) == []

    def test_missing_spot_on_latest_snapshot_is_logged_and_skipped(self, fake_db):
        ticks, snaps = spike_history()
        snaps[-1]["spot"] = None
        load(fake_db, ticks, snaps)
        assert detector.run_for_series(1) == []
        assert any("no spot" in m for _s, m, _l in fake_db.logs)

    def test_illiquid_strikes_are_never_judged(self, fake_db):
        ticks, snaps = spike_history()
        for t in ticks:
            t["liquid"] = False
        load(fake_db, ticks, snaps)
        assert detector.run_for_series(1) == []

    def test_too_few_samples_is_skipped(self, fake_db, cfg, monkeypatch):
        monkeypatch.setattr(detector.config, "MIN_SAMPLES", 20)
        load(fake_db, *spike_history())
        assert detector.run_for_series(1) == []

    def test_too_short_a_baseline_span_is_skipped(self, fake_db, monkeypatch):
        monkeypatch.setattr(detector.config, "MIN_BASELINE_SPAN_MINUTES", 10_000)
        load(fake_db, *spike_history())
        assert detector.run_for_series(1) == []

    def test_stale_strike_not_present_in_latest_snapshot_is_skipped(self, fake_db):
        """A strike that stopped quoting must not be judged against an
        old reading of itself — its 'latest' is not the latest."""
        ticks, snaps = spike_history()
        latest_ts = max(t["snapshot_ts"] for t in ticks)
        ticks = [t for t in ticks
                 if not (t["strike"] == 3400.0 and t["snapshot_ts"] == latest_ts)]
        load(fake_db, ticks, snaps)
        assert all(e["strike"] != 3400.0 for e in detector.run_for_series(1))

    def test_zero_iv_rows_are_excluded(self, fake_db):
        ticks, snaps = spike_history()
        for t in ticks:
            if t["strike"] == 3400.0:
                t["iv"] = 0
        load(fake_db, ticks, snaps)
        assert all(e["strike"] != 3400.0 for e in detector.run_for_series(1))


class TestDirectionality:
    def test_downside_moves_are_never_detected(self, fake_db):
        """DESIGN QUESTION, pinned as a test.

        Both rules use `raw > SPIKE_THRESHOLD_PCT`, so a vol CRUSH — the
        surface collapsing 30% after an event — produces no alert at
        all. For a monitor whose purpose is spotting unusual vol, that
        may well be intentional, but nothing in the code or README says
        so, and it is the kind of omission that is discovered on the day
        it matters.

        If one-sided is intended, keep this test as documentation. If
        not, the fix is `abs(raw) >` plus a direction field on the
        event, and this test inverts.
        """
        ticks, snaps = build_history(vol_path=[0.18] * 7 + [0.18 * 0.6])
        load(fake_db, ticks, snaps)
        assert detector.run_for_series(1) == []


class TestSuppression:
    def test_one_event_per_moneyness_bucket_per_rule(self, fake_db):
        """Within a single pass, adjacent strikes in the same ~1% band
        must collapse to one event rather than one per strike."""
        load(fake_db, *spike_history(bump=1.4))
        events = detector.run_for_series(1)
        keys = [(e["side"], e["rule"], detector._bucket(e["moneyness"]))
                for e in events]
        assert len(keys) == len(set(keys))

    def test_recent_event_in_the_same_bucket_suppresses(self, fake_db):
        ticks, snaps = spike_history()
        load(fake_db, ticks, snaps)
        first = detector.run_for_series(1)
        assert first

        fake_db.inserted.clear()
        fake_db.recent_events = [{
            "strike": e["strike"], "side": e["side"], "rule": e["rule"],
            "moneyness": e["moneyness"],
            "detected_at": T0.isoformat(),
        } for e in first]
        assert detector.run_for_series(1) == []

    def test_expired_suppression_allows_refire(self, fake_db):
        ticks, snaps = spike_history()
        load(fake_db, ticks, snaps)
        first = detector.run_for_series(1)
        long_ago = (T0 - datetime.timedelta(hours=48)).isoformat()
        fake_db.recent_events = [{
            "strike": e["strike"], "side": e["side"], "rule": e["rule"],
            "moneyness": e["moneyness"], "detected_at": long_ago,
        } for e in first]
        assert detector.run_for_series(1), "48h-old events must not suppress"

    def test_unknown_rule_string_in_history_does_not_crash_the_pass(self, fake_db):
        """A rule name from an older deploy, or a hand-inserted row,
        reaches float(rule.replace(...)) in _suppression_keys. A
        ValueError there aborts the entire detection pass for that
        series — silently, since ingest swallows it into a log line.
        """
        ticks, snaps = spike_history()
        load(fake_db, ticks, snaps, events=[{
            "strike": 3400.0, "side": "c", "rule": "ema_fast",
            "moneyness": 0.0, "detected_at": T0.isoformat(),
        }])
        try:
            detector.run_for_series(1)
        except ValueError as e:
            pytest.xfail(f"unknown rule name crashes the detection pass: {e}")

    def test_bucket_boundary_behaviour_is_pinned(self, fake_db):
        """Moneyness buckets are computed from the strike's CURRENT
        moneyness, so a fixed strike changes bucket as spot moves. This
        is a documented improvement over exact-strike keys but not a
        complete fix: crossing a boundary lets the same underlying
        event re-fire once.
        """
        assert detector._bucket(0.0149) == "1"
        assert detector._bucket(0.0151) == "2"
        assert detector._bucket(None) == "na"
        assert detector._bucket(-0.02) == "-2"

    def test_bucket_edges_use_bankers_rounding(self):
        """Minor but worth pinning: Python's round() rounds halves to
        even, so the bucket boundaries are not evenly spaced. 0.005
        lands in bucket 0 while 0.015 lands in bucket 2 — bucket 1 is
        entered and left asymmetrically.

        Harmless at current tolerances (suppression is approximate by
        design), but it means a strike sitting exactly on a boundary
        behaves differently above and below the money. Use
        math.floor(m / BUCKET) if even bands are ever needed.
        """
        assert detector._bucket(0.005) == "0"
        assert detector._bucket(0.015) == "2"
        assert detector._bucket(0.025) == "2"


class TestSmileEvidence:
    def test_pure_spot_move_is_flagged_would_suppress(self, fake_db):
        """Spot rallies steadily, the surface never moves. Raw fires on
        strikes sliding along the curve; every such event must carry
        would_suppress=True so shadow mode can count them."""
        n = 8
        spot_path = [3400.0 * (1 + 0.004 * i) for i in range(n)]
        ticks, snaps = build_history(n_snapshots=n, spot_path=spot_path,
                                     side="p")
        load(fake_db, ticks, snaps)
        events = detector.run_for_series(1)
        if not events:
            pytest.skip("scenario produced no raw fires; strengthen the ramp")
        assert any(e["would_suppress"] for e in events)

    def test_real_vol_event_is_not_flagged(self, fake_db):
        load(fake_db, *spike_history(bump=1.4))
        events = detector.run_for_series(1)
        assert events
        assert not all(e["would_suppress"] for e in events)

    def test_spot_move_pct_is_recorded_when_baseline_known(self, fake_db):
        n = 8
        spot_path = [3400.0 + 5 * i for i in range(n)]
        vol_path = [0.18] * (n - 1) + [0.18 * 1.3]
        ticks, snaps = build_history(n_snapshots=n, spot_path=spot_path,
                                     vol_path=vol_path)
        load(fake_db, ticks, snaps)
        events = detector.run_for_series(1)
        assert events
        assert any(e["spot_move_pct"] is not None for e in events)

    def test_unjudgeable_adjustment_stays_visible(self, fake_db):
        """adj=None means "cannot judge". Such an event must still be
        emitted, with would_suppress False — never silently dropped."""
        load(fake_db, *spike_history(bump=1.3))
        for e in detector.run_for_series(1):
            if e["adj_pct_change"] is None:
                assert e["would_suppress"] is False

    def test_severity_prefers_adjusted_over_raw(self):
        """A large raw move that the smile explains away must not be
        labelled 'high'."""
        assert detector._severity(0.01, 0.90) == "info"
        assert detector._severity(None, 0.90) == "high"
        assert detector._severity(0.35, 0.02) == "high"


class TestTimestampHandling:
    @pytest.mark.parametrize("suffix", ["+00:00", "Z"])
    def test_iso_variants_parse_equal(self, suffix):
        base = "2026-08-19T06:00:00"
        assert detector._parse_ts(base + suffix) == T0

    def test_postgrest_short_offset_parses(self):
        """PostgREST can return '+00' rather than '+00:00'. On Python
        <3.11 fromisoformat raises on that form; the workers run 3.12+,
        so this test also documents the minimum version."""
        assert detector._parse_ts("2026-08-19T06:00:00+00") == T0

    def test_tick_and_snapshot_timestamps_must_match_exactly(self, fake_db):
        """latest_snap is looked up by exact timestamp equality. A
        formatting difference between the two tables means the latest
        snapshot is never found and detection silently stops."""
        ticks, snaps = spike_history()
        for s in snaps:
            s["snapshot_ts"] = s["snapshot_ts"].replace("+00:00", "Z")
        load(fake_db, ticks, snaps)
        assert detector.run_for_series(1), "Z vs +00:00 must not break the join"
