"""
ingest.py — Watches the extension's download folder, parses each chain
file, and writes a narrow tick row per (strike, side) plus one snapshot
metadata row and an upserted latest_chain.

Parsing is chain_loader.load_chain, copied verbatim from the working
desktop app. That column mapping is already proven against real CQG
exports and is deliberately not re-derived here.

FOUR CHANGES FROM THE PREVIOUS INGEST, EACH FIXING A REAL FAILURE
-----------------------------------------------------------------
1. IDEMPOTENT. Rows are keyed on the file's own snapshot timestamp,
   with a unique constraint behind them. The old version keyed on
   ingestion time with no constraint, so a re-downloaded file or a
   retried write created a duplicate snapshot — inflating storage and,
   worse, feeding the EMA repeated identical readings that dragged the
   baseline toward the current value and masked real moves.

2. FILE-STABILITY CHECK. A file must report the same size across
   consecutive polls before it is opened. The old version parsed the
   instant glob saw the file and deleted it afterwards regardless — on
   Windows, with Chrome writing the download, that is a live race that
   loses snapshots permanently.

3. NEVER DELETES ON FAILURE. A file is only removed after a confirmed
   successful write. Failures move it to a `failed/` subfolder instead,
   where it can be inspected or replayed. Nothing is destroyed on a path
   where we do not yet understand what went wrong.

4. LIQUIDITY IS COMPUTED AND STORED. chain_loader already works out
   both-sides-quoted; the old ingest discarded it, so the detector ran
   over dead far-OTM strikes whose IV wanders freely on stale quotes.
   Those strikes produce most false positives.
"""
import datetime
import glob
import os
import shutil
import time

import alerts
import config
import db
import smile
from chain_loader import load_chain, detect_instrument
from expiry_parser import snapshot_time_from_filename

import re

EXPIRY_LABEL_RE = re.compile(r"\s*(\d+)\s*D\s*\(Exp:\s*([^)]+)\)\s*(\S+)", re.IGNORECASE)


def parse_expiry_label(label):
    if not label:
        return None, None, None
    m = EXPIRY_LABEL_RE.match(label)
    if m:
        return int(m.group(1)), m.group(2).strip(), m.group(3).strip()
    from expiry_parser import days_to_expiry_from_label
    return days_to_expiry_from_label(label), None, None


def is_liquid(rec, side):
    """Gate applied once, at write time, so every downstream consumer
    sees the same definition. Three independent ways a quote can be
    untrustworthy, any one of which disqualifies it.

    Fails CLOSED on missing OI or delta: the docstring's promise is that
    any check can disqualify, so a strike whose OI or delta is absent
    cannot be confirmed liquid and is treated as illiquid. This suits a
    feed (like this CQG one) that reliably includes both. If a feed
    legitimately omits them for otherwise-good strikes, set
    REQUIRE_OI/REQUIRE_DELTA to false to restore lenient behaviour."""
    bid, ask = rec.get(f"{side}_bid"), rec.get(f"{side}_ask")
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return False
    mid = (bid + ask) / 2
    if mid <= 0 or (ask - bid) / mid > config.MAX_SPREAD_PCT:
        return False
    oi = rec.get(f"{side}_oi")
    if oi is None:
        if config.REQUIRE_OI:
            return False
    elif oi < config.MIN_OI:
        return False
    d = rec.get(f"{side}_delta")
    if d is None:
        if config.REQUIRE_DELTA:
            return False
    elif not (config.MIN_ABS_DELTA <= abs(d) <= config.MAX_ABS_DELTA):
        return False
    iv = rec.get(f"{side}_impvlt")
    return iv is not None and iv > 0


def stable_files(watch_dir, sizes):
    """Returns files whose size has been unchanged for FILE_STABLE_CHECKS
    consecutive polls. `sizes` is mutated to carry state between calls."""
    ready = []
    current = {}
    for path in glob.glob(os.path.join(watch_dir, config.WATCH_PATTERN)):
        if path in _locked:
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        prev_size, count = sizes.get(path, (None, 0))
        count = count + 1 if size == prev_size else 0
        current[path] = (size, count)
        if count >= config.FILE_STABLE_CHECKS - 1:
            ready.append(path)
    sizes.clear()
    sizes.update(current)
    return ready


