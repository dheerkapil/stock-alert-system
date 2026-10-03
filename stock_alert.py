import os
import time
import logging
import threading
import requests
import yfinance as yf
import pandas as pd
from datetime import datetime
import config

logging.basicConfig(level=config.LOG_LEVEL, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

WEBUI_URL = os.environ.get("WEBUI_URL", "https://stock-alert-ui.onrender.com")

# ------------------------------------------------------------------
#  WORKER TICK — for /api/health liveness checks
# ------------------------------------------------------------------
_last_worker_tick = time.time()

def get_last_worker_tick():
    return _last_worker_tick

# ------------------------------------------------------------------
#  TRADINGVIEW CACHE — 60 seconds per symbol, thread-safe
# ------------------------------------------------------------------
_TV_CACHE = {}                  # {symbol: (timestamp, data_dict)}
_TV_CACHE_LOCK = threading.Lock()
_TV_CACHE_TTL = 60

# ------------------------------------------------------------------
#  DATA-SOURCE HEALTH MONITORING
# ------------------------------------------------------------------
TV_FAILURE_THRESHOLD = 3
TV_MIN_COVERAGE      = 0.10

_tv_failure_streak = 0
_tv_alert_sent     = False

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
#  TRADINGVIEW — fetch (no cache) with 429 retry
# ------------------------------------------------------------------
def to_tradingview_symbol(symbol):
    return symbol.upper().replace('-', '_')

def _fetch_tv_batch(symbols):
    """
    Raw TradingView fetch. No cache. Retries each chunk once on 429.
    Returns {symbol: {price, volume, avg_vol_10d, rvol, prev_close}}.
    """
    if not symbols:
        return {}

    result = {}
    for i in range(0, len(symbols), config.TRADINGVIEW_CHUNK_SIZE):
        chunk = symbols[i:i + config.TRADINGVIEW_CHUNK_SIZE]
        tickers = [f"NSE:{to_tradingview_symbol(s)}" for s in chunk]
        payload = {
            "symbols": {"tickers": tickers},
            "columns": [
                "close",
                "volume",
                "average_volume_10d_calc",
                "relative_volume_10d_calc",
                "prev_close_price",
            ]
        }

        for attempt in (1, 2):
            try:
                resp = requests.post(
                    "https://scanner.tradingview.com/india/scan",
                    json=payload, timeout=15
                )

                if resp.status_code == 429:
                    if attempt == 1:
                        logger.warning("TradingView 429 — retrying in 3s")
                        time.sleep(3)
                        continue
                    else:
                        logger.warning("TradingView 429 again — skipping this chunk")
                        break

                resp.raise_for_status()

                for entry in resp.json().get('data', []):
                    tv_symbol = entry['s'].split(':')[1]
                    vals = entry['d']
                    price       = vals[0] if len(vals) > 0 else None
                    volume      = vals[1] if len(vals) > 1 else None
                    avg_vol_10d = vals[2] if len(vals) > 2 else None
                    rvol        = vals[3] if len(vals) > 3 else None
                    prev_close  = vals[4] if len(vals) > 4 else None
                    tv_clean = tv_symbol.upper().replace('-', '_')
                    for original in chunk:
                        if to_tradingview_symbol(original) == tv_clean:
                            result[original] = {
                                "price":       float(price)       if price       is not None else None,
                                "volume":      float(volume)      if volume      is not None else None,
                                "avg_vol_10d": float(avg_vol_10d) if avg_vol_10d is not None else None,
                                "rvol":        float(rvol)        if rvol        is not None else None,
                                "prev_close":  float(prev_close)  if prev_close  is not None else None,
                            }
                            break
                break
            except Exception as e:
                logger.warning(f"TradingView error (attempt {attempt}): {e}")
                if attempt == 1:
                    time.sleep(2)
                continue

        time.sleep(config.TRADINGVIEW_DELAY)

    return result

# ------------------------------------------------------------------
#  TRADINGVIEW — cached wrapper (60s per symbol)
# ------------------------------------------------------------------
def get_prices_with_volume(symbols):
    """
    60-second per-symbol cached TradingView lookup.
    Shared safely between the worker thread and request handlers.
    """
    symbols = list(set(symbols))
    if not symbols:
        return {}

    now = time.time()
    result = {}
    missing = []

    with _TV_CACHE_LOCK:
        for sym in symbols:
            entry = _TV_CACHE.get(sym)
            if entry and (now - entry[0]) < _TV_CACHE_TTL:
                result[sym] = entry[1]
            else:
                missing.append(sym)

    if not missing:
        return result

    logger.info(f"TV fetch: {len(missing)} missing, {len(result)} cached")

    fetched = _fetch_tv_batch(missing)

    with _TV_CACHE_LOCK:
        for sym, data in fetched.items():
            _TV_CACHE[sym] = (now, data)

    result.update(fetched)
    return result

# ------------------------------------------------------------------
#  YAHOO FALLBACK — live price only
# ------------------------------------------------------------------
def get_prices_yfinance(symbols):
    prices = {}
    for sym in symbols:
        try:
            df = yf.Ticker(f"{sym.upper()}.NS").history(period="1d", interval="1m")
            if not df.empty:
                prices[sym] = {
                    "price": float(df['Close'].iloc[-1]),
                    "volume": None, "avg_vol_10d": None,
                    "rvol": None, "prev_close": None,
                }
        except Exception:
            pass
    return prices

def get_prices(symbols):
    pv = get_prices_with_volume(symbols)
    missing = [s for s in symbols if s not in pv or pv[s].get("price") is None]
    if missing and config.YAHOO_FINANCE_ENABLED:
        logger.info(f"Yahoo fallback for {len(missing)} symbols.")
        pv.update(get_prices_yfinance(missing))
    return {s: v.get("price") for s, v in pv.items()}

# ------------------------------------------------------------------
#  YAHOO — daily bars (EOD snapshot + backfill)
# ------------------------------------------------------------------
def batch_fetch_daily_bars(symbols, days=5, include_today=False):
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
#  ALERT MESSAGE FORMATTER
# ------------------------------------------------------------------
def format_alert_message(alert, cmp_price):
    """
    Format matches user's spec exactly:

        RELIANCE

        Price > 2900

        Day Chg: +1.2%

        RVol: 2.45

        Note: buy 2500
    """
    symbol    = alert['symbol']
    condition = alert['condition']
    trigger   = alert['trigger_price']
    pct_chg   = alert.get('pct_chg')
    rvol      = alert.get('rvol')
    notes     = (alert.get('notes') or '').strip()

    day_chg_str = f"{pct_chg:+.2f}%" if pct_chg is not None else "-"
    rvol_str    = f"{rvol:.2f}"    if rvol    is not None else "-"

    lines = [
        symbol,
        "",
        f"Price {condition} {trigger}",
        "",
        f"Day Chg: {day_chg_str}",
        "",
        f"RVol: {rvol_str}",
    ]
    if notes:
        lines.append("")
        lines.append(f"Note: {notes}")

    return "\n".join(lines)

# ------------------------------------------------------------------
#  MAIN LOOP — supervised. No hourly heartbeat. Anomaly detection.
# ------------------------------------------------------------------
def main():
    global _last_worker_tick, _tv_failure_streak, _tv_alert_sent

    logger.info(f"🚀 Worker started. Poll interval: {config.POLL_INTERVAL}s.")
    logger.info(f"Market hours: {config.START_TIME} - {config.STOP_TIME} IST. Weekdays only.")

    send_telegram(
        f"🟢 System online — "
        f"{datetime.now(config.TIMEZONE).strftime('%Y-%m-%d %H:%M:%S IST')}"
    )

    while True:
        try:
            _last_worker_tick = time.time()
            now = datetime.now(config.TIMEZONE)

            while not is_weekday(now) or not is_market_open(now):
                time.sleep(60)
                _last_worker_tick = time.time()
                now = datetime.now(config.TIMEZONE)

            _last_worker_tick = time.time()
            alerts = get_active_alerts()

            if not alerts:
                logger.info("No active alerts.")
                _tv_failure_streak = 0
                if _tv_alert_sent:
                    send_telegram("✅ Market data recovered")
                    _tv_alert_sent = False
                time.sleep(config.POLL_INTERVAL)
                continue

            symbols = list(set(a['symbol'] for a in alerts))
            logger.info(f"Fetching prices for {len(symbols)} symbols...")
            prices = get_prices(symbols)

            coverage = len(prices) / len(symbols) if symbols else 1.0
            if coverage < TV_MIN_COVERAGE:
                _tv_failure_streak += 1
                logger.warning(f"Low data coverage: {int(coverage*100)}% "
                               f"(streak={_tv_failure_streak})")
                if _tv_failure_streak >= TV_FAILURE_THRESHOLD and not _tv_alert_sent:
                    send_telegram(
                        f"⚠️ Market data degraded\n"
                        f"Coverage: {int(coverage*100)}% of {len(symbols)} symbols\n"
                        f"Streak: {_tv_failure_streak} cycles\n"
                        f"Since: {now.strftime('%H:%M')} IST"
                    )
                    _tv_alert_sent = True
            else:
                if _tv_alert_sent:
                    send_telegram(
                        f"✅ Market data recovered\n"
                        f"Coverage: {int(coverage*100)}% of {len(symbols)} symbols"
                    )
                    _tv_alert_sent = False
                _tv_failure_streak = 0

            for alert in alerts:
                current = prices.get(alert['symbol'])
                if current is None:
                    continue

                triggered = (
                    (alert['condition'] == '>' and current > alert['trigger_price']) or
                    (alert['condition'] == '<' and current < alert['trigger_price'])
                )

                if triggered:
                    send_telegram(format_alert_message(alert, current))
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
            _last_worker_tick = time.time()
            logger.exception(f"Worker error: {e}. Restarting in 30s.")
            time.sleep(30)

if __name__ == "__main__":
    main()