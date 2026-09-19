import os
import time
import logging
import requests
import yfinance as yf
import pandas as pd
from datetime import datetime, date
import config

logging.basicConfig(level=config.LOG_LEVEL, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

WEBUI_URL = os.environ.get("WEBUI_URL", "https://stock-alert-ui.onrender.com")

# ------------------------------------------------------------------
#  DAY-LEVEL CACHES
# ------------------------------------------------------------------
_PREV_CLOSE_CACHE = {}
_PREV_CLOSE_DATE = None

_AVG_VOLUME_CACHE = {}
_AVG_VOLUME_DATE = None

# ------------------------------------------------------------------
#  HEARTBEAT STATE
# ------------------------------------------------------------------
_last_heartbeat_hour = None

# ------------------------------------------------------------------
#  AUTH HEADER FOR WORKER ROUTES
# ------------------------------------------------------------------
def api_headers():
    headers = {}
    if config.WORKER_API_KEY:
        headers['X-API-Key'] = config.WORKER_API_KEY
    return headers

# ------------------------------------------------------------------
#  HELPERS
# ------------------------------------------------------------------
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
        else:
            return idx
    except Exception:
        return None

# ------------------------------------------------------------------
#  FETCH ALERTS FROM WEB UI
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
#  PRICE + VOLUME FETCHING (TradingView)
# ------------------------------------------------------------------
def to_tradingview_symbol(symbol):
    return symbol.upper().replace('-', '_')

def get_prices_with_volume(symbols):
    """
    Fetch live price + current volume from TradingView scanner.
    Returns {symbol: {"price": float|None, "volume": float|None}}.
    """
    symbols = list(set(symbols))
    if not symbols:
        return {}

    result = {}
    for i in range(0, len(symbols), config.TRADINGVIEW_CHUNK_SIZE):
        chunk = symbols[i:i + config.TRADINGVIEW_CHUNK_SIZE]
        tickers = [f"NSE:{to_tradingview_symbol(s)}" for s in chunk]
        payload = {"symbols": {"tickers": tickers}, "columns": ["close", "volume"]}
        try:
            resp = requests.post(
                "https://scanner.tradingview.com/india/scan",
                json=payload, timeout=15
            )
            resp.raise_for_status()
            data = resp.json()
            for entry in data.get('data', []):
                tv_symbol = entry['s'].split(':')[1]
                vals = entry['d']
                price = vals[0] if len(vals) > 0 else None
                volume = vals[1] if len(vals) > 1 else None
                tv_clean = tv_symbol.upper().replace('-', '_')
                for original in chunk:
                    if to_tradingview_symbol(original) == tv_clean:
                        result[original] = {
                            "price": float(price) if price is not None else None,
                            "volume": float(volume) if volume is not None else None,
                        }
                        break
        except Exception as e:
            logger.warning(f"TradingView error: {e}")
        time.sleep(config.TRADINGVIEW_DELAY)
    return result

def get_prices_tradingview_chunked(symbols):
    """Backward-compat wrapper. Returns {symbol: price}."""
    pv = get_prices_with_volume(symbols)
    return {s: v.get("price") for s, v in pv.items()}

# ------------------------------------------------------------------
#  PRICE FETCHING (Yahoo Finance fallback)
# ------------------------------------------------------------------
def get_prices_yfinance(symbols):
    """Per-symbol Yahoo fallback. Returns {symbol: {"price": p, "volume": None}}."""
    prices = {}
    for sym in symbols:
        try:
            ticker = yf.Ticker(f"{sym.upper()}.NS")
            data = ticker.history(period="1d", interval="1m")
            if not data.empty:
                prices[sym] = {"price": float(data['Close'].iloc[-1]), "volume": None}
        except Exception:
            pass
    return prices

# ------------------------------------------------------------------
#  MASTER PRICE FETCHER
# ------------------------------------------------------------------
def get_prices(symbols):
    """Return {symbol: price}. Uses TradingView first, Yahoo fallback."""
    pv = get_prices_with_volume(symbols)
    missing = [s for s in symbols if s not in pv or pv[s].get("price") is None]
    if missing and config.YAHOO_FINANCE_ENABLED:
        logger.info(f"Yahoo fallback for {len(missing)} symbols.")
        yf_pv = get_prices_yfinance(missing)
        pv.update(yf_pv)
    return {s: v.get("price") for s, v in pv.items()}

# ------------------------------------------------------------------
#  PREVIOUS CLOSE - DAY-LEVEL CACHE
# ------------------------------------------------------------------
def _invalidate_cache_if_new_day():
    global _PREV_CLOSE_CACHE, _PREV_CLOSE_DATE, _AVG_VOLUME_CACHE, _AVG_VOLUME_DATE
    today = date.today()
    if _PREV_CLOSE_DATE != today:
        _PREV_CLOSE_CACHE = {}
        _PREV_CLOSE_DATE = today
        logger.info(f"Previous-close cache invalidated for new day: {today}")
    if _AVG_VOLUME_DATE != today:
        _AVG_VOLUME_CACHE = {}
        _AVG_VOLUME_DATE = today
        logger.info(f"Avg-volume cache invalidated for new day: {today}")

def batch_fetch_prev_closes(symbols):
    if not symbols:
        return {}
    symbols = [s.upper() for s in symbols]
    tickers = [f"{s}.NS" for s in symbols]

    logger.info(f"Batch fetching previous closes for {len(symbols)} symbols...")
    result = {}

    try:
        data = yf.download(
            tickers=" ".join(tickers),
            period="10d", interval="1d",
            progress=False, group_by='ticker',
            threads=True, auto_adjust=False
        )
    except Exception as e:
        logger.error(f"Batch download failed: {e}")
        return {}

    today_ist = datetime.now(config.TIMEZONE).date()

    for sym, ticker in zip(symbols, tickers):
        try:
            df = None
            if len(symbols) == 1:
                df = data
            else:
                if hasattr(data.columns, 'levels') and ticker in data.columns.levels[0]:
                    df = data[ticker]
                else:
                    result[sym] = None
                    continue

            if df is None or df.empty:
                result[sym] = None
                continue

            if 'Close' not in df.columns:
                logger.warning(f"No 'Close' column for {sym} — skipping.")
                result[sym] = None
                continue

            closes = df['Close'].dropna()
            if closes.empty:
                result[sym] = None
                continue

            prev_close = None
            for i in range(len(closes) - 1, -1, -1):
                idx = closes.index[i]
                bar_date = _extract_date_ist(idx)
                if bar_date is not None and bar_date < today_ist:
                    prev_close = float(closes.iloc[i])
                    break

            result[sym] = prev_close
        except Exception as e:
            logger.warning(f"Failed to extract prev close for {sym}: {e}")
            result[sym] = None

    return result

def get_prev_closes(symbols):
    """Cache-first fetch of prev closes. NOT safe inside request handlers."""
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

def get_prev_closes_cached_only(symbols):
    """Cache only. Safe inside request handlers."""
    _invalidate_cache_if_new_day()
    if not symbols:
        return {}
    return {s.upper(): _PREV_CLOSE_CACHE.get(s.upper()) for s in symbols}

def add_symbol_to_cache(symbol):
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
#  20-DAY AVERAGE VOLUME - DAY-LEVEL CACHE
# ------------------------------------------------------------------
def batch_fetch_avg_volume(symbols):
    """
    Batch fetch 20-day average volume (excluding today's incomplete bar).
    One Yahoo call for all symbols. Returns {symbol: avg_volume | None}.
    """
    if not symbols:
        return {}
    symbols = [s.upper() for s in symbols]
    tickers = [f"{s}.NS" for s in symbols]

    logger.info(f"Batch fetching 20-day avg volume for {len(symbols)} symbols...")
    result = {}

    try:
        data = yf.download(
            tickers=" ".join(tickers),
            period="45d", interval="1d",
            progress=False, group_by='ticker',
            threads=True, auto_adjust=False
        )
    except Exception as e:
        logger.error(f"Avg-volume batch download failed: {e}")
        return {}

    today_ist = datetime.now(config.TIMEZONE).date()

    for sym, ticker in zip(symbols, tickers):
        try:
            df = None
            if len(symbols) == 1:
                df = data
            else:
                if hasattr(data.columns, 'levels') and ticker in data.columns.levels[0]:
                    df = data[ticker]
                else:
                    result[sym] = None
                    continue

            if df is None or df.empty or 'Volume' not in df.columns:
                result[sym] = None
                continue

            volumes = df['Volume'].dropna()
            completed = []
            for i in range(len(volumes)):
                idx = volumes.index[i]
                bar_date = _extract_date_ist(idx)
                if bar_date is not None and bar_date < today_ist:
                    completed.append(float(volumes.iloc[i]))

            if len(completed) >= 20:
                result[sym] = sum(completed[-20:]) / 20
            elif completed:
                result[sym] = sum(completed) / len(completed)
            else:
                result[sym] = None
        except Exception as e:
            logger.warning(f"Avg-volume extract failed for {sym}: {e}")
            result[sym] = None

    return result

def get_avg_volumes(symbols):
    """Cache-first fetch of avg volumes. NOT safe inside request handlers."""
    global _AVG_VOLUME_CACHE
    _invalidate_cache_if_new_day()
    if not symbols:
        return {}

    symbols = [s.upper() for s in symbols]
    result = {}
    missing = []
    for sym in symbols:
        if sym in _AVG_VOLUME_CACHE:
            result[sym] = _AVG_VOLUME_CACHE[sym]
        else:
            missing.append(sym)

    if missing:
        fetched = batch_fetch_avg_volume(missing)
        for sym in missing:
            val = fetched.get(sym)
            _AVG_VOLUME_CACHE[sym] = val
            result[sym] = val
        logger.info(f"Cached avg volumes for {len(missing)} new symbols.")

    return result

def get_avg_volumes_cached_only(symbols):
    """Cache only. Safe inside request handlers."""
    _invalidate_cache_if_new_day()
    if not symbols:
        return {}
    return {s.upper(): _AVG_VOLUME_CACHE.get(s.upper()) for s in symbols}

# ------------------------------------------------------------------
#  END-OF-DAY SNAPSHOT
# ------------------------------------------------------------------
def batch_fetch_eod(symbols):
    """
    Fetch today's completed OHLCV bar for each symbol in ONE Yahoo call.
    Called around 4:30 PM IST. Returns:
    {symbol: {"open":..,"high":..,"low":..,"close":..,"volume":..} | None}.
    """
    if not symbols:
        return {}
    symbols = [s.upper() for s in symbols]
    tickers = [f"{s}.NS" for s in symbols]

    logger.info(f"Batch fetching EOD data for {len(symbols)} symbols...")
    result = {}

    try:
        data = yf.download(
            tickers=" ".join(tickers),
            period="5d", interval="1d",
            progress=False, group_by='ticker',
            threads=True, auto_adjust=False
        )
    except Exception as e:
        logger.error(f"EOD batch download failed: {e}")
        return {}

    today_ist = datetime.now(config.TIMEZONE).date()

    for sym, ticker in zip(symbols, tickers):
        try:
            df = None
            if len(symbols) == 1:
                df = data
            else:
                if hasattr(data.columns, 'levels') and ticker in data.columns.levels[0]:
                    df = data[ticker]
                else:
                    result[sym] = None
                    continue

            if df is None or df.empty or 'Close' not in df.columns:
                result[sym] = None
                continue

            row = None
            for i in range(len(df) - 1, -1, -1):
                idx = df.index[i]
                bar_date = _extract_date_ist(idx)
                if bar_date == today_ist:
                    row = df.iloc[i]
                    break

            if row is None:
                result[sym] = None
                continue

            def _fv(col):
                if col not in df.columns:
                    return None
                v = row[col]
                return None if pd.isna(v) else float(v)

            result[sym] = {
                "open":   _fv('Open'),
                "high":   _fv('High'),
                "low":    _fv('Low'),
                "close":  _fv('Close'),
                "volume": _fv('Volume'),
            }
        except Exception as e:
            logger.warning(f"EOD extract failed for {sym}: {e}")
            result[sym] = None

    return result

# ------------------------------------------------------------------
#  TELEGRAM (with retry)
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
#  MAIN LOOP (supervised + heartbeat)
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
                symbol = alert['symbol']
                cond = alert['condition']
                trigger = alert['trigger_price']
                current = prices.get(symbol)
                if current is None:
                    continue

                triggered = False
                if cond == '>' and current > trigger:
                    triggered = True
                elif cond == '<' and current < trigger:
                    triggered = True

                if triggered:
                    msg = (f"🔔 ALERT\n{symbol} {cond} {trigger}\n"
                           f"Current: {current}\n{now.strftime('%H:%M:%S')} IST")
                    send_telegram(msg)
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