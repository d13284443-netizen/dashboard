"""
test_db.py — the paging and ordering guarantees.

db.py's own header explains that this file exists to prevent a specific
silent failure: an ascending, capped history query that quietly returned
stale rows, so `history[-1]` stopped being the latest reading and
detection stopped without ever raising. That is the single most
expensive class of bug in this system — it produces no error, no alert,
and no gap in the data. It deserves direct tests.

All HTTP is faked. Nothing here reaches Supabase.
"""
import datetime

import pytest

import db

UTC = datetime.timezone.utc
SINCE = datetime.datetime(2026, 8, 19, 0, 0, tzinfo=UTC)


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = "error body"

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def captured_get(monkeypatch):
    """Serves pages from a scripted list and records every request."""
    calls = []

    def make(pages):
        def fake_get(url, headers=None, params=None, timeout=None):
            calls.append({"url": url, "headers": headers or {},
                          "params": params or {}})
            idx = len(calls) - 1
            return FakeResp(pages[idx] if idx < len(pages) else [])
        monkeypatch.setattr(db.requests, "get", fake_get)
        return calls

    return make


class TestPaging:
    def test_a_single_short_page_ends_the_loop(self, captured_get):
        calls = captured_get([[{"id": 1}, {"id": 2}]])
        rows = db.fetch_all("iv_ticks", {})
        assert len(rows) == 2
        assert len(calls) == 1

    def test_a_full_page_triggers_another_request(self, captured_get):
        full = [{"id": i} for i in range(db.PAGE)]
        calls = captured_get([full, [{"id": 9999}]])
        rows = db.fetch_all("iv_ticks", {})
        assert len(rows) == db.PAGE + 1
        assert len(calls) == 2

    def test_range_headers_advance_by_page_size(self, captured_get):
        full = [{"id": i} for i in range(db.PAGE)]
        calls = captured_get([full, full, []])
        db.fetch_all("iv_ticks", {})
        ranges = [c["headers"]["Range"] for c in calls]
        assert ranges[0] == f"0-{db.PAGE - 1}"
        assert ranges[1] == f"{db.PAGE}-{2 * db.PAGE - 1}"

    def test_paging_survives_a_server_side_cap_below_page_size(
            self, captured_get, monkeypatch):
        """THE ORIGINAL BUG, in miniature.

        Supabase's db-max-rows can be lower than PAGE. When it is, every
        page comes back short and the loop stops after one request —
        which means fetch_all returns a TRUNCATED result while looking
        like a complete one.

        This is why the DESC ordering in fetch_history matters so much:
        it is the only thing that keeps a truncated window harmless.
        Pinned here so the interaction is visible.
        """
        server_cap = 100
        capped = [{"id": i} for i in range(server_cap)]
        calls = captured_get([capped, capped])
        rows = db.fetch_all("iv_ticks", {}, page_size=db.PAGE)
        assert len(rows) == server_cap
        assert len(calls) == 1, "a short page is indistinguishable from the end"

    def test_hard_cap_logs_loudly_rather_than_truncating_silently(
            self, captured_get, monkeypatch):
        logged = []
        monkeypatch.setattr(db, "log",
                            lambda s, m, level="info": logged.append((m, level)))
        page = [{"id": i} for i in range(10)]
        captured_get([page] * 20)
        rows = db.fetch_all("iv_ticks", {}, page_size=10, hard_cap=30)
        assert len(rows) == 30
        assert any(lvl == "error" and "hard cap" in m for m, lvl in logged)

    def test_http_error_propagates(self, monkeypatch):
        monkeypatch.setattr(db.requests, "get",
                            lambda *a, **k: FakeResp([], status=500))
        with pytest.raises(RuntimeError):
            db.fetch_all("iv_ticks", {})


class TestHistoryOrdering:
    def test_history_is_requested_descending(self, captured_get):
        calls = captured_get([[]])
        db.fetch_history(1, SINCE)
        assert calls[0]["params"]["order"] == "snapshot_ts.desc"

    def test_history_is_returned_oldest_first(self, captured_get):
        captured_get([[{"snapshot_ts": "2026-08-19T07:00:00+00:00"},
                       {"snapshot_ts": "2026-08-19T06:00:00+00:00"}]])
        rows = db.fetch_history(1, SINCE)
        assert rows[0]["snapshot_ts"] < rows[-1]["snapshot_ts"]

    def test_a_truncated_history_keeps_the_newest_rows(self, captured_get):
        """The guarantee that makes truncation survivable: with DESC on
        the wire, a cap drops the OLD end (baseline gets shorter) rather
        than the NEW end (latest becomes stale)."""
        newest = "2026-08-19T12:00:00+00:00"
        captured_get([[{"snapshot_ts": newest},
                       {"snapshot_ts": "2026-08-19T11:00:00+00:00"}]])
        rows = db.fetch_history(1, SINCE)
        assert rows[-1]["snapshot_ts"] == newest

    def test_snapshots_are_also_descending_then_reversed(self, captured_get):
        calls = captured_get([[]])
        db.fetch_snapshots(1, SINCE)
        assert calls[0]["params"]["order"] == "snapshot_ts.desc"

    def test_history_selects_only_the_narrow_column_set(self, captured_get):
        calls = captured_get([[]])
        db.fetch_history(1, SINCE)
        selected = set(calls[0]["params"]["select"].split(","))
        assert selected == {"snapshot_ts", "strike", "side", "iv", "delta",
                            "moneyness", "oi", "liquid"}

    def test_since_filter_is_applied(self, captured_get):
        calls = captured_get([[]])
        db.fetch_history(1, SINCE)
        assert calls[0]["params"]["snapshot_ts"].startswith("gte.")

    def test_events_query_is_scoped_to_the_series(self, captured_get):
        calls = captured_get([[]])
        db.fetch_recent_events(7, SINCE)
        assert calls[0]["params"]["series_id"] == "eq.7"


