"""
expiry_parser.py — Extracts "days to expiry" from CQG's expiry label
string (e.g. "0D (Exp: Jul 22) GCE34/N26" -> 0 days). This is the only
reliable source of expiry timing in the export — CQG doesn't give an
explicit expiry timestamp field, just this human-readable label with a
leading "<N>D" day-count.

If the label doesn't match this format, days_to_expiry() returns None
rather than guessing, and callers must handle that (e.g. by asking the
person to enter days-to-expiry manually) — a wrong silent guess here
would corrupt every Black-Scholes projection downstream.
"""
import re
import datetime


def snapshot_time_from_filename(filename):
    """CQG exports are named 'options-YYYYMMDD-HHMMSS.xlsx' — this is the
    most reliable source of 'when was this chain snapshotted', more
    trustworthy than file-modified-time (which changes on copy/download).
    Returns a naive datetime (no timezone attached — caller must localize
    it, since CQG doesn't encode timezone in the filename) or None if the
    filename doesn't match this pattern."""
    m = re.search(r"options-(\d{8})-(\d{6})", filename)
    if not m:
        return None
    date_part, time_part = m.groups()
    try:
        return datetime.datetime.strptime(date_part + time_part, "%Y%m%d%H%M%S")
    except ValueError:
        return None


def days_to_expiry_from_label(label):
    """Returns an int (days to expiry) parsed from the leading '<N>D' in
    the label, or None if the label doesn't match the expected pattern."""
    if not label:
        return None
    m = re.match(r"\s*(\d+)\s*D\b", label, re.IGNORECASE)
    if not m:
        return None
    return int(m.group(1))


def years_to_expiry(days, snapshot_hour_utc=None, expiry_hour_utc=None):
    """Converts a whole-day count to a fractional year, optionally
    refining with hour-of-day info if the caller has it (e.g. "0 days"
    could mean anywhere from a few minutes to ~24 hours left — if you know
    the file's snapshot time and the contract's actual settlement/expiry
    time, pass them here for a more accurate fraction-of-a-day).

    Without hour info, "0D" is treated as a small nonzero fraction (6
    hours) rather than exactly 0, since exactly 0 makes every option's
    time value collapse to nothing, which is very likely wrong on
    same-day-expiry snapshots taken hours before actual settlement. This
    default is explicitly a rough approximation — the UI must surface
    that a same-day file's projections are approximate unless the person
    supplies the actual expiry time.
    """
    if days is None:
        return None
    if snapshot_hour_utc is not None and expiry_hour_utc is not None:
        # Fractional day remaining today, plus whole days beyond that.
        hours_left_today = max(0.0, expiry_hour_utc - snapshot_hour_utc)
        whole_days_beyond = max(0, days - 1) if days > 0 else 0
        total_days = whole_days_beyond + hours_left_today / 24.0
        return total_days / 365.0
    if days == 0:
        return (6.0 / 24.0) / 365.0  # rough same-day default: 6 hours left
    return days / 365.0