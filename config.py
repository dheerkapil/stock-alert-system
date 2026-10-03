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

# NSE trading holidays (fixed-date only; update variable-date ones annually)
NSE_HOLIDAYS = {
    "2026-01-26",  # Republic Day
    "2026-05-01",  # Maharashtra Day
    "2026-08-15",  # Independence Day
    "2026-10-02",  # Gandhi Jayanti
    "2026-12-25",  # Christmas
}