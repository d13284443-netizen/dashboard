"""
watchdog.py — Separate process. Alerts if ingest stops producing data.

Checks worker_health.last_success_at rather than looking at the
Downloads folder, which means it catches all three failure modes with
one test: the CQG session dying so nothing downloads, the extension
downloading fine while ingest is crashed, and Supabase being
unreachable from the VPS. A folder-watching watchdog would miss the
second and third entirely.

Alerts once on the transition into staleness and once on recovery — not
every cycle — so a multi-hour CQG outage produces two messages rather
than forty.

STALE THRESHOLD: at a 15-20 minute download cadence, a 60-minute
threshold means roughly three consecutive missed cycles before anyone is
woken up. That is deliberately forgiving: one skipped download during a
CQG page reload is normal and should not page you.
"""
import datetime
import os
import time

import requests

import config
import db

STALE_MINUTES = int(os.environ.get("WATCHDOG_STALE_MINUTES", "60"))
CHECK_SECONDS = int(os.environ.get("WATCHDOG_CHECK_SECONDS", "300"))


def last_success():
    rows = db.fetch_all("worker_health",
                        {"worker": "eq.ingest", "select": "last_success_at"})
    if not rows or not rows[0].get("last_success_at"):
        return None
    return datetime.datetime.fromisoformat(
        rows[0]["last_success_at"].replace("Z", "+00:00"))


def send(msg):
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        print(f"[watchdog] would have sent: {msg}")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": config.TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
        return r.status_code < 300
    except Exception:
        return False


def default_partition_count():
    """Rows trapped in iv_ticks_default. Should always be 0. Non-zero
    means partition creation fell behind — the failure mode that can
    jam the partitioned table. Returns None if the RPC isn't reachable
    (older schema, network), which the caller treats as 'don't alert'."""
    try:
        import requests
        r = requests.post(
            f"{config.SUPABASE_URL}/rest/v1/rpc/iv_ticks_default_count",
            headers={"apikey": config.SUPABASE_SERVICE_KEY,
                     "Authorization": f"Bearer {config.SUPABASE_SERVICE_KEY}",
                     "Content-Type": "application/json"},
            json={}, timeout=10)
        if r.status_code < 300:
            return int(r.json())
    except Exception:
        pass
    return None


def _load_alerted():
    """Read the persisted stale-alert flag from worker_health, so the
    watchdog resumes its state across restarts instead of re-alerting."""
    try:
        rows = db.fetch_all("worker_health",
                            {"worker": "eq.watchdog", "select": "detail"})
        if rows and isinstance(rows[0].get("detail"), dict):
            return bool(rows[0]["detail"].get("stale_alerted", False))
    except Exception:
        pass
    return False


def _save_alerted(value):
    try:
        db.upsert_health("watchdog", detail={"stale_alerted": bool(value)})
    except Exception:
        pass


def run():
    config.require("SUPABASE_URL", "SUPABASE_SERVICE_KEY")
    # Seed the alert flag from the last persisted state, so a restart in
    # the middle of an outage does NOT re-fire the stale alert (which
    # would defeat the "two messages, not forty" promise). The flag is
    # written to worker_health.detail after every send.
    alerted = _load_alerted()
    partition_alerted = False
    threshold = datetime.timedelta(minutes=STALE_MINUTES)
    print(f"watchdog: alerting after {STALE_MINUTES} min without a successful ingest"
          f" (resuming alerted={alerted})")

    while True:
        try:
            ls = last_success()
            now = datetime.datetime.now(datetime.timezone.utc)
            stale = ls is None or (now - ls) > threshold

            if stale and not alerted:
                age = "never" if ls is None else f"{(now - ls).total_seconds()/60:.0f} min ago"
                if send(f"\u26a0\ufe0f IV monitor: ingest stalled.\n"
                        f"Last successful write: {age}.\n"
                        f"Check Chrome / CQG login on the VPS."):
                    alerted = True
                    _save_alerted(True)
                    db.log("watchdog", f"stale alert sent (last success {age})", level="error")
            elif not stale and alerted:
                if send("\u2705 IV monitor: ingest recovered, data is flowing again."):
                    alerted = False
                    _save_alerted(False)
                    db.log("watchdog", "recovery notice sent")

            # Default-partition trap: rows here mean partition creation
            # fell behind. Left unchecked this jams partition creation and
            # blocks retention — the class of failure that filled the
            # previous database. Alert once on the transition into a
            # non-empty default, clear once it drains.
            dc = default_partition_count()
            if dc is not None:
                if dc > 0 and not partition_alerted:
                    if send(f"\u26a0\ufe0f IV monitor: {dc} rows in the default partition.\n"
                            f"Partition creation has fallen behind — run "
                            f"select ensure_iv_tick_partitions(3); on the database."):
                        partition_alerted = True
                        db.log("watchdog", f"default partition non-empty ({dc} rows)", level="error")
                elif dc == 0 and partition_alerted:
                    partition_alerted = False
                    db.log("watchdog", "default partition drained")
        except Exception as e:
            db.log("watchdog", f"check failed: {e}", level="error")
        time.sleep(CHECK_SECONDS)


if __name__ == "__main__":
    run()
