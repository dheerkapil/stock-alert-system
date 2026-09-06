import os
import pytz

POLL_INTERVAL = 60
START_TIME = "09:15"
STOP_TIME = "15:30"
TIMEZONE = pytz.timezone("Asia/Kolkata")

TRADINGVIEW_CHUNK_SIZE = 150
TRADINGVIEW_DELAY = 0.5
YAHOO_FINANCE_ENABLED = True

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

DB_FILE = os.environ.get("DB_FILE", "watchlist.db")
CONSECUTIVE_TV_FAILURES_THRESHOLD = 3
LOG_LEVEL = "INFO"

# Required for web_ui.py to fetch NSE symbols
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"