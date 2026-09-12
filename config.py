import os
import pytz

# ---- Time & Polling ----
POLL_INTERVAL = 300            # 5 minutes

# ---- Market Hours (IST) ----
START_TIME = "09:00"
STOP_TIME  = "15:45"

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

# ---- User-Agent ----
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# ---- Worker API Key (for stock_alert.py to authenticate to web_ui.py) ----
WORKER_API_KEY = os.environ.get("WORKER_API_KEY", "")