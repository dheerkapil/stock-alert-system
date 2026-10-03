import time
import logging
import threading
import requests
import yfinance as yf
import pandas as pd
from datetime import datetime
import config

logging.basicConfig(level="INFO", format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

WEBUI_URL = "https://stock-alert-ui.onrender.com"

# ------------------------------------------------------------------
#  WORKER TICK — read by /api/health
# ------------------------------------------------------------------
_last_tick = time.time()

def get_last_tick():
    return _last_tick

# ------------------------------------------------------------------
#  TRADINGVIEW CACHE — RAM only, thread-safe
#  Only refresh_tv_cache() writes. Everyone else reads.
# ------------------------------------------------------------------
_TV_CACHE = {}
_TV_CACHE_LOCK = threading.Lock()
_backoff_until = 0.0

def _tv_symbol(sym):
    return sym.upper().replace('-', '_')

def _tv_fetch(symbols):
    """One-shot TradingView fetch. On 429, sets backoff and aborts."""
    global _backoff_until

    result = {}
    for i in range(0, len(symbols), config.TRADINGVIEW_CHUNK_SIZE):
        chunk = symbols[i:i + config.TRADINGVIEW_CHUNK_SIZE]
        payload = {
            "symbols": {"tickers": [f"NSE:{_tv_symbol(s)}" for s in chunk]},
            "columns": [
                "close", "volume",
                "average_volume_10d_calc", "relative_volume_10d_calc",
                "prev_close_price",
            ]
        }
        try:
            r = requests.post("https://scanner.tradingview.com/india/scan",
                              json=payload, timeout=8)

            if r.status_code == 429:
                _backoff_until = time.time() + config.TV_CACHE_BACKOFF_SECONDS
                logger.warning(f"TV 429 — backing off {config.TV_CACHE_BACKOFF_SECONDS}s")
                return result

            r.raise_for_status()

            for entry in r.json().get('data', []):
                ticker = entry['s'].split(':')[1]
                v = entry['d']
                def f(x): return float(x) if x is not None else None
                data = {
                    "price":       f(v[0]) if len(v) > 0 else None,
                    "volume":      f(v[1]) if len(v) > 1 else None,
                    "avg_vol_10d": f(v[2]) if len(v) > 2 else None,
                    "rvol":        f(v[3]) if len(v) > 3 else None,
                    "prev_close":  f(v[4]) if len(v) > 4 else None,
                }
                for s in chunk:
                    if _tv_symbol(s) == ticker.upper().replace('-', '_'):
                        result[s] = data
                        break
        except Exception as e:
            logger.warning(f"TV chunk failed: {e}")

        time.sleep(config.TRADINGVIEW_DELAY)

    return result

def refresh_tv_cache(symbols):
    """Fetch only symbols whose cache is stale/missing. Caller: market_loop only."""
    if time.time() < _backoff_until:
        return 0

    symbols = list(set(symbols))
    now = time.time()

    with _TV_CACHE_LOCK:
        missing = [s for s in symbols
                   if s not in _TV_CACHE
                   or (now - _TV_CACHE[s][0]) >= config.TV_CACHE_TTL_SECONDS]

    if not missing:
        return 0

    logger.info(f"TV refresh: {len(missing)} of {len(symbols)}")
    fetched = _tv_fetch(missing)

    with _TV_CACHE_LOCK:
        for sym, data in fetched.items():
            _TV_CACHE[sym] = (now, data)

    return len(fetched)

def get_cached(symbols):
    """Read-only cache access. Never fetches."""
    result = {}
    with _TV_CACHE_LOCK:
        for s in set(symbols):
            e = _TV_CACHE.get(s)
            if e:
                result[s] = e[1]
    return result

def cache_snapshot():
    with _TV_CACHE_LOCK:
        return {s: {"ts": ts, "data": d} for s, (ts, d) in _TV_CACHE.items()}

def cache_restore(entries, max_age=1800):
    now = time.time()
    n = 0
    with _TV_CACHE_LOCK:
        for sym, e in entries.items():
            ts = float(e.get("ts", 0))
            if now - ts > max_age:
                continue
            _TV_CACHE[sym] = (ts, e["data"])
            n += 1
    return n

# ------------------------------------------------------------------
#  YAHOO — daily bars (EOD + backfill)
# ------------------------------------------------------------------
def batch_fetch_daily_bars(symbols, days=5, include_today=False):
    if not symbols:
        return []
    symbols = [s.upper() for s in symbols]
    tickers = [f"{s}.NS" for s in symbols]

    logger.info(f"Yahoo: {days}d bars for {len(symbols)} symbols")

    try:
        data = yf.download(
            tickers=" ".join(tickers),
            period=f"{days + 2}d", interval="1d",
            progress=False, group_by='ticker',
            threads=True, auto_adjust=False
        )
    except Exception as e:
        logger.error(f"Yahoo download failed: {e}")
        return []

    today = datetime.now(config.TIMEZONE).date()
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
                idx = df.index[i]
                bar_date = (idx.astimezone(config.TIMEZONE).date()
                            if hasattr(idx, 'tzinfo') and idx.tzinfo
                            else idx.date())
                if not include_today and bar_date >= today:
                    continue

                def fv(col):
                    if col not in df.columns:
                        return None
                    v = df[col].iloc[i]
                    return None if pd.isna(v) else float(v)

                rows.append((sym, bar_date.strftime('%Y-%m-%d'),
                             fv('Open'), fv('High'), fv('Low'), fv('Close'), fv('Volume')))
        except Exception as e:
            logger.warning(f"Extract failed for {sym}: {e}")

    return rows

# ------------------------------------------------------------------
#  TELEGRAM
# ------------------------------------------------------------------
def send_telegram(message, retries=3):
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.error("Telegram credentials missing")
        return False

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": config.TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}

    for attempt in range(1, retries + 1):
        try:
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        if attempt < retries:
            time.sleep(2)

    logger.error(f"Telegram failed: {message[:60]}")
    return False

def format_alert(alert):
    """Format alert as specified by the user."""
    notes = (alert.get('notes') or '').strip()
    pct = alert.get('pct_chg')
    rvol = alert.get('rvol')

    lines = [
        alert['symbol'],
        "",
        f"Price {alert['condition']} {alert['trigger_price']}",
        "",
        f"Day Chg: {pct:+.2f}%" if pct is not None else "Day Chg: -",
        "",
        f"RVol: {rvol:.2f}" if rvol is not None else "RVol: -",
    ]
    if notes:
        lines.append("")
        lines.append(f"Note: {notes}")
    return "\n".join(lines)

# ------------------------------------------------------------------
#  WORKER MAIN
# ------------------------------------------------------------------
def worker_loop():
    global _last_tick

    logger.info(f"🚀 Worker started. Poll interval: {config.POLL_INTERVAL}s.")

    send_telegram(f"🟢 System online — {datetime.now(config.TIMEZONE).strftime('%Y-%m-%d %H:%M:%S IST')}")

    while True:
        try:
            _last_tick = time.time()
            now = datetime.now(config.TIMEZONE)

            # Wait for market hours
            while not (now.weekday() < 5
                       and datetime.strptime(config.START_TIME, "%H:%M").time()
                           <= now.time()
                           <= datetime.strptime(config.STOP_TIME, "%H:%M").time()):
                time.sleep(60)
                _last_tick = time.time()
                now = datetime.now(config.TIMEZONE)

            _last_tick = time.time()

            # Get alerts from web UI
            try:
                r = requests.get(f"{WEBUI_URL}/api/alerts",
                                 headers={'X-API-Key': config.WORKER_API_KEY},
                                 timeout=10)
                r.raise_for_status()
                alerts = [a for a in r.json()
                          if a['is_active'] == 1 and a['is_triggered'] == 0]
            except Exception as e:
                logger.error(f"Failed to fetch alerts: {e}")
                time.sleep(config.POLL_INTERVAL)
                continue

            if not alerts:
                time.sleep(config.POLL_INTERVAL)
                continue

            # Read prices from cache (never fetch — warmer does that)
            symbols = list({a['symbol'] for a in alerts})
            prices = get_cached(symbols)

            for a in alerts:
                price = prices.get(a['symbol'], {}).get('price')
                if price is None:
                    continue

                fired = (
                    (a['condition'] == '>' and price > a['trigger_price']) or
                    (a['condition'] == '<' and price < a['trigger_price'])
                )
                if not fired:
                    continue

                send_telegram(format_alert(a))

                try:
                    requests.post(f"{WEBUI_URL}/api/mark_triggered/{a['id']}",
                                  headers={'X-API-Key': config.WORKER_API_KEY},
                                  timeout=10)
                except Exception as e:
                    logger.error(f"mark_triggered failed: {e}")

                logger.info(f"Alert {a['id']} ({a['symbol']}) triggered")

            time.sleep(config.POLL_INTERVAL)

        except Exception as e:
            _last_tick = time.time()
            logger.exception(f"Worker error: {e}")
            time.sleep(30)