class TestWrites:
    @pytest.fixture
    def captured_post(self, monkeypatch):
        calls = []

        def fake_post(url, headers=None, params=None, json=None, timeout=None):
            calls.append({"url": url, "headers": headers or {},
                          "params": params or {}, "json": json})
            return FakeResp([{"id": 1}], status=201)

        monkeypatch.setattr(db.requests, "post", fake_post)
        return calls

    def test_empty_rows_is_a_no_op(self, captured_post):
        db.insert("iv_ticks", [])
        assert captured_post == []

    def test_upsert_requests_merge_duplicates(self, captured_post):
        db.insert("latest_chain", [{"a": 1}], on_conflict="series_id,strike",
                  upsert=True)
        assert "merge-duplicates" in captured_post[0]["headers"]["Prefer"]

    def test_non_upsert_requests_ignore_duplicates(self, captured_post):
        db.insert("iv_ticks", [{"a": 1}], on_conflict="k", upsert=False)
        assert "ignore-duplicates" in captured_post[0]["headers"]["Prefer"]

    def test_failed_insert_raises_with_context(self, monkeypatch):
        monkeypatch.setattr(db.requests, "post",
                            lambda *a, **k: FakeResp([], status=409))
        with pytest.raises(RuntimeError, match="iv_ticks"):
            db.insert("iv_ticks", [{"a": 1}], on_conflict="k")

    def test_health_reporting_never_raises(self, monkeypatch):
        """Explicit design promise in the source: health reporting must
        never take down the worker."""
        monkeypatch.setattr(db.requests, "post",
                            lambda *a, **k: (_ for _ in ()).throw(
                                OSError("network down")))
        db.upsert_health("ingest", success=True)

    def test_log_prints_before_it_writes(self, monkeypatch, capsys):
        """VPS-side logs must not depend on Supabase being reachable."""
        monkeypatch.setattr(db.requests, "post",
                            lambda *a, **k: (_ for _ in ()).throw(
                                OSError("network down")))
        db.log("ingest", "hello")
        assert "hello" in capsys.readouterr().out

    def test_errors_go_to_stderr(self, monkeypatch, capsys):
        monkeypatch.setattr(db.requests, "post", lambda *a, **k: FakeResp([]))
        db.log("ingest", "boom", level="error")
        assert "boom" in capsys.readouterr().err

    def test_long_messages_are_truncated_before_sending(self, captured_post):
        db.log("ingest", "x" * 10_000)
        assert len(captured_post[0]["json"][0]["message"]) <= 4000


class TestSeriesCache:
    @pytest.fixture(autouse=True)
    def _clear(self):
        db._series_cache.clear()
        yield
        db._series_cache.clear()

    def test_existing_series_is_found_not_recreated(self, monkeypatch):
        monkeypatch.setattr(db, "fetch_all", lambda *a, **k: [{"id": 4}])
        monkeypatch.setattr(db, "insert_returning",
                            lambda *a: pytest.fail("must not insert"))
        assert db.get_or_create_series("GOLD", "GCE1/Q26", "Sep 25") == 4

    def test_missing_series_is_created(self, monkeypatch):
        monkeypatch.setattr(db, "fetch_all", lambda *a, **k: [])
        monkeypatch.setattr(db, "insert_returning", lambda *a: [{"id": 9}])
        assert db.get_or_create_series("GOLD", "NEW", None) == 9

    def test_second_call_is_served_from_cache(self, monkeypatch):
        hits = []
        monkeypatch.setattr(db, "fetch_all",
                            lambda *a, **k: hits.append(1) or [{"id": 4}])
        db.get_or_create_series("GOLD", "GCE1/Q26", None)
        db.get_or_create_series("GOLD", "GCE1/Q26", None)
        assert len(hits) == 1

    def test_cache_key_ignores_the_expiry_label(self, monkeypatch):
        """The cache is keyed on (instrument, symbol) only. If a symbol
        is ever reused across expiries, the first label wins forever
        within the process. Harmless for CQG's naming (the symbol
        encodes the expiry) but worth pinning."""
        monkeypatch.setattr(db, "fetch_all", lambda *a, **k: [{"id": 4}])
        a = db.get_or_create_series("GOLD", "GCE1/Q26", "Sep 25")
        b = db.get_or_create_series("GOLD", "GCE1/Q26", "Oct 30")
        assert a == b