def quarantine(path):
    """Preserve a file we could not handle, rather than deleting it."""
    try:
        failed_dir = os.path.join(os.path.dirname(path), "failed")
        os.makedirs(failed_dir, exist_ok=True)
        shutil.move(path, os.path.join(failed_dir, os.path.basename(path)))
    except OSError:
        pass


def retire(path):
    """Removes a file we have finished with.

    On Windows this is not guaranteed to succeed: Excel, antivirus, or
    Chrome's own writer can hold a lock on a file we have already read
    successfully. That must not discard a completed ingest, and it must
    not cause the same file to be reprocessed on every poll forever.

    Three fallbacks, in order of preference: delete it, move it aside,
    or failing both, remember it in-process so this run stops picking it
    up. The remembered set is lost on restart, which is fine — the
    unique constraint on (series_id, snapshot_ts) makes a repeat ingest
    a no-op rather than a duplicate.
    """
    for _ in range(3):
        try:
            os.remove(path)
            return True
        except OSError:
            time.sleep(0.5)
    try:
        done_dir = os.path.join(os.path.dirname(path), "processed")
        os.makedirs(done_dir, exist_ok=True)
        shutil.move(path, os.path.join(done_dir, os.path.basename(path)))
        return True
    except OSError:
        pass
    _locked.add(path)
    db.log("ingest", f"{os.path.basename(path)}: data saved, but the file is locked "
                     f"(is it open in Excel?) — skipping it for the rest of this run",
           level="warn")
    return False


_locked = set()

# Highest snapshot_ts written to latest_chain per series, so a replayed
# older file can't overwrite newer prices. Process-local; the schema
# trigger is the durable cross-restart guarantee, this just avoids the
# round-trip. Seeded lazily: an unknown series has no entry, so its
# first file of the run always writes (correct — nothing to be stale
# against yet that the trigger won't also catch).
_latest_chain_ts = {}


