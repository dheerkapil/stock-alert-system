import os
import time
import logging
import requests
import yfinance as yf
import pandas as pd
from datetime import datetime
import config

logging.basicConfig(level=config.LOG_LEVEL, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

WEBUI_URL = os.environ.get("WEBUI_URL", "https://stock-alert-ui.onrender.com")

# ------------------------------------------------------------------
#  HEARTBEAT STATE
# ------------------------------------------------------------------
_last_heartbeat_hour = None

# ------------------------------------------------------------------
#  HELPERS
# ------------------------------------------------------------------
def api_headers():
    h = {}
    if config.WORKER_API_KEY:
        h['X-API-Key'] = config.WORKER_API_KEY
    return h

def is_market_open(now):
    start = datetime.strptime(config.START_TIME, "%H:%M").time()
    stop  = datetime.strptime(config.STOP_TIME, "%H:%M").time()
    return start <= now.time() <= stop

def is_weekday(now):
    return now.weekday() < 5

def _extract_date_ist(idx):
    try:
        if hasattr(idx, 'tzinfo') and idx.tzinfo is not None:
            return idx.astimezone(config.TIMEZONE).date()
        elif hasattr(idx, 'date'):
            return idx.date()
        return idx
    except Exception:
        return None

# ------------------------------------------------------------------
#  FETCH ACTIVE ALERTS FROM WEB UI
# ------------------------------------------------------------------
def get_active_alerts():
    try:
        resp = requests.get(f"{WEBUI_URL}/api/alerts", headers=api_headers(), timeout=10)
        resp.raise_for_status()
        alerts = resp.json()
        return [a for a in alerts if a['is_active'] == 1 and a['is_triggered'] == 0]
    except Exception as e:
        logger.error(f"Failed to fetch alerts: {e}")
        return []

# ------------------------------------------------------------------
#  TRADINGVIEW — live price + volume metrics
# ------------------------------------------------------------------
def to_tradingview_symbol(symbol):
    return symbol.upper().replace('-', '_')

def get_prices_with_volume(symbols):
    """
    Fetch live price, volume, 10-day average volume, and relative volume
    from TradingView's scanner in batched calls.
    Returns {symbol: {price, volume, avg_vol_10d, rvol}}.
    """
    symbols = list(set(symbols))
    if not symbols:
        return {}

    result = {}
    for i in range(0, len(symbols), config.TRADINGVIEW_CHUNK_SIZE):
        chunk = symbols[i:i + config.TRADINGVIEW_CHUNK_SIZE]
        tickers = [f"NSE:{to_tradingview_symbol(s)}" for s in chunk]
        payload = {
            "symbols": {"tickers": tickers},
            "columns": ["close", "volume", "average_volume_10d_calc", "relative_volume_10d_calc"]
        }
        try:
            resp = requests.post(
                "https://scanner.tradingview.com/india/scan",
                json=payload, timeout=15
            )
            resp.raise_for_status()
            for entry in resp.json().get('data', []):
                tv_symbol = entry['s'].split(':')[1]
                vals = entry['d']
                price       = vals[0] if len(vals) > 0 else None
                volume      = vals[1] if len(vals) > 1 else None
                avg_vol_10d = vals[2] if len(vals) > 2 else None
                rvol        = vals[3] if len(vals) > 3 else None
                tv_clean = tv_symbol.upper().replace('-', '_')
                for original in chunk:
                    if to_tradingview_symbol(original) == tv_clean:
                        result[original] = {
                            "price":       float(price)       if price       is not None else None,
                            "volume":      float(volume)      if volume      is not None else None,
                            "avg_vol_10d": float(avg_vol_10d) if avg_vol_10d is not None else None,
                            "rvol":        float(rvol)        if rvol        is not None else None,
                        }
                        break
        except Exception as e:
            logger.warning(f"TradingView error: {e}")
        time.sleep(config.TRADINGVIEW_DELAY)
    return result

# ------------------------------------------------------------------
#  YAHOO FALLBACK — live price only
# ------------------------------------------------------------------
def get_prices_yfinance(symbols):
    """Per-symbol fallback used only if TradingView returns nothing."""
    prices = {}
    for sym in symbols:
        try:
            df = yf.Ticker(f"{sym.upper()}.NS").history(period="1d", interval="1m")
            if not df.empty:
                prices[sym] = {
                    "price": float(df['Close'].iloc[-1]),
                    "volume": None, "avg_vol_10d": None, "rvol": None,
                }
        except Exception:
            pass
    return prices

def get_prices(symbols):
    """Return {symbol: price}. TradingView primary, Yahoo fallback."""
    pv = get_prices_with_volume(symbols)
    missing = [s for s in symbols if s not in pv or pv[s].get("price") is None]
    if missing and config.YAHOO_FINANCE_ENABLED:
        logger.info(f"Yahoo fallback for {len(missing)} symbols.")
        pv.update(get_prices_yfinance(missing))
    return {s: v.get("price") for s, v in pv.items()}

# ------------------------------------------------------------------
#  YAHOO — daily bars (EOD snapshot + startup backfill + new-symbol backfill)
# ------------------------------------------------------------------
def batch_fetch_daily_bars(symbols, days=5, include_today=False):
    """
    ONE Yahoo call for all symbols. Returns a list of tuples:
      (symbol, trade_date_str, open, high, low, close, volume)
    Excludes today's incomplete bar unless include_today=True.
    """
    if not symbols:
        return []
    symbols = [s.upper() for s in symbols]
    tickers = [f"{s}.NS" for s in symbols]

    logger.info(f"Batch fetching {days}d bars for {len(symbols)} symbols...")

    try:
        data = yf.download(
            tickers=" ".join(tickers),
            period=f"{days + 2}d", interval="1d",
            progress=False, group_by='ticker',
            threads=True, auto_adjust=False
        )
    except Exception as e:
        logger.error(f"Batch download failed: {e}")
        return []

    today_ist = datetime.now(config.TIMEZONE).date()
    rows = []

    for sym, ticker in zip(symbols, tickers):
        try:
            if len(symbols) == 1:
                df = data
            elif hasattr(data.columns, 'levels') and ticker in data.columns.levels[0]:
                df = data[ticker]
            else:
                continue

            if df is None or df.empty or 'Close' not in df.columns:
                continue

            for i in range(len(df)):
                bar_date = _extract_date_ist(df.index[i])
                if bar_date is None:
                    continue
                if not include_today and bar_date >= today_ist:
                    continue

                def _fv(col):
                    if col not in df.columns:
                        return None
                    v = df[col].iloc[i]
                    return None if pd.isna(v) else float(v)

                rows.append((
                    sym,
                    bar_date.strftime('%Y-%m-%d'),
                    _fv('Open'), _fv('High'), _fv('Low'), _fv('Close'), _fv('Volume')
                ))
        except Exception as e:
            logger.warning(f"Extract failed for {sym}: {e}")

    return rows

# ------------------------------------------------------------------
#  TELEGRAM (retry)
# ------------------------------------------------------------------
def send_telegram(message, retries=3):
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.error("Telegram credentials missing.")
        return False

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": config.TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}

    for attempt in range(1, retries + 1):
        try:
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code == 200:
                return True
            logger.warning(f"Telegram returned {r.status_code} (attempt {attempt}/{retries})")
        except Exception as e:
            logger.warning(f"Telegram error (attempt {attempt}/{retries}): {e}")
        if attempt < retries:
            time.sleep(2)

    logger.error(f"Telegram failed after {retries} attempts: {message[:80]}")
    return False

# ------------------------------------------------------------------
#  MAIN LOOP — supervised, with hourly heartbeat
# ------------------------------------------------------------------
def main():
    global _last_heartbeat_hour

    logger.info(f"🚀 Worker started. Poll interval: {config.POLL_INTERVAL}s.")
    logger.info(f"Market hours: {config.START_TIME} - {config.STOP_TIME} IST. Weekdays only.")

    while True:
        try:
            now = datetime.now(config.TIMEZONE)

            while not is_weekday(now) or not is_market_open(now):
                time.sleep(60)
                now = datetime.now(config.TIMEZONE)

            alerts = get_active_alerts()

            if now.hour != _last_heartbeat_hour:
                send_telegram(
                    f"💓 Alive — {now.strftime('%H:%M')} IST\n"
                    f"Active alerts: {len(alerts)}"
                )
                _last_heartbeat_hour = now.hour

            if not alerts:
                logger.info("No active alerts.")
                time.sleep(config.POLL_INTERVAL)
                continue

            symbols = list(set(a['symbol'] for a in alerts))
            logger.info(f"Fetching prices for {len(symbols)} symbols...")
            prices = get_prices(symbols)

            for alert in alerts:
                current = prices.get(alert['symbol'])
                if current is None:
                    continue

                triggered = (
                    (alert['condition'] == '>' and current > alert['trigger_price']) or
                    (alert['condition'] == '<' and current < alert['trigger_price'])
                )

                if triggered:
                    send_telegram(
                        f"🔔 ALERT\n{alert['symbol']} {alert['condition']} {alert['trigger_price']}\n"
                        f"Current: {current}\n{now.strftime('%H:%M:%S')} IST"
                    )
                    try:
                        requests.post(
                            f"{WEBUI_URL}/api/mark_triggered/{alert['id']}",
                            headers=api_headers(), timeout=10
                        )
                    except Exception as e:
                        logger.error(f"Failed to mark triggered: {e}")
                    logger.info(f"Alert {alert['id']} triggered.")

            time.sleep(config.POLL_INTERVAL)

        except Exception as e:
            logger.exception(f"Worker error: {e}. Restarting in 30s.")
            time.sleep(30)

if __name__ == "__main__":
    main()