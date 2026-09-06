import os
import time
import logging
import requests
import yfinance as yf
from datetime import datetime
import config

logging.basicConfig(level=config.LOG_LEVEL, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

WEBUI_URL = os.environ.get("WEBUI_URL", "https://stock-alert-ui.onrender.com")

# ------------------------------------------------------------------
#  HELPERS
# ------------------------------------------------------------------
def is_market_open(now):
    start = datetime.strptime(config.START_TIME, "%H:%M").time()
    stop  = datetime.strptime(config.STOP_TIME, "%H:%M").time()
    return start <= now.time() <= stop

def is_weekday(now):
    # Monday = 0, Sunday = 6
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
#  PRICE FETCHING
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
#  MAIN LOOP (with weekday check)
# ------------------------------------------------------------------
def main():
    logger.info(f"🚀 Worker started. Poll interval: {config.POLL_INTERVAL}s.")
    logger.info(f"Market hours: {config.START_TIME} - {config.STOP_TIME} IST. Weekdays only.")

    while True:
        now = datetime.now(config.TIMEZONE)

        # Wait until it is a weekday AND market is open
        while not is_weekday(now) or not is_market_open(now):
            time.sleep(60)  # check every minute
            now = datetime.now(config.TIMEZONE)

        # If we reach here, it's a weekday and market is open
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