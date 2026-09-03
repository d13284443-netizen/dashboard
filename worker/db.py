"""
db.py — Supabase PostgREST access for the workers.

THE BUG THIS FILE EXISTS TO PREVENT
-----------------------------------
The previous detector fetched history with `order=collected_at.asc` and
`limit=50000`. Three expiries at 250 strikes is 1,500 rows per snapshot,
so the cap was crossed after ~33 snapshots. Past that, ascending order
means you receive the OLDEST 50,000 rows — so `history[-1]`, which every
check treats as "the latest reading", was silently hours stale. Both the
EMA and drift checks then compared stale data against stale data. It
never raised an error; detection just quietly stopped.

Two defences here:

  1. fetch_all() pages with Range headers until the server stops
     returning full pages, so no cap can silently truncate a result.
  2. Every history query orders DESCENDING and reverses client-side.
     If a limit ever does bind, it truncates the OLD end of the window
     (harmless — the baseline gets shorter) rather than the NEW end
     (catastrophic — "latest" becomes stale).

Supabase also enforces its own `db-max-rows` server-side, which on some
projects defaults to 1,000. Paging is the only thing that makes a query
correct regardless of what that setting happens to be.
"""
import datetime
import sys

import requests

import config

PAGE = 1000
TIMEOUT = 30


def _headers(extra=None):
    h = {
        "apikey": config.SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {config.SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def fetch_all(table, params, page_size=PAGE, hard_cap=500_000):
    """GETs every matching row, paging via Range headers.

    hard_cap is a runaway guard, not a business limit — if it ever trips
    something is wrong with the query, and it logs loudly rather than
    returning a quietly-truncated result that looks fine.
    """
    url = f"{config.SUPABASE_URL}/rest/v1/{table}"
    out, offset = [], 0
    while True:
        resp = requests.get(
            url, headers=_headers({"Range-Unit": "items",
                                   "Range": f"{offset}-{offset + page_size - 1}"}),
            params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        batch = resp.json()
        out.extend(batch)
        if len(batch) < page_size:
            return out
        offset += page_size
        if offset >= hard_cap:
            log("db", f"fetch_all({table}) hit hard cap {hard_cap} — query is too broad",
                level="error")
            return out


def insert(table, rows, on_conflict=None, upsert=False, timeout=TIMEOUT):
    """Insert rows. With upsert=True and on_conflict set, this becomes
    the idempotency mechanism: re-ingesting an already-seen file is a
    no-op (merge-duplicates) rather than a duplicate snapshot."""
    if not rows:
        return
    url = f"{config.SUPABASE_URL}/rest/v1/{table}"
    params = {}
    prefer = ["return=minimal"]
    if on_conflict:
        params["on_conflict"] = on_conflict
        prefer.append("resolution=merge-duplicates" if upsert else "resolution=ignore-duplicates")
    resp = requests.post(url, headers=_headers({"Prefer": ",".join(prefer)}),
                         params=params, json=rows, timeout=timeout)
    if resp.status_code >= 300:
        raise RuntimeError(f"insert into {table} failed ({resp.status_code}): {resp.text[:400]}")


def insert_returning(table, rows):
    url = f"{config.SUPABASE_URL}/rest/v1/{table}"
    resp = requests.post(url, headers=_headers({"Prefer": "return=representation"}),
                         json=rows, timeout=TIMEOUT)
    if resp.status_code >= 300:
        raise RuntimeError(f"insert into {table} failed ({resp.status_code}): {resp.text[:400]}")
    return resp.json()


def mark_alerted(event_ids):
    """Set alerted=true on the given spike_events rows, so a query for
    'which detections actually reached me' returns them. Best-effort:
    the alert already went out, so a failure to mark must not raise."""
    if not event_ids:
        return
    try:
        ids = ",".join(str(int(i)) for i in event_ids)
        url = f"{config.SUPABASE_URL}/rest/v1/spike_events"
        requests.patch(url, headers=_headers({"Prefer": "return=minimal"}),
                       params={"id": f"in.({ids})"},
                       json={"alerted": True}, timeout=15)
    except Exception:
        pass


def upsert_health(worker, success=False, error=None, detail=None):
    fields = {"worker": worker}
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if success:
        fields["last_success_at"] = now
    if error:
        fields["last_error"] = str(error)[:2000]
        fields["last_error_at"] = now
    if detail is not None:
        fields["detail"] = detail
    try:
        url = f"{config.SUPABASE_URL}/rest/v1/worker_health"
        requests.post(url, headers=_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
                      params={"on_conflict": "worker"}, json=[fields], timeout=10)
    except Exception:
        pass  # health reporting must never take down the worker


def log(source, message, level="info"):
    """Prints first, then best-effort writes to Supabase. Printing first
    means VPS-side logs never depend on Supabase being reachable."""
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    stream = sys.stderr if level == "error" else sys.stdout
    print(f"[{ts}][{level.upper()}][{source}] {message}", file=stream, flush=True)
    if not (config.SUPABASE_URL and config.SUPABASE_SERVICE_KEY):
        return
    try:
        requests.post(f"{config.SUPABASE_URL}/rest/v1/debug_log",
                      headers=_headers({"Prefer": "return=minimal"}),
                      json=[{"source": source, "level": level,
                             "message": str(message)[:4000]}], timeout=10)
    except Exception:
        pass


def get_or_create_series(instrument, symbol, expiry_label):
    """Resolves (instrument, symbol) to the smallint id that tick rows
    carry. Cached in-process — the set of tracked expiries changes a few
    times a week at most, so re-querying per file would be wasteful."""
    key = (instrument, symbol)
    if key in _series_cache:
        return _series_cache[key]

    rows = fetch_all("series", {"instrument": f"eq.{instrument}",
                                "symbol": f"eq.{symbol}", "select": "id"})
    if rows:
        _series_cache[key] = rows[0]["id"]
        return _series_cache[key]

    created = insert_returning("series", [{
        "instrument": instrument, "symbol": symbol,
        "expiry_date_label": expiry_label,
    }])
    _series_cache[key] = created[0]["id"]
    return _series_cache[key]


_series_cache = {}


def fetch_history(series_id, since_dt):
    """Tick history for one series since a cutoff, oldest-first.

    Ordered DESC on the wire (so any server-side cap truncates the old
    end, not the new end) and reversed here. Paged, so it cannot be
    silently truncated at all.
    """
    rows = fetch_all("iv_ticks", {
        "series_id": f"eq.{series_id}",
        "snapshot_ts": f"gte.{since_dt.isoformat()}",
        "select": "snapshot_ts,strike,side,iv,delta,moneyness,oi,liquid",
        "order": "snapshot_ts.desc",
    })
    rows.reverse()
    return rows


def fetch_snapshots(series_id, since_dt):
    rows = fetch_all("snapshots", {
        "series_id": f"eq.{series_id}",
        "snapshot_ts": f"gte.{since_dt.isoformat()}",
        "select": "snapshot_ts,spot,atm_iv,days_to_expiry",
        "order": "snapshot_ts.desc",
    })
    rows.reverse()
    return rows


def fetch_recent_events(series_id, since_dt):
    return fetch_all("spike_events", {
        "series_id": f"eq.{series_id}",
        "detected_at": f"gte.{since_dt.isoformat()}",
        "select": "strike,side,rule,moneyness,detected_at",
    })
