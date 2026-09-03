"""
chain_loader.py — Reads a CQG options-chain XLSX export into a list of
per-strike records. Same COL mapping and parsing logic already verified
against a real CQG export in cqg_options_analyser.py — reused here as-is
rather than re-derived, to avoid introducing a fresh parsing bug.
"""
from pathlib import Path

COL = {
    "call_ts": 0, "call_oi": 1, "call_vtot": 2, "call_bid": 3, "call_ask": 4,
    "call_impvlt": 5, "call_theov": 6, "call_delta": 7, "call_gamma": 8,
    "call_vega": 9, "call_theta": 10, "call_rho": 11,
    "strike": 12,
    "put_rho": 13, "put_theta": 14, "put_vega": 15, "put_gamma": 16,
    "put_delta": 17, "put_theov": 18, "put_impvlt": 19,
    "put_bid": 20, "put_ask": 21, "put_vtot": 22, "put_oi": 23, "put_ts": 24,
}

# Nasdaq/S&P instrument detection (same roots verified earlier).
# "strike_round": the "nice round number" step traders actually think in
# for this instrument's strikes — used ONLY by the scanner's optional
# "round strikes" filter (chosen by the user), never to alter what's
# actually in the chain. Silver genuinely trades in mixed $0.20/$0.25
# steps depending on price level, so it gets a list; everything else is
# a single number.
INSTRUMENT_PROFILES = {
    "GOLD":   {"roots": ["GC"], "multiplier": 100, "strike_round": 25},
    "SILVER": {"roots": ["SI"], "multiplier": 5000, "strike_round": [0.20, 0.25]},
    "NASDAQ": {"roots": ["ENQ", "MNQ"], "multiplier": 20, "strike_round": 50},
    "SP500":  {"roots": ["EP", "MES"], "multiplier": 50, "strike_round": 25},
}


def detect_instrument(symbol):
    if not symbol:
        return None
    all_roots = [(name, root) for name, profile in INSTRUMENT_PROFILES.items()
                 for root in profile["roots"]]
    for name, root in sorted(all_roots, key=lambda x: len(x[1]), reverse=True):
        if symbol.upper().startswith(root):
            return name
    return None


def to_f(v):
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def mid(bid, ask):
    b, a = to_f(bid), to_f(ask)
    if b is None and a is None:
        return None
    if b is None:
        return a
    if a is None:
        return b
    return (b + a) / 2


def both_sided(bid, ask):
    return to_f(bid) is not None and to_f(ask) is not None


def load_chain(path):
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if len(rows) < 5:
        raise ValueError("File too short — expected header rows + strike data")

    meta = {
        "symbol": str(rows[0][0]) if rows[0][0] else "?",
        "expiry": str(rows[1][0]) if rows[1][0] else "?",
        "file": Path(path).name,
    }

    records = []
    for row in rows[4:]:
        if row is None or COL["strike"] >= len(row):
            continue
        strike = to_f(row[COL["strike"]])
        if strike is None:
            continue
        r = {"strike": strike}
        for fld, col in COL.items():
            if fld == "strike":
                continue
            r[fld] = to_f(row[col]) if col < len(row) else None

        cb, ca = r.get("call_bid"), r.get("call_ask")
        pb, pa = r.get("put_bid"), r.get("put_ask")
        r["call_prem"] = mid(cb, ca)
        r["put_prem"] = mid(pb, pa)
        r["call_liquid"] = both_sided(cb, ca)
        r["put_liquid"] = both_sided(pb, pa)
        records.append(r)

    if not records:
        raise ValueError("No strike rows parsed — check the column mapping still matches the export.")

    records.sort(key=lambda x: x["strike"])
    return meta, records


def detect_spot(records):
    best, bd = None, 9999
    for r in records:
        d = r.get("call_delta")
        if d is None:
            continue
        diff = abs(d - 50)
        if diff < bd:
            bd, best = diff, r["strike"]
    return best