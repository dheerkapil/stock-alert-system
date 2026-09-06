import os
import pytz

# ---- Time & Polling ----
POLL_INTERVAL = 60

# 🔥 TEMPORARY: Run 24/7 for testing. Change back to "09:15" and "15:30" later.
START_TIME = "00:00"
STOP_TIME  = "23:59"

TIMEZONE = pytz.timezone("Asia/Kolkata")

# ---- Data Sources ----
TRADINGVIEW_CHUNK_SIZE = 150
TRADINGVIEW_DELAY = 0.5
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

# ---- User‑Agent (required for NSE symbol fetch) ----
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"