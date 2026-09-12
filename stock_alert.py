import os
import time
import logging
import requests
import yfinance as yf
from datetime import datetime, date
import config

logging.basicConfig(level=config.LOG_LEVEL, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

WEBUI_URL = os.environ.get("WEBUI_URL", "https://stock-alert-ui.onrender.com")

# ------------------------------------------------------------------
#  DAY-LEVEL CACHE FOR PREVIOUS CLOSES
# ------------------------------------------------------------------
_PREV_CLOSE_CACHE = {}          # {symbol: prev_close}
_PREV_CLOSE_DATE = None         # date when cache was built

# ------------------------------------------------------------------
#  HELPERS
# ------------------------------------------------------------------
def is_market_open(now):
    start = datetime.strptime(config.START_TIME, "%H:%M").time()
    stop  = datetime.strptime(config.STOP_TIME, "%H:%M").time()
    return start <= now.time() <= stop

def is_weekday(now):
    return now.weekday() < 5

# ------------------------------------------------------------------
#  FETCH ALERTS FROM WEB UI
# ------------------------------------------------------------------
def get_active_alerts():
    try:
        resp = requests.get(f"{WEBUI_URL}/api/alerts", timeout=10)
        resp.raise_for_status()
        alerts = resp.json()
        return [a for a in alerts if a['is_active'] == 1 and a['is_triggered'] == 0]
    except Exception as e:
        logger.error(f"Failed to fetch alerts: {e}")
        return []

# ------------------------------------------------------------------
#  PRICE FETCHING (TradingView)
# ------------------------------------------------------------------
def to_tradingview_symbol(symbol):
    return symbol.upper().replace('-', '_')

def get_prices_tradingview_chunked(symbols):
    if not symbols:
        return {}
    prices = {}
    for i in range(0, len(symbols), config.TRADINGVIEW_CHUNK_SIZE):
        chunk = symbols[i:i+config.TRADINGVIEW_CHUNK_SIZE]
        tickers = [f"NSE:{to_tradingview_symbol(s)}" for s in chunk]
        payload = {"symbols": {"tickers": tickers}, "columns": ["close"]}
        try:
            resp = requests.post("https://scanner.tradingview.com/india/scan", json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            for entry in data.get('data', []):
                tv_symbol = entry['s'].split(':')[1]
                price = entry['d'][0]
                if price is not None:
                    tv_clean = tv_symbol.upper().replace('-', '_')
                    for original in chunk:
                        if to_tradingview_symbol(original) == tv_clean:
                            prices[original] = float(price)
                            break
        except Exception as e:
            logger.warning(f"TradingView error: {e}")
        time.sleep(config.TRADINGVIEW_DELAY)
    return prices

# ------------------------------------------------------------------
#  PRICE FETCHING (Yahoo Finance fallback)
# ------------------------------------------------------------------
def get_prices_yfinance(symbols):
    prices = {}
    for sym in symbols:
        try:
            ticker = yf.Ticker(f"{sym.upper()}.NS")
            data = ticker.history(period="1d", interval="1m")
            if not data.empty:
                prices[sym] = float(data['Close'].iloc[-1])
        except Exception:
            pass
    return prices

# ------------------------------------------------------------------
#  MASTER PRICE FETCHER
# ------------------------------------------------------------------
def get_prices(symbols):
    symbols = list(set(symbols))
    tv_prices = get_prices_tradingview_chunked(symbols)
    missing = [s for s in symbols if s not in tv_prices or tv_prices.get(s) is None]
    if missing and config.YAHOO_FINANCE_ENABLED:
        logger.info(f"Yahoo fallback for {len(missing)} symbols.")
        yf_prices = get_prices_yfinance(missing)
        tv_prices.update(yf_prices)
    return tv_prices

# ------------------------------------------------------------------
#  PREVIOUS CLOSE - DAY-LEVEL CACHE
# ------------------------------------------------------------------
def _invalidate_cache_if_new_day():
    """Clear the cache if the date has changed."""
    global _PREV_CLOSE_CACHE, _PREV_CLOSE_DATE
    today = date.today()
    if _PREV_CLOSE_DATE != today:
        _PREV_CLOSE_CACHE = {}
        _PREV_CLOSE_DATE = today
        logger.info(f"Previous-close cache invalidated for new day: {today}")

def batch_fetch_prev_closes(symbols):
    """
    Batch download previous closes for multiple symbols in ONE Yahoo Finance call.
    Used at 8 AM to warm up the cache.
    """
    if not symbols:
        return {}
    symbols = [s.upper() for s in symbols]
    tickers = [f"{s}.NS" for s in symbols]

    logger.info(f"Batch fetching previous closes for {len(symbols)} symbols...")
    result = {}

    try:
        data = yf.download(
            tickers=" ".join(tickers),
            period="5d",
            interval="1d",
            progress=False,
            group_by='ticker',
            threads=True,
            auto_adjust=False
        )

        for sym, ticker in zip(symbols, tickers):
            try:
                if ticker in data.columns.levels[0]:
                    df = data[ticker]
                    closes = df['Close'].dropna()
                    if len(closes) >= 2:
                        result[sym] = float(closes.iloc[-2])
                    elif len(closes) == 1:
                        result[sym] = float(closes.iloc[-1])
                    else:
                        result[sym] = None
                else:
                    result[sym] = None
            except Exception as e:
                logger.warning(f"Failed to extract prev close for {sym}: {e}")
                result[sym] = None
    except Exception as e:
        logger.error(f"Batch download failed: {e}")
        return {}

    return result

def get_prev_closes(symbols):
    """
    Return previous close for each symbol.
    - Uses day-level cache.
    - Fetches missing symbols in a single batch call.
    - Never re-fetches during the same trading day.
    """
    global _PREV_CLOSE_CACHE

    _invalidate_cache_if_new_day()

    if not symbols:
        return {}

    symbols = [s.upper() for s in symbols]
    result = {}
    missing = []

    for sym in symbols:
        if sym in _PREV_CLOSE_CACHE:
            result[sym] = _PREV_CLOSE_CACHE[sym]
        else:
            missing.append(sym)

    if missing:
        fetched = batch_fetch_prev_closes(missing)
        for sym in missing:
            val = fetched.get(sym)
            _PREV_CLOSE_CACHE[sym] = val
            result[sym] = val
        logger.info(f"Cached previous closes for {len(missing)} new symbols.")

    return result

def add_symbol_to_cache(symbol):
    """
    Fetch and cache a single symbol's previous close.
    Called when a new alert is added during the trading day.
    """
    global _PREV_CLOSE_CACHE
    _invalidate_cache_if_new_day()

    sym = symbol.upper()
    if sym in _PREV_CLOSE_CACHE:
        return _PREV_CLOSE_CACHE[sym]

    fetched = batch_fetch_prev_closes([sym])
    val = fetched.get(sym)
    _PREV_CLOSE_CACHE[sym] = val
    logger.info(f"Added {sym} to prev-close cache: {val}")
    return val

# ------------------------------------------------------------------
#  TELEGRAM
# ------------------------------------------------------------------
def send_telegram(message):
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.error("Telegram credentials missing.")
        return
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": config.TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Telegram error: {e}")

# ------------------------------------------------------------------
#  MAIN LOOP
# ------------------------------------------------------------------
def main():
    logger.info(f"🚀 Worker started. Poll interval: {config.POLL_INTERVAL}s.")
    logger.info(f"Market hours: {config.START_TIME} - {config.STOP_TIME} IST. Weekdays only.")

    while True:
        now = datetime.now(config.TIMEZONE)

        while not is_weekday(now) or not is_market_open(now):
            time.sleep(60)
            now = datetime.now(config.TIMEZONE)

        alerts = get_active_alerts()
        if not alerts:
            logger.info("No active alerts.")
            time.sleep(config.POLL_INTERVAL)
            continue

        symbols = list(set(a['symbol'] for a in alerts))
        logger.info(f"Fetching prices for {len(symbols)} symbols...")
        prices = get_prices(symbols)

        for alert in alerts:
            symbol = alert['symbol']
            cond = alert['condition']
            trigger = alert['trigger_price']
            current = prices.get(symbol)
            if current is None:
                continue

            triggered = False
            if cond == '>=' and current >= trigger:
                triggered = True
            elif cond == '<=' and current <= trigger:
                triggered = True

            if triggered:
                msg = (f"🔔 ALERT\n{symbol} {cond} {trigger}\nCurrent: {current}\n{now.strftime('%H:%M:%S')} IST")
                send_telegram(msg)
                try:
                    requests.post(f"{WEBUI_URL}/api/mark_triggered/{alert['id']}", timeout=10)
                except Exception as e:
                    logger.error(f"Failed to mark triggered: {e}")
                logger.info(f"Alert {alert['id']} triggered.")

        time.sleep(config.POLL_INTERVAL)

if __name__ == "__main__":
    main()