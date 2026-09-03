"""
config.py — Environment configuration for the IV monitor workers.

All secrets come from the environment. The service-role key bypasses
RLS and must never reach a browser or a public repo.
"""
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Where the Chrome extension drops downloads on the Windows VPS.
WATCH_DIR = os.environ.get("WATCH_DIR", str(Path.home() / "Downloads"))
WATCH_PATTERN = os.environ.get("WATCH_PATTERN", "options-*.xlsx")
INSTRUMENT = os.environ.get("INSTRUMENT", "GOLD")

# Poll frequently even though files only land every 15-20 min — polling
# is nearly free locally, and it minimises the delay between a file
# appearing and an alert going out.
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "20"))

# A file must hold the same size across two consecutive polls before we
# open it. Without this, a partially-written download gets parsed and
# then deleted. The original code opened files the instant glob saw
# them, which is a race the desktop version got away with only because
# downloads were local and fast.
FILE_STABLE_CHECKS = int(os.environ.get("FILE_STABLE_CHECKS", "2"))

# ---- Detector tuning, sized for a 15-20 minute ingest cadence -------
#
# The original used ema_span=12 meaning "12 samples". On a desktop poll
# loop that was ~12 minutes; here it would silently mean 3-4 HOURS, and
# would shift again whenever the download interval drifted. A half-life
# in minutes is cadence-independent: the baseline covers the same amount
# of real time whether samples arrive every 15 minutes or every 25.
EMA_HALFLIFE_MINUTES = float(os.environ.get("EMA_HALFLIFE_MINUTES", "90"))
MIN_SAMPLES = int(os.environ.get("MIN_SAMPLES", "4"))
MIN_BASELINE_SPAN_MINUTES = float(os.environ.get("MIN_BASELINE_SPAN_MINUTES", "60"))

SPIKE_THRESHOLD_PCT = float(os.environ.get("SPIKE_THRESHOLD_PCT", "0.10"))
DRIFT_WINDOWS_HOURS = [float(x) for x in
                       os.environ.get("DRIFT_WINDOWS_HOURS", "1,3,6,24").split(",")]

# Liquidity gate. Illiquid far-OTM strikes with a zero bid and a stale
# ask produce IV that wanders freely and generates most false spikes.
MIN_OI = float(os.environ.get("MIN_OI", "10"))
MAX_SPREAD_PCT = float(os.environ.get("MAX_SPREAD_PCT", "0.50"))
MIN_ABS_DELTA = float(os.environ.get("MIN_ABS_DELTA", "5"))    # CQG delta is 0-100
MAX_ABS_DELTA = float(os.environ.get("MAX_ABS_DELTA", "95"))

# Suppression, keyed to a moneyness bucket rather than an exact strike.
# When spot moves, the "hot" strike moves with it, so exact-strike keys
# let one underlying event re-fire on 3405, then 3410, then 3415.
SUPPRESS_EMA_HOURS = float(os.environ.get("SUPPRESS_EMA_HOURS", "2"))
MONEYNESS_BUCKET = float(os.environ.get("MONEYNESS_BUCKET", "0.01"))  # ~1% bands

# Week-1 shadow mode. When true, alerts fire on the RAW rule and the
# smile-adjusted numbers are recorded alongside for comparison. Flip to
# false once the data shows the adjusted rule is behaving.
ALERT_ON_RAW = os.environ.get("ALERT_ON_RAW", "true").lower() == "true"
MAX_ALERTS_PER_CYCLE = int(os.environ.get("MAX_ALERTS_PER_CYCLE", "6"))


def require(*names):
    missing = [n for n in names if not globals().get(n)]
    if missing:
        print(f"FATAL: missing required environment variables: {', '.join(missing)}")
        sys.exit(1)
