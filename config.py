import os
import pytz

# Time
POLL_INTERVAL = 300
TIMEZONE = pytz.timezone("Asia/Kolkata")

# Market hours (IST)
START_TIME = "09:00"
STOP_TIME  = "15:45"

# TradingView
TRADINGVIEW_CHUNK_SIZE = 100
TRADINGVIEW_DELAY = 1.0
TV_CACHE_TTL_SECONDS = 60
TV_CACHE_BACKOFF_SECONDS = 300

# Telegram
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID")

# Database
DB_FILE = os.environ.get("DB_FILE", "watchlist.db")

# Worker key (web UI ↔ worker auth)
WORKER_API_KEY = os.environ.get("WORKER_API_KEY", "")

# HTTP user agent
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# Rolling window size for EOD storage (whole market)
BACKFILL_DAYS = 10

# NOTE: there is deliberately NO NSE_HOLIDAYS list here.
#
# Holidays are handled by the data instead of by a hand-maintained calendar:
# a non-trading day has no bhavcopy, the fetch returns "stale", and the EOD
# window counts only days that actually have rows (so a holiday never occupies
# a slot). The list that used to live here listed 2026-11-09 as Diwali
# Balipratipada — but that was a real trading day, and because the fetcher
# trusted the list over the data, that day's EOD was silently dropped and never
# retried. Removing the list removes that entire class of failure.
