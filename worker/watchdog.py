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


def run():
    config.require("SUPABASE_URL", "SUPABASE_SERVICE_KEY")
    alerted = False
    threshold = datetime.timedelta(minutes=STALE_MINUTES)
    print(f"watchdog: alerting after {STALE_MINUTES} min without a successful ingest")

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
                    db.log("watchdog", f"stale alert sent (last success {age})", level="error")
            elif not stale and alerted:
                if send("\u2705 IV monitor: ingest recovered, data is flowing again."):
                    alerted = False
                    db.log("watchdog", "recovery notice sent")
        except Exception as e:
            db.log("watchdog", f"check failed: {e}", level="error")
        time.sleep(CHECK_SECONDS)


if __name__ == "__main__":
    run()
