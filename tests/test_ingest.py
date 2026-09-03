"""
test_ingest.py — the file lifecycle (stability, quarantine, retire) and
the write path.

Three of the four documented fixes in ingest.py's header are about file
handling on Windows, and none of them had a test. These are cheap to
cover because the logic is pure filesystem work.
"""
import datetime
import os
import shutil

import pytest

import ingest
from expiry_parser import snapshot_time_from_filename


@pytest.fixture(autouse=True)
def _clear_locked():
    ingest._locked.clear()
    ingest._latest_chain_ts.clear()
    yield
    ingest._locked.clear()
    ingest._latest_chain_ts.clear()


def touch(d, name, size=10):
    p = os.path.join(str(d), name)
    with open(p, "wb") as f:
        f.write(b"x" * size)
    return p


# --------------------------------------------------------------------
# stable_files
# --------------------------------------------------------------------
class TestStability:
    def test_a_file_is_not_ready_on_first_sight(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ingest.config, "WATCH_PATTERN", "options-*.xlsx")
        touch(tmp_path, "options-20260819-061500.xlsx")
        sizes = {}
        assert ingest.stable_files(str(tmp_path), sizes) == []

    def test_ready_after_an_unchanged_poll(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ingest.config, "WATCH_PATTERN", "options-*.xlsx")
        touch(tmp_path, "options-20260819-061500.xlsx")
        sizes = {}
        ingest.stable_files(str(tmp_path), sizes)
        assert len(ingest.stable_files(str(tmp_path), sizes)) == 1

    def test_a_growing_file_is_never_ready(self, tmp_path, monkeypatch):
        """The exact race the check exists to stop: Chrome still writing
        while glob already sees the file."""
        monkeypatch.setattr(ingest.config, "WATCH_PATTERN", "options-*.xlsx")
        p = touch(tmp_path, "options-20260819-061500.xlsx", 10)
        sizes = {}
        for size in (20, 30, 40):
            assert ingest.stable_files(str(tmp_path), sizes) == []
            with open(p, "wb") as f:
                f.write(b"x" * size)

    def test_locked_files_are_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ingest.config, "WATCH_PATTERN", "options-*.xlsx")
        p = touch(tmp_path, "options-20260819-061500.xlsx")
        ingest._locked.add(p)
        sizes = {}
        ingest.stable_files(str(tmp_path), sizes)
        assert ingest.stable_files(str(tmp_path), sizes) == []

    def test_a_vanished_file_is_forgotten(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ingest.config, "WATCH_PATTERN", "options-*.xlsx")
        p = touch(tmp_path, "options-20260819-061500.xlsx")
        sizes = {}
        ingest.stable_files(str(tmp_path), sizes)
        os.remove(p)
        ingest.stable_files(str(tmp_path), sizes)
        assert sizes == {}

    def test_non_matching_files_are_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ingest.config, "WATCH_PATTERN", "options-*.xlsx")
        touch(tmp_path, "notes.txt")
        touch(tmp_path, "positions-20260819.xlsx")
        sizes = {}
        ingest.stable_files(str(tmp_path), sizes)
        assert ingest.stable_files(str(tmp_path), sizes) == []

    def test_stable_checks_of_one_makes_the_first_poll_ready(
            self, tmp_path, monkeypatch):
        """Boundary on the config knob — FILE_STABLE_CHECKS=1 disables
        the protection entirely. Pinned so the effect of setting it is
        obvious to whoever tunes it."""
        monkeypatch.setattr(ingest.config, "WATCH_PATTERN", "options-*.xlsx")
        monkeypatch.setattr(ingest.config, "FILE_STABLE_CHECKS", 1)
        touch(tmp_path, "options-20260819-061500.xlsx")
        assert len(ingest.stable_files(str(tmp_path), {})) == 1


