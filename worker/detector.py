"""
detector.py — IV spike detection.

Two independent mechanisms, kept from the original because the pairing
is sound: an EMA check that catches sharp jumps against a smoothed
baseline, and point-to-point drift checks over several windows that
catch slow moves an EMA would absorb.

WHAT CHANGED, AND WHY
---------------------
TIME-WEIGHTED EMA. The original used a sample-count span (`span=12`).
On the desktop poll loop that meant roughly 12 minutes; at a 15-20
minute ingest cadence the identical code means a 3-4 HOUR baseline, and
it shifts again whenever the download interval drifts or a cycle is
missed. A half-life expressed in minutes covers the same span of real
time regardless of how many samples happen to land in it, which also
makes the detector robust to the irregular cadence a browser-driven
download inevitably has.

BASELINE EXCLUDES THE LATEST POINT. Kept from the original and worth
stating: the reading being judged must not have helped form the
baseline it is judged against, or every spike partially hides itself.

SUPPRESSION BY MONEYNESS BUCKET. The original keyed suppression on the
exact strike, so as spot moved the same underlying event re-fired on
3405, then 3410, then 3415 — each a fresh key that passed suppression
cleanly. Bucketing by ~1% moneyness bands means a drifting hot strike
stays inside one suppression key.

LIQUIDITY GATE FIRST. Strikes with no real two-sided market are skipped
before any detection runs. Their IV moves on stale quotes, not on
information.

EVERY EVENT CARRIES ITS SMILE-ADJUSTED COUNTERPART. See smile.py. In
shadow mode the raw rule still decides what alerts, while the adjusted
numbers accumulate alongside — so the decision to switch over is made
from a week of labelled evidence rather than from a guess.
"""
import datetime
from collections import defaultdict

import config
import db
import smile


def time_weighted_ema(points, halflife_minutes):
    """points: [(datetime, value)] oldest first, INCLUDING the latest.
    Returns the EMA of everything except the final point, or None."""
    if len(points) < 2:
        return None
    baseline = points[:-1]
    ema = baseline[0][1]
    for i in range(1, len(baseline)):
        dt_min = (baseline[i][0] - baseline[i - 1][0]).total_seconds() / 60.0
        if dt_min <= 0:
            continue
        alpha = 1.0 - 0.5 ** (dt_min / halflife_minutes)
        ema = alpha * baseline[i][1] + (1 - alpha) * ema
    return ema


def _parse_ts(s):
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def _bucket(m):
    if m is None:
        return "na"
    return str(int(round(m / config.MONEYNESS_BUCKET)))


def _severity(adj, raw):
    """Adjusted change drives severity when it can be computed, because
    it is the number that reflects an actual surface move. Falls back to
    raw when the strike's equivalent position fell outside the baseline
    chain."""
    v = abs(adj) if adj is not None else abs(raw)
    if v >= 3 * config.SPIKE_THRESHOLD_PCT:
        return "high"
    if v >= 1.5 * config.SPIKE_THRESHOLD_PCT:
        return "warn"
    return "info"


