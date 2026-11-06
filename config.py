import os
import pytz

# ---- Time & Polling ----
POLL_INTERVAL = 300            # 5 minutes

# ---- Market Hours (IST) ----
START_TIME = "09:00"
STOP_TIME  = "15:45"

TIMEZONE = pytz.timezone("Asia/Kolkata")

# ---- Data Sources ----
TRADINGVIEW_CHUNK_SIZE = 100   # smaller batches, less likely to trigger rate limit
TRADINGVIEW_DELAY = 1.0        # seconds between chunks
YAHOO_FINANCE_ENABLED = True

# ---- Telegram ----
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID")

# ---- Database ----
DB_FILE = os.environ.get("DB_FILE", "watchlist.db")

# ---- Failure Detection ----
CONSECUTIVE_TV_FAILURES_THRESHOLD = 3

# ---- Logging ----
LOG_LEVEL = "INFO"

# ---- User-Agent ----
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# ---- Worker API Key ----
WORKER_API_KEY = os.environ.get("WORKER_API_KEY", "")

# ---- NSE Trading Holidays ----
# Fixed-date holidays only. Variable-date holidays (Holi, Diwali, Eid, etc.)
# change yearly. Update this set annually from NSE's official calendar:
# https://www.nseindia.com/resources/exchange-communication-holidays
NSE_HOLIDAYS = {
    "2026-01-26",  # Republic Day
    "2026-05-01",  # Maharashtra Day
    "2026-08-15",  # Independence Day
    "2026-10-02",  # Gandhi Jayanti
    "2026-12-25",  # Christmas
}