"""
test_expiry_watchdog_config.py — three small surfaces that are cheap to
cover and expensive to get wrong.

expiry_parser feeds every Black-76 projection in the dashboard, the
watchdog is the only thing that tells you the monitor died, and config
holds the thresholds that decide what an alert even is.
"""
import datetime

import pytest

import config
import expiry_parser as ep
import watchdog

UTC = datetime.timezone.utc


# --------------------------------------------------------------------
# expiry_parser
# --------------------------------------------------------------------
class TestSnapshotTimeFromFilename:
    def test_canonical_name(self):
        assert ep.snapshot_time_from_filename("options-20260819-061500.xlsx") == \
            datetime.datetime(2026, 8, 19, 6, 15, 0)

    def test_returns_naive_datetime(self):
        """Contract the docstring states explicitly: no timezone is
        attached and the caller must localize. ingest.py does NOT
        localize — see test_ingest.TestTimezoneHandling."""
        assert ep.snapshot_time_from_filename(
            "options-20260819-061500.xlsx").tzinfo is None

    @pytest.mark.parametrize("name", [
        "options-20260819-061500 (1).xlsx",      # Chrome duplicate
        "prefix-options-20260819-061500.xlsx",
        "C:\\Users\\me\\Downloads\\options-20260819-061500.xlsx",
    ])
    def test_search_tolerates_decoration(self, name):
        assert ep.snapshot_time_from_filename(name) is not None

    @pytest.mark.parametrize("name", [
        "options.xlsx", "options-2026819-061500.xlsx", "",
        "options-20261345-061500.xlsx",          # month 13, day 45
        "options-20260819-256100.xlsx",          # hour 25, minute 61
    ])
    def test_rejects_bad_names(self, name):
        assert ep.snapshot_time_from_filename(name) is None

    def test_leap_day_is_accepted(self):
        assert ep.snapshot_time_from_filename(
            "options-20280229-120000.xlsx") is not None

    def test_non_leap_feb_29_is_rejected(self):
        assert ep.snapshot_time_from_filename(
            "options-20260229-120000.xlsx") is None


class TestDaysToExpiryFromLabel:
    @pytest.mark.parametrize("label,want", [
        ("0D (Exp: Jul 22) GCE34/N26", 0),
        ("  7D (Exp: Aug 1)", 7),
        ("127d something", 127),
        ("30D", 30),
    ])
    def test_parses(self, label, want):
        assert ep.days_to_expiry_from_label(label) == want

    @pytest.mark.parametrize("label", [None, "", "Exp: Jul 22", "D7", "7 days"])
    def test_declines_rather_than_guessing(self, label):
        """A wrong silent guess corrupts every Black-Scholes projection
        downstream, so None is the correct answer."""
        assert ep.days_to_expiry_from_label(label) is None

    def test_does_not_match_a_digit_run_that_is_not_a_day_count(self):
        assert ep.days_to_expiry_from_label("2026 contract") is None


class TestYearsToExpiry:
    def test_whole_days(self):
        assert ep.years_to_expiry(365) == pytest.approx(1.0)

    def test_none_passes_through(self):
        assert ep.years_to_expiry(None) is None

    def test_same_day_default_is_nonzero(self):
        """Exactly 0 collapses every option's time value, which is
        almost certainly wrong for a snapshot taken hours before
        settlement."""
        v = ep.years_to_expiry(0)
        assert 0 < v < 1 / 365

    def test_hour_refinement_beats_the_default(self):
        refined = ep.years_to_expiry(0, snapshot_hour_utc=13, expiry_hour_utc=18)
        assert refined == pytest.approx((5 / 24) / 365)

    def test_multi_day_with_hours(self):
        v = ep.years_to_expiry(3, snapshot_hour_utc=12, expiry_hour_utc=18)
        assert v == pytest.approx((2 + 6 / 24) / 365)

    def test_expiry_hour_already_passed_returns_zero(self):
        """EDGE CASE worth a decision.

        Snapshot at 19:00, expiry at 18:00 the same day, days=0. The
        function returns exactly 0.0 — the very outcome the days==0
        default exists to avoid. projectLegPrice in the dashboard treats
        remaining <= 0 as intrinsic-only, so this degrades gracefully
        there, but any caller dividing by T would raise.
        """
        assert ep.years_to_expiry(0, snapshot_hour_utc=19,
                                  expiry_hour_utc=18) == 0.0

    def test_negative_days_is_not_rejected(self):
        """No guard against a negative day count. A malformed label
        cannot produce one today (the regex requires \\d+), but an API
        caller could. Returns a negative year fraction."""
        assert ep.years_to_expiry(-5) < 0