def run_for_series(series_id):
    """One detection pass for one expiry. Called right after that
    expiry's file is ingested."""
    now = datetime.datetime.now(datetime.timezone.utc)
    lookback = max(max(config.DRIFT_WINDOWS_HOURS), 6) + 2
    since = now - datetime.timedelta(hours=lookback)

    ticks = db.fetch_history(series_id, since)
    if not ticks:
        return []
    snaps = {_parse_ts(s["snapshot_ts"]): s for s in db.fetch_snapshots(series_id, since)}
    if not snaps:
        return []

    # Group into per-snapshot slices (for smiles) and per-strike series
    # (for the checks). One pass, both structures.
    by_snapshot = defaultdict(list)
    by_key = defaultdict(list)
    for t in ticks:
        ts = _parse_ts(t["snapshot_ts"])
        t["_ts"] = ts
        by_snapshot[ts].append(t)
        by_key[(t["strike"], t["side"])].append(t)

    latest_ts = max(by_snapshot)
    latest_snap = snaps.get(latest_ts)
    if not latest_snap or not latest_snap.get("spot"):
        db.log("detector", f"series {series_id}: latest snapshot has no spot — skipping",
               level="warn")
        return []
    spot_now = latest_snap["spot"]
    atm_now = latest_snap.get("atm_iv")

    # Precompute one smile per (snapshot, side); reused across every
    # strike that references that snapshot as a baseline.
    smiles = {(ts, side): smile.build_smile(rows, side)
              for ts, rows in by_snapshot.items() for side in ("c", "p")}

    suppressed = _suppression_keys(series_id, now)
    events = []

    for (strike, side), series in by_key.items():
        series.sort(key=lambda t: t["_ts"])
        latest = series[-1]
        if latest["_ts"] != latest_ts or not latest.get("liquid") or not latest.get("iv"):
            continue

        usable = [t for t in series if t.get("liquid") and t.get("iv") and t["iv"] > 0]
        if len(usable) < config.MIN_SAMPLES:
            continue
        span_min = (usable[-1]["_ts"] - usable[0]["_ts"]).total_seconds() / 60.0
        if span_min < config.MIN_BASELINE_SPAN_MINUTES:
            continue

        iv_now = latest["iv"]
        points = [(t["_ts"], t["iv"]) for t in usable]

        candidates = []
        ema = time_weighted_ema(points, config.EMA_HALFLIFE_MINUTES)
        if ema and ema > 0:
            raw = (iv_now - ema) / ema
            if raw > config.SPIKE_THRESHOLD_PCT:
                # The EMA baseline is a weighted blend across time and so
                # has no single snapshot to take a smile from. The
                # half-life point is the closest honest stand-in for
                # "when this baseline is centred".
                ref_ts = _nearest_ts(
                    by_snapshot,
                    latest_ts - datetime.timedelta(minutes=config.EMA_HALFLIFE_MINUTES))
                candidates.append(("ema", ema, raw, ref_ts))

        for w in config.DRIFT_WINDOWS_HOURS:
            target = latest_ts - datetime.timedelta(hours=w)
            prior = [t for t in usable if t["_ts"] <= target]
            if not prior:
                continue
            base = prior[-1]
            if not base["iv"] or base["iv"] <= 0:
                continue
            raw = (iv_now - base["iv"]) / base["iv"]
            if raw > config.SPIKE_THRESHOLD_PCT:
                candidates.append((f"drift_{w:g}h", base["iv"], raw, base["_ts"]))

        for rule, baseline_iv, raw, ref_ts in candidates:
            key = (side, rule, _bucket(latest.get("moneyness")))
            if key in suppressed:
                continue

            adj = spot_move = atm_change = skew_then = None
            base_snap = snaps.get(ref_ts)
            if base_snap and base_snap.get("spot"):
                adj, _ = smile.adjusted_change(
                    iv_now, smiles.get((ref_ts, side), []),
                    base_snap["spot"], strike, spot_now)
                spot_move = (spot_now - base_snap["spot"]) / base_snap["spot"]
                if base_snap.get("atm_iv") and atm_now:
                    atm_change = (atm_now - base_snap["atm_iv"]) / base_snap["atm_iv"]
                    skew_then = baseline_iv - base_snap["atm_iv"]

            skew_now = (iv_now - atm_now) if atm_now else None
            would_suppress = adj is not None and adj <= config.SPIKE_THRESHOLD_PCT

            events.append({
                "series_id": series_id,
                "snapshot_ts": latest_ts.isoformat(),
                "strike": strike, "side": side, "rule": rule,
                "latest_iv": iv_now, "baseline_iv": baseline_iv,
                "raw_pct_change": raw, "adj_pct_change": adj,
                "spot_move_pct": spot_move, "atm_iv_change_pct": atm_change,
                "skew_now": skew_now,
                "skew_change": (skew_now - skew_then)
                               if (skew_now is not None and skew_then is not None) else None,
                "moneyness": latest.get("moneyness"), "delta": latest.get("delta"),
                "severity": _severity(adj, raw),
                "would_suppress": would_suppress,
            })
            suppressed.add(key)

    if events:
        db.insert("spike_events", events)
    db.upsert_health("detector", success=True, detail={
        "series_id": series_id, "events": len(events),
        "suppressed_by_smile": sum(1 for e in events if e["would_suppress"]),
    })
    return events


def _nearest_ts(by_snapshot, target):
    return min(by_snapshot, key=lambda ts: abs((ts - target).total_seconds()))


def _suppression_keys(series_id, now):
    """A rule does not re-fire for its own window length — a 6-hour
    drift signal is meaningfully the same signal for 6 hours. The EMA
    rule gets a shorter fixed window since it is meant to be responsive.
    """
    longest = max(max(config.DRIFT_WINDOWS_HOURS), config.SUPPRESS_EMA_HOURS)
    rows = db.fetch_recent_events(series_id, now - datetime.timedelta(hours=longest))
    keys = set()
    for r in rows:
        rule = r["rule"]
        # A rule string from an older deploy, or a hand-inserted row, can
        # be anything. Parse defensively: an unrecognised rule is skipped
        # (it simply won't suppress) rather than raising ValueError and
        # aborting the entire detection pass for this series.
        if rule == "ema":
            window = config.SUPPRESS_EMA_HOURS
        else:
            try:
                window = float(rule.replace("drift_", "").replace("h", ""))
            except (ValueError, AttributeError):
                continue
        if _parse_ts(r["detected_at"]) >= now - datetime.timedelta(hours=window):
            keys.add((r["side"], rule, _bucket(r.get("moneyness"))))
    return keys
