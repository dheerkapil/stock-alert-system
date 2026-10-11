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

# NSE trading holidays — update each January when NSE publishes the next year's calendar.
# Fixed-date holidays below are certain. Variable-date ones are best-effort;
# verify against NSE's official list each year.
NSE_HOLIDAYS = {
    # Fixed-date
    "2026-01-26",  # Republic Day (Mon)
    "2026-05-01",  # Maharashtra Day (Fri)
    "2026-08-15",  # Independence Day (Sat — weekend, no effect)
    "2026-10-02",  # Gandhi Jayanti (Fri)
    "2026-12-25",  # Christmas (Fri)

    # Variable-date — VERIFY ANNUALLY against NSE calendar
    "2026-02-15",  # Mahashivratri (Sun — weekend, no effect)
    "2026-03-04",  # Holi
    "2026-03-26",  # Ram Navami
    "2026-03-31",  # Mahavir Jayanti
    "2026-04-03",  # Good Friday
    "2026-04-14",  # Dr. Ambedkar Jayanti
    "2026-05-27",  # Eid-ul-Adha
    "2026-08-26",  # Janmashtami
    "2026-09-14",  # Ganesh Chaturthi
    "2026-10-20",  # Dussehra
    "2026-11-09",  # Diwali Balipratipada
    "2026-11-24",  # Guru Nanak Jayanti
}