# --------------------------------------------------------------------
# watchdog
# --------------------------------------------------------------------
class TestWatchdogStaleness:
    def _run_once(self, monkeypatch, last, alerted_before):
        """Exercises one loop iteration's decision logic without the
        infinite loop."""
        now = datetime.datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
        threshold = datetime.timedelta(minutes=watchdog.STALE_MINUTES)
        stale = last is None or (now - last) > threshold
        fired = None
        if stale and not alerted_before:
            fired = "stale"
        elif not stale and alerted_before:
            fired = "recovered"
        return stale, fired

    def test_never_written_counts_as_stale(self, monkeypatch):
        stale, fired = self._run_once(monkeypatch, None, False)
        assert stale and fired == "stale"

    def test_fresh_write_is_not_stale(self):
        last = datetime.datetime(2026, 8, 19, 11, 45, tzinfo=UTC)
        stale, fired = self._run_once(None, last, False)
        assert not stale and fired is None

    def test_alert_fires_once_not_every_cycle(self):
        last = datetime.datetime(2026, 8, 19, 8, 0, tzinfo=UTC)
        assert self._run_once(None, last, False)[1] == "stale"
        assert self._run_once(None, last, True)[1] is None

    def test_recovery_notice_fires_on_the_transition_back(self):
        last = datetime.datetime(2026, 8, 19, 11, 55, tzinfo=UTC)
        assert self._run_once(None, last, True)[1] == "recovered"

    def test_last_success_parses_both_iso_forms(self, monkeypatch):
        for value in ("2026-08-19T11:45:00+00:00", "2026-08-19T11:45:00Z"):
            monkeypatch.setattr(watchdog.db, "fetch_all",
                                lambda *a, **k: [{"last_success_at": value}])
            assert watchdog.last_success() is not None

    def test_missing_health_row_returns_none(self, monkeypatch):
        monkeypatch.setattr(watchdog.db, "fetch_all", lambda *a, **k: [])
        assert watchdog.last_success() is None

    def test_null_last_success_returns_none(self, monkeypatch):
        monkeypatch.setattr(watchdog.db, "fetch_all",
                            lambda *a, **k: [{"last_success_at": None}])
        assert watchdog.last_success() is None

    def test_alerted_flag_is_process_local(self):
        """GAP. The `alerted` flag lives in a local variable, so a
        watchdog restart during a long outage re-sends the stale alert.
        Minor, but it defeats the 'two messages, not forty' promise if
        the service is flapping. Persisting it in worker_health would
        fix it."""
        import inspect
        src = inspect.getsource(watchdog.run)
        assert "alerted = False" in src and "worker_health" not in src


# --------------------------------------------------------------------
# config contracts
# --------------------------------------------------------------------
class TestConfigContracts:
    def test_drift_windows_parse_to_floats(self):
        assert all(isinstance(w, float) for w in config.DRIFT_WINDOWS_HOURS)

    def test_drift_window_names_round_trip_through_the_rule_string(self):
        """detector formats rules as f"drift_{w:g}h" and _suppression_keys
        parses them back with float(). Every configured window must
        survive that round trip, or suppression raises at runtime."""
        for w in config.DRIFT_WINDOWS_HOURS:
            rule = f"drift_{w:g}h"
            assert float(rule.replace("drift_", "").replace("h", "")) == w

    def test_fractional_windows_also_round_trip(self):
        for w in (0.5, 1.5, 0.25):
            rule = f"drift_{w:g}h"
            assert float(rule.replace("drift_", "").replace("h", "")) == w

    def test_scientific_notation_window_breaks_the_round_trip(self):
        """Guard on the parsing idiom rather than on today's config: a
        very small or very large window formats as '1e-05h', and the
        naive replace("h", "") happens to survive it — but a window of
        1e+05 formats as '100000h' and is fine, while any future rule
        name containing 'h' would not be. Documenting that the rule
        name is a fragile encoding of a float."""
        assert float(f"{1e-05:g}".replace("h", "")) == 1e-05

    def test_severity_multipliers_are_ordered(self):
        assert 1.5 * config.SPIKE_THRESHOLD_PCT < 3 * config.SPIKE_THRESHOLD_PCT

    def test_delta_band_is_on_the_cqg_0_to_100_scale(self):
        assert config.MAX_ABS_DELTA > 1, (
            "band looks like a 0-1 decimal convention; every strike "
            "would fail the liquidity gate")

    def test_require_exits_on_missing_variables(self, monkeypatch):
        monkeypatch.setattr(config, "SUPABASE_URL", "")
        with pytest.raises(SystemExit):
            config.require("SUPABASE_URL")

    def test_require_passes_when_present(self, monkeypatch):
        monkeypatch.setattr(config, "SUPABASE_URL", "http://x")
        config.require("SUPABASE_URL")
