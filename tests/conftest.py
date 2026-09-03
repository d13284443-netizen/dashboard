"""
conftest.py — shared fixtures.

Design decision worth stating: nothing here touches Supabase, Telegram,
or the network. `db` is replaced with an in-memory fake and `requests`
is stubbed out, so the whole suite runs in CI with no secrets and no
external service. Anything that genuinely needs a live database belongs
in the SQL suite (tests/sql/), not here.

The worker modules read `config` at call time (not import time) for
most values, so tuning knobs can be monkeypatched per-test.
"""
import datetime
import importlib
import math
import os
import sys
from pathlib import Path

import pytest

WORKER = Path(__file__).resolve().parent.parent / "worker"
sys.path.insert(0, str(WORKER))

# Keep the real .env out of the test run entirely. Without this, a
# developer's local .env would silently change detector thresholds and
# make tests pass or fail depending on whose machine they run on.
os.environ.setdefault("SUPABASE_URL", "http://test.invalid")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-key")
for _k in ("SPIKE_THRESHOLD_PCT", "EMA_HALFLIFE_MINUTES", "MIN_SAMPLES",
           "MIN_BASELINE_SPAN_MINUTES", "DRIFT_WINDOWS_HOURS", "MIN_OI",
           "MAX_SPREAD_PCT", "MIN_ABS_DELTA", "MAX_ABS_DELTA",
           "MONEYNESS_BUCKET", "SUPPRESS_EMA_HOURS", "ALERT_ON_RAW",
           "MAX_ALERTS_PER_CYCLE", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
    os.environ.pop(_k, None)

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 8, 19, 6, 0, tzinfo=UTC)


def ts(minutes):
    return T0 + datetime.timedelta(minutes=minutes)


# --------------------------------------------------------------------
# In-memory stand-in for db.py
# --------------------------------------------------------------------
class FakeDB:
    """Records every write and serves canned reads.

    Deliberately does NOT emulate PostgREST semantics beyond what the
    workers rely on — on_conflict behaviour is recorded and asserted,
    not simulated, because the real guarantee lives in the SQL
    constraints and is tested in tests/sql/.
    """

    def __init__(self):
        self.history = []
        self.snapshots = []
        self.recent_events = []
        self.inserted = []          # [(table, rows, on_conflict, upsert)]
        self.health = []
        self.logs = []
        self.series_ids = {}
        self._next_series = 1
        self.fail_on_table = None   # set to a table name to simulate a write failure

    # ---- reads
    def fetch_history(self, series_id, since_dt):
        return [dict(r) for r in self.history
                if datetime.datetime.fromisoformat(
                    r["snapshot_ts"].replace("Z", "+00:00")) >= since_dt]

    def fetch_snapshots(self, series_id, since_dt):
        return [dict(r) for r in self.snapshots
                if datetime.datetime.fromisoformat(
                    r["snapshot_ts"].replace("Z", "+00:00")) >= since_dt]

    def fetch_recent_events(self, series_id, since_dt):
        return [dict(r) for r in self.recent_events]

    def fetch_all(self, table, params, **kw):
        return []

    # ---- writes
    def insert(self, table, rows, on_conflict=None, upsert=False, timeout=None):
        if table == self.fail_on_table:
            raise RuntimeError(f"simulated write failure on {table}")
        self.inserted.append((table, [dict(r) for r in rows], on_conflict, upsert))

    def insert_returning(self, table, rows):
        return [{"id": 1}]

    def upsert_health(self, worker, success=False, error=None, detail=None):
        self.health.append({"worker": worker, "success": success,
                            "error": error, "detail": detail})

    def log(self, source, message, level="info"):
        self.logs.append((source, str(message), level))

    def get_or_create_series(self, instrument, symbol, expiry_label):
        key = (instrument, symbol)
        if key not in self.series_ids:
            self.series_ids[key] = self._next_series
            self._next_series += 1
        return self.series_ids[key]

    # ---- assertions helpers
    def rows_for(self, table):
        out = []
        for t, rows, _oc, _up in self.inserted:
            if t == table:
                out.extend(rows)
        return out

    def conflict_spec(self, table):
        for t, _rows, oc, up in self.inserted:
            if t == table:
                return oc, up
        return None, None


@pytest.fixture
def fake_db(monkeypatch):
    """Swaps the fake into every module that imported `db`."""
    import db as real_db
    fake = FakeDB()
    for name in ("fetch_history", "fetch_snapshots", "fetch_recent_events",
                 "fetch_all", "insert", "insert_returning", "upsert_health",
                 "log", "get_or_create_series"):
        monkeypatch.setattr(real_db, name, getattr(fake, name))
    return fake