def ingest_one(path):
    filename = os.path.basename(path)
    try:
        meta, records = load_chain(path)
    except Exception as e:
        db.log("ingest", f"parse failed for {filename}: {e}", level="warn")
        quarantine(path)
        return None

    days, date_label, symbol = parse_expiry_label(meta.get("expiry"))
    symbol = symbol or meta.get("symbol")
    instrument = detect_instrument(meta.get("symbol")) or config.INSTRUMENT

    # The file's own name is the authoritative snapshot time. Falling
    # back to "now" would break idempotency, so an unparseable name is a
    # quarantine, not a guess: two ingests of the same unnamed file
    # would otherwise become two distinct snapshots.
    snap_dt = snapshot_time_from_filename(filename)
    if snap_dt is None:
        db.log("ingest", f"{filename}: no timestamp in filename — quarantined", level="warn")
        quarantine(path)
        return None
    # Interpret the naive filename time in the VPS's configured zone,
    # then convert to UTC. With WATCH_TZ=UTC (the default) this is
    # identical to the old behaviour; with a real local zone it fixes
    # the offset that would otherwise skew every snapshot_ts.
    try:
        from zoneinfo import ZoneInfo
        local_tz = ZoneInfo(config.WATCH_TZ)
    except Exception:
        local_tz = datetime.timezone.utc
    snap_ts = snap_dt.replace(tzinfo=local_tz).astimezone(datetime.timezone.utc)

    spot = smile.detect_spot(records)
    atm = smile.atm_iv(records, spot)
    series_id = db.get_or_create_series(instrument, symbol, date_label)

    ticks, chain_rows, liquid_count = [], [], 0
    for r in records:
        strike = r["strike"]
        m = smile.moneyness(strike, spot)
        for side, code in (("call", "c"), ("put", "p")):
            liquid = is_liquid(r, side)
            liquid_count += 1 if liquid else 0
            ticks.append({
                "snapshot_ts": snap_ts.isoformat(),
                "series_id": series_id,
                "strike": strike,
                "side": code,
                "iv": r.get(f"{side}_impvlt"),
                "delta": r.get(f"{side}_delta"),
                "moneyness": m,
                "oi": r.get(f"{side}_oi"),
                "liquid": liquid,
            })
        chain_rows.append({
            "series_id": series_id, "strike": strike,
            "snapshot_ts": snap_ts.isoformat(),
            **{f"call_{k}": r.get(f"call_{v}") for k, v in
               (("bid", "bid"), ("ask", "ask"), ("iv", "impvlt"), ("delta", "delta"),
                ("gamma", "gamma"), ("vega", "vega"), ("theta", "theta"), ("oi", "oi"))},
            **{f"put_{k}": r.get(f"put_{v}") for k, v in
               (("bid", "bid"), ("ask", "ask"), ("iv", "impvlt"), ("delta", "delta"),
                ("gamma", "gamma"), ("vega", "vega"), ("theta", "theta"), ("oi", "oi"))},
        })

    # Order matters: snapshot metadata first, so the smile-adjustment
    # code can never find ticks without the spot they need to be
    # interpreted against.
    db.insert("snapshots", [{
        "series_id": series_id, "snapshot_ts": snap_ts.isoformat(),
        "days_to_expiry": days, "spot": spot, "atm_iv": atm,
        "strike_count": len(records), "liquid_strike_count": liquid_count,
        "source_file": filename,
    }], on_conflict="series_id,snapshot_ts", upsert=False)

    db.insert("iv_ticks", ticks,
              on_conflict="snapshot_ts,series_id,strike,side", upsert=False)

    # latest_chain must only ever move FORWARD in time. Replaying a
    # quarantined/older file (which the README invites) would otherwise
    # overwrite current prices with stale ones, and the payoff builder
    # and scanner both read this table. Two layers guard it: a BEFORE
    # UPDATE trigger in the schema (protects against any writer), and
    # this process-local check (skips the write entirely so we don't
    # even round-trip a stale file). The database trigger is the durable
    # guarantee; this is the cheap fast path.
    prev = _latest_chain_ts.get(series_id)
    if prev is not None and snap_ts <= prev:
        db.log("ingest", f"{filename}: snapshot {snap_ts:%H:%M} is not newer than "
                         f"{prev:%H:%M} already in latest_chain — skipping chain overwrite",
               level="warn")
    else:
        db.insert("latest_chain", chain_rows,
                  on_conflict="series_id,strike", upsert=True)
        _latest_chain_ts[series_id] = snap_ts

    retire(path)
    return {"series_id": series_id, "symbol": symbol, "snapshot_ts": snap_ts,
            "strikes": len(records), "liquid": liquid_count, "spot": spot,
            "atm_iv": atm, "days": days}


def run():
    config.require("SUPABASE_URL", "SUPABASE_SERVICE_KEY")
    db.log("ingest", f"watching {config.WATCH_DIR} for {config.WATCH_PATTERN}")
    sizes = {}
    # Imported here rather than at module scope so ingest can be run
    # standalone for testing without the detector's dependencies.
    from detector import run_for_series

    while True:
        try:
            for path in stable_files(config.WATCH_DIR, sizes):
                result = ingest_one(path)
                if not result:
                    continue
                db.log("ingest", f"{result['symbol']} @ {result['snapshot_ts']:%H:%M} — "
                                 f"{result['strikes']} strikes, {result['liquid']} liquid, "
                                 f"spot {result['spot']}")
                db.upsert_health("ingest", success=True, detail={
                    "symbol": result["symbol"], "strikes": result["strikes"],
                    "liquid": result["liquid"],
                })
                # Detection runs on arrival, not on a timer. At a 15-20
                # minute cadence a 30-second timer would re-run the same
                # check ~40 times per new datapoint, burning API quota to
                # recompute an unchanged answer.
                try:
                    events = run_for_series(result["series_id"])
                    if events:
                        alerts.notify(result["symbol"], events)
                except Exception as e:
                    db.log("detector", f"check failed after ingest: {e}", level="error")
        except Exception as e:
            db.log("ingest", f"loop iteration failed: {e}", level="error")
            db.upsert_health("ingest", error=e)
        time.sleep(config.POLL_SECONDS)


if __name__ == "__main__":
    run()