# --------------------------------------------------------------------
# quarantine / retire
# --------------------------------------------------------------------
class TestFileDisposal:
    def test_quarantine_moves_rather_than_deletes(self, tmp_path):
        p = touch(tmp_path, "options-20260819-061500.xlsx")
        ingest.quarantine(p)
        assert not os.path.exists(p)
        assert os.path.exists(tmp_path / "failed" / "options-20260819-061500.xlsx")

    def test_quarantine_twice_does_not_raise(self, tmp_path):
        """A replayed failure must not take down the loop."""
        p = touch(tmp_path, "options-20260819-061500.xlsx")
        ingest.quarantine(p)
        touch(tmp_path, "options-20260819-061500.xlsx")
        ingest.quarantine(p)   # destination already exists

    def test_retire_deletes_on_the_happy_path(self, tmp_path):
        p = touch(tmp_path, "options-20260819-061500.xlsx")
        assert ingest.retire(p) is True
        assert not os.path.exists(p)

    def test_retire_falls_back_to_moving_when_delete_fails(
            self, tmp_path, monkeypatch, fake_db):
        p = touch(tmp_path, "options-20260819-061500.xlsx")
        monkeypatch.setattr(ingest.os, "remove",
                            lambda _p: (_ for _ in ()).throw(OSError("locked")))
        monkeypatch.setattr(ingest.time, "sleep", lambda _s: None)
        assert ingest.retire(p) is True
        assert os.path.exists(tmp_path / "processed" / "options-20260819-061500.xlsx")

    def test_retire_gives_up_into_the_locked_set(
            self, tmp_path, monkeypatch, fake_db):
        """Both fallbacks fail (Excel holding the file). The file must
        be remembered so it is not reprocessed every poll forever."""
        p = touch(tmp_path, "options-20260819-061500.xlsx")
        monkeypatch.setattr(ingest.os, "remove",
                            lambda _p: (_ for _ in ()).throw(OSError()))
        monkeypatch.setattr(ingest.shutil, "move",
                            lambda *_a: (_ for _ in ()).throw(OSError()))
        monkeypatch.setattr(ingest.time, "sleep", lambda _s: None)
        assert ingest.retire(p) is False
        assert p in ingest._locked
        assert any("locked" in m for _s, m, _l in fake_db.logs)


# --------------------------------------------------------------------
# expiry label parsing
# --------------------------------------------------------------------
class TestExpiryLabel:
    def test_full_label(self):
        assert ingest.parse_expiry_label("0D (Exp: Jul 22) GCE34/N26") == \
            (0, "Jul 22", "GCE34/N26")

    def test_multi_digit_days(self):
        days, date, sym = ingest.parse_expiry_label("127D (Exp: Dec 24) GCZ26")
        assert days == 127 and date == "Dec 24" and sym == "GCZ26"

    def test_partial_label_falls_back_to_day_count_only(self):
        assert ingest.parse_expiry_label("30D something else") == (30, None, None)

    @pytest.mark.parametrize("label", [None, "", "no day count here"])
    def test_unparseable(self, label):
        assert ingest.parse_expiry_label(label) == (None, None, None)


# --------------------------------------------------------------------
# ingest_one — the write path
# --------------------------------------------------------------------
class TestIngestOne:
    def test_happy_path_writes_all_three_tables(self, chain_xlsx, fake_db,
                                                monkeypatch):
        monkeypatch.setattr(ingest.smile, "detect_spot", lambda r: 3400.0)
        result = ingest.ingest_one(chain_xlsx())
        assert result is not None
        assert {t for t, *_ in fake_db.inserted} == {
            "snapshots", "iv_ticks", "latest_chain"}

    def test_snapshot_timestamp_comes_from_the_filename(self, chain_xlsx,
                                                        fake_db):
        ingest.ingest_one(chain_xlsx(name="options-20260819-061500.xlsx"))
        snap = fake_db.rows_for("snapshots")[0]
        assert snap["snapshot_ts"].startswith("2026-08-19T06:15:00")

    def test_unparseable_filename_is_quarantined_not_guessed(
            self, chain_xlsx, fake_db, tmp_path):
        path = chain_xlsx(name="options-export-final.xlsx")
        assert ingest.ingest_one(path) is None
        assert not fake_db.inserted, "nothing may be written for an untimed file"
        assert os.path.exists(tmp_path / "failed" / "options-export-final.xlsx")

    def test_a_chrome_duplicate_download_maps_to_the_same_snapshot(
            self, chain_xlsx, fake_db):
        """Chrome names a re-download 'options-...-061500 (1).xlsx'.
        The regex is a search, so it still extracts the original
        timestamp — which is what makes the unique constraint
        deduplicate it instead of creating a second snapshot."""
        a = snapshot_time_from_filename("options-20260819-061500.xlsx")
        b = snapshot_time_from_filename("options-20260819-061500 (1).xlsx")
        assert a == b

    def test_idempotency_uses_ignore_duplicates_on_the_natural_key(
            self, chain_xlsx, fake_db):
        ingest.ingest_one(chain_xlsx())
        assert fake_db.conflict_spec("snapshots") == ("series_id,snapshot_ts", False)
        assert fake_db.conflict_spec("iv_ticks") == (
            "snapshot_ts,series_id,strike,side", False)

    def test_latest_chain_is_upserted(self, chain_xlsx, fake_db):
        ingest.ingest_one(chain_xlsx())
        assert fake_db.conflict_spec("latest_chain") == ("series_id,strike", True)

    def test_two_tick_rows_per_strike(self, chain_xlsx, fake_db):
        ingest.ingest_one(chain_xlsx(strikes=[3400, 3425]))
        ticks = fake_db.rows_for("iv_ticks")
        assert len(ticks) == 4
        assert {t["side"] for t in ticks} == {"c", "p"}

    def test_parse_failure_quarantines_and_writes_nothing(
            self, tmp_path, fake_db):
        bad = tmp_path / "options-20260819-061500.xlsx"
        bad.write_bytes(b"this is not a workbook")
        assert ingest.ingest_one(str(bad)) is None
        assert not fake_db.inserted
        assert os.path.exists(tmp_path / "failed" / bad.name)

    def test_a_failed_tick_write_leaves_the_file_in_place_for_retry(
            self, chain_xlsx, fake_db):
        """Snapshots insert succeeds, iv_ticks fails. The exception
        propagates to the run loop; the file must NOT have been retired,
        or the snapshot is orphaned with no ticks and no way to replay.
        """
        path = chain_xlsx()
        fake_db.fail_on_table = "iv_ticks"
        with pytest.raises(RuntimeError):
            ingest.ingest_one(path)
        assert os.path.exists(path), "file must survive for the next poll"

    def test_liquid_count_mixes_strikes_and_sides(self, chain_xlsx, fake_db):
        """COSMETIC DEFECT, pinned.

        liquid_strike_count increments per (strike, side), so it can
        reach 2x strike_count. The startup log line the README tells you
        to sanity-check — '247 strikes, 132 liquid' — is therefore
        comparing counts in different units, and the README's advice to
        loosen the gate 'if liquid is a small fraction of strikes' is
        calibrated against the wrong denominator.
        """
        ingest.ingest_one(chain_xlsx(strikes=[3400, 3425]))
        snap = fake_db.rows_for("snapshots")[0]
        assert snap["strike_count"] == 2
        assert snap["liquid_strike_count"] > snap["strike_count"]