@pytest.fixture
def no_network(monkeypatch):
    """Any accidental outbound HTTP is a test failure, not a slow test."""
    import requests

    def boom(*a, **k):
        raise AssertionError("test attempted a real network call")

    monkeypatch.setattr(requests, "post", boom)
    monkeypatch.setattr(requests, "get", boom)


@pytest.fixture
def cfg():
    import config
    importlib.reload(config)
    return config


# --------------------------------------------------------------------
# Synthetic market data
# --------------------------------------------------------------------
def smile_curve(spot, atm_vol=0.18, skew=1.5, curvature=120.0,
                lo=-0.10, hi=0.10, step=25):
    """IV as a function of log-moneyness on a fixed strike grid.

    Same shape as the project's own test_detector.py helper, kept
    compatible so scenarios can be moved between the two suites.
    Returns [(strike, iv_in_percentage_points)].
    """
    out, k = [], round(spot * math.exp(lo) / step) * step
    while k <= spot * math.exp(hi):
        m = math.log(k / spot)
        out.append((float(k), atm_vol * (1 - skew * m + curvature * m * m) * 100))
        k += step
    return out


def make_ticks(series_id, snapshot_ts, spot, atm_vol=0.18, side="c",
               liquid=True, step=25, **kw):
    """One snapshot's worth of tick rows, in the shape db.fetch_history
    returns them (ISO strings, not datetimes)."""
    rows = []
    for strike, iv in smile_curve(spot, atm_vol=atm_vol, step=step, **kw):
        rows.append({
            "snapshot_ts": snapshot_ts.isoformat(),
            "strike": strike,
            "side": side,
            "iv": iv,
            "delta": 50.0,
            "moneyness": math.log(strike / spot),
            "oi": 500.0,
            "liquid": liquid,
        })
    return rows


def make_snapshot(snapshot_ts, spot, atm_iv):
    return {"snapshot_ts": snapshot_ts.isoformat(), "spot": spot,
            "atm_iv": atm_iv, "days_to_expiry": 30}


def build_history(n_snapshots=8, interval_min=15, spot=3400.0, atm_vol=0.18,
                  side="c", spot_path=None, vol_path=None, start=None):
    """A flat or scripted history ending at `start` (default T0).

    spot_path / vol_path, when given, are per-snapshot overrides
    (oldest first) so a test can script exactly what the market did.
    """
    start = start or T0
    ticks, snaps = [], []
    for i in range(n_snapshots):
        t = start - datetime.timedelta(minutes=interval_min * (n_snapshots - 1 - i))
        s = spot_path[i] if spot_path else spot
        v = vol_path[i] if vol_path else atm_vol
        ticks.extend(make_ticks(1, t, s, atm_vol=v, side=side))
        import smile as smile_mod
        curve = smile_curve(s, atm_vol=v)
        snaps.append(make_snapshot(t, s, smile_mod.interp(curve, s)))
    return ticks, snaps


@pytest.fixture
def chain_xlsx(tmp_path):
    """Writes a CQG-shaped options-chain workbook and returns its path.

    Factory fixture: call with overrides to produce malformed variants
    without duplicating the layout knowledge in every test.
    """
    import openpyxl

    def _make(name="options-20260819-061500.xlsx", symbol="GCE1/Q26",
              expiry="30D (Exp: Sep 25) GCE1/Q26", strikes=None,
              header_rows=4, shift_columns=0, blank_strikes=False):
        strikes = strikes if strikes is not None else [3350, 3375, 3400, 3425, 3450]
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append([symbol])
        ws.append([expiry])
        ws.append(["generated for tests"])
        for _ in range(max(0, header_rows - 3)):
            ws.append(["column headers would be here"])
        for k in strikes:
            call_delta = max(1.0, min(99.0, 50 - (k - 3400) * 0.4))
            row = [
                "09:15",        # call_ts
                500,            # call_oi
                120,            # call_vtot
                12.5,           # call_bid
                13.5,           # call_ask
                18.2,           # call_impvlt
                13.0,           # call_theov
                call_delta,     # call_delta
                0.001,          # call_gamma
                1.2,            # call_vega
                -0.4,           # call_theta
                0.01,           # call_rho
                None if blank_strikes else k,   # strike
                0.01, -0.4, 1.2, 0.001,         # put rho/theta/vega/gamma
                -(100 - call_delta),            # put_delta
                12.0, 18.4, 11.5, 12.5, 90, 400, "09:15",
            ]
            ws.append([None] * shift_columns + row)
        path = tmp_path / name
        wb.save(path)
        return str(path)

    return _make
