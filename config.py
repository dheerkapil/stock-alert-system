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

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise ValueError("Missing Telegram credentials.")

DB_FILE = os.environ.get("DB_FILE", "watchlist.db")
CONSECUTIVE_TV_FAILURES_THRESHOLD = 3
LOG_LEVEL = "INFO"