class TestTimezoneHandling:
    def test_filename_time_is_stamped_utc_without_conversion(
            self, chain_xlsx, fake_db):
        """SUSPECTED DEFECT — verify against your VPS before fixing.

        expiry_parser.snapshot_time_from_filename explicitly returns a
        NAIVE datetime and documents that 'caller must localize it'.
        ingest_one instead does `.replace(tzinfo=utc)`, which asserts
        the filename time was already UTC.

        CQG names the file in the browser's local time. If the VPS runs
        on anything other than UTC, every snapshot_ts is wrong by the
        offset. Consequences, in order of severity:

          - On a VPS ahead of UTC (e.g. Dubai, +4), snapshots are
            stamped 4 hours in the FUTURE. detector's lookback window
            still finds them, but suppression compares
            server-now-based detected_at against these, and
            watchdog staleness maths is skewed.
          - On a VPS behind UTC, snapshots look older than they are and
            can fall outside the lookback window entirely — detection
            silently stops, exactly the failure class db.py was
            written to prevent.

        Fix: either localize with the VPS zone and convert
        (`.astimezone(utc)`), or configure the VPS to UTC and assert it
        at startup. This test pins today's behaviour.
        """
        ingest.ingest_one(chain_xlsx(name="options-20260819-061500.xlsx"))
        ts = fake_db.rows_for("snapshots")[0]["snapshot_ts"]
        assert ts.endswith("+00:00")
        assert "06:15:00" in ts, "local clock time was reinterpreted as UTC"


class TestLatestChainOrdering:
    @pytest.mark.xfail(reason="latest_chain upsert has no snapshot_ts guard, "
                              "so replaying an old file clobbers current data",
                       strict=False)
    def test_replaying_an_old_file_must_not_clobber_newer_chain_data(
            self, chain_xlsx, fake_db):
        """DEFECT — asserts the DESIRED behaviour, so this flips to
        XPASS once fixed.

        latest_chain is upserted on (series_id, strike) with no guard on
        snapshot_ts. Replaying a quarantined file from this morning —
        which the README explicitly invites you to do, and which the
        `failed/` folder exists to make possible — silently overwrites
        the current chain with stale prices. The payoff builder and the
        scanner both read latest_chain, so the effect is wrong premiums
        in the UI with no error anywhere.

        Fix: `on conflict ... do update ... where latest_chain.snapshot_ts
        < excluded.snapshot_ts`, or skip the write in the worker when
        the file is older than what is already stored.
        """
        ingest.ingest_one(chain_xlsx(name="options-20260819-140000.xlsx"))
        newest = fake_db.rows_for("latest_chain")[0]["snapshot_ts"]

        fake_db.inserted.clear()
        ingest.ingest_one(chain_xlsx(name="options-20260819-060000.xlsx"))
        written = fake_db.rows_for("latest_chain")

        assert not written or written[0]["snapshot_ts"] >= newest, (
            "an older file was written over the newer chain")
