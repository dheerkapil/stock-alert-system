import os
import io
import json
import time
import logging
import threading
import requests
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
import config

logging.basicConfig(level="INFO", format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

WEBUI_URL = "https://stock-alert-ui.onrender.com"

_last_tick = time.time()

def get_last_tick():
    return _last_tick

# ------------------------------------------------------------------
#  TRADINGVIEW CACHE — RAM + file-backed
# ------------------------------------------------------------------
_TV_CACHE = {}
_TV_CACHE_LOCK = threading.Lock()
_backoff_until = 0.0

_TV_FILE = os.path.join(os.path.dirname(os.path.abspath(config.DB_FILE)), "tv_cache_local.json")
_TV_FILE_MEMO = {"mtime": 0, "data": {}}
_TV_FILE_MEMO_LOCK = threading.Lock()

def _tv_symbol(sym):
    return sym.upper().replace('-', '_')

def _write_tv_file():
    try:
        with _TV_CACHE_LOCK:
            payload = {s: {"ts": ts, "data": d} for s, (ts, d) in _TV_CACHE.items()}
        tmp = _TV_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, _TV_FILE)
    except Exception as e:
        logger.warning(f"TV cache file write failed: {e}")

def _read_tv_file():
    try:
        if not os.path.exists(_TV_FILE):
            return {}
        mtime = os.path.getmtime(_TV_FILE)
        with _TV_FILE_MEMO_LOCK:
            if mtime == _TV_FILE_MEMO["mtime"]:
                return _TV_FILE_MEMO["data"]
            with open(_TV_FILE) as f:
                raw = json.load(f)
            data = {}
            for sym, e in raw.items():
                try:
                    ts = float(e.get("ts", 0))
                    d = e.get("data") or {}
                    if d.get("price") is None:
                        continue
                    data[sym] = (ts, d)
                except Exception:
                    continue
            _TV_FILE_MEMO["mtime"] = mtime
            _TV_FILE_MEMO["data"] = data
            return data
    except Exception as e:
        logger.warning(f"TV cache file read failed: {e}")
        return {}

def _tv_fetch(symbols):
    global _backoff_until

    result = {}
    total_chunks = (len(symbols) + config.TRADINGVIEW_CHUNK_SIZE - 1) // config.TRADINGVIEW_CHUNK_SIZE

    for idx, i in enumerate(range(0, len(symbols), config.TRADINGVIEW_CHUNK_SIZE), start=1):
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
                logger.warning(f"TV 429 on chunk {idx}/{total_chunks}")
                return result

            if r.status_code != 200:
                logger.warning(f"TV chunk {idx}/{total_chunks} HTTP {r.status_code}")
                time.sleep(config.TRADINGVIEW_DELAY)
                continue

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
            logger.warning(f"TV chunk {idx}/{total_chunks} failed: {e}")

        time.sleep(config.TRADINGVIEW_DELAY)

    return result

def refresh_tv_cache(symbols):
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

    logger.info(f"TV refresh: {len(missing)} missing of {len(symbols)}")
    fetched = _tv_fetch(missing)

    updated = 0
    with _TV_CACHE_LOCK:
        for sym, data in fetched.items():
            if data.get("price") is None:
                continue
            _TV_CACHE[sym] = (now, data)
            updated += 1

    if updated:
        _write_tv_file()

    logger.info(f"TV refresh wrote {updated} to RAM+file")
    return updated

def get_cached(symbols):
    result = {}
    with _TV_CACHE_LOCK:
        for s in set(symbols):
            e = _TV_CACHE.get(s)
            if e:
                result[s] = e[1]

    missing = set(symbols) - set(result.keys())
    if missing:
        file_cache = _read_tv_file()
        for s in missing:
            e = file_cache.get(s)
            if e:
                result[s] = e[1]

    return result

def cache_snapshot():
    with _TV_CACHE_LOCK:
        ram = {s: {"ts": ts, "data": d} for s, (ts, d) in _TV_CACHE.items()}
    if ram:
        return ram
    file_cache = _read_tv_file()
    return {s: {"ts": ts, "data": d} for s, (ts, d) in file_cache.items()}

def cache_restore(entries, max_age=1800):
    now = time.time()
    n = 0
    with _TV_CACHE_LOCK:
        for sym, e in entries.items():
            ts = float(e.get("ts", 0))
            if now - ts > max_age:
                continue
            data = e.get("data") or {}
            if data.get("price") is None:
                continue
            _TV_CACHE[sym] = (ts, data)
            n += 1
    if n:
        _write_tv_file()
    logger.info(f"cache_restore loaded {n}")
    return n

# ------------------------------------------------------------------
#  NSE BHAVCOPY — primary EOD source
# ------------------------------------------------------------------
_NSE_BHAV_URL = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv"

def _bhav_headers():
    return {
        "User-Agent": config.USER_AGENT,
        "Accept": "text/csv,*/*",
        "Referer": "https://www.nseindia.com/",
    }

def fetch_bhavcopy_for_date(target_date, symbols):
    """
    Fetch NSE bhavcopy for a single date.
    Returns:
      None  → file not available (retry later)
      []    → file available but no matching symbols (mark complete)
      [...] → list of (symbol, date_str, open, high, low, close, volume)
    """
    url = _NSE_BHAV_URL.format(ddmmyyyy=target_date.strftime('%d%m%Y'))
    try:
        r = requests.get(url, headers=_bhav_headers(), timeout=20)
    except Exception as e:
        logger.warning(f"Bhavcopy {target_date}: request failed: {e}")
        return None

    if r.status_code == 404:
        return None
    if r.status_code != 200:
        logger.warning(f"Bhavcopy {target_date}: HTTP {r.status_code}")
        return None

    try:
        df = pd.read_csv(io.StringIO(r.text))
        df.columns = [c.strip() for c in df.columns]
        if 'SERIES' in df.columns:
            df['SERIES'] = df['SERIES'].astype(str).str.strip()
            df = df[df['SERIES'].isin(['EQ', 'BE'])]
    except Exception as e:
        logger.warning(f"Bhavcopy {target_date}: parse failed: {e}")
        return None

    sym_col   = next((c for c in df.columns if c.upper() == 'SYMBOL'), None)
    open_col  = next((c for c in df.columns if 'OPEN'  in c.upper()), None)
    high_col  = next((c for c in df.columns if 'HIGH'  in c.upper()), None)
    low_col   = next((c for c in df.columns if 'LOW'   in c.upper()), None)
    close_col = next((c for c in df.columns if c.upper() in ('CLOSE_PRICE', 'CLOSE')), None)
    vol_col   = next((c for c in df.columns if 'TTL_TRD_QNTY' in c.upper() or 'VOLUME' in c.upper()), None)

    if not sym_col or not close_col:
        logger.warning(f"Bhavcopy {target_date}: missing SYMBOL or CLOSE column")
        return None

    watch = {s.upper() for s in symbols}
    sub = df[df[sym_col].astype(str).str.upper().isin(watch)]

    date_str = target_date.strftime('%Y-%m-%d')
    rows = []
    for _, row in sub.iterrows():
        try:
            sym = str(row[sym_col]).strip().upper()
            c = float(row[close_col]) if pd.notna(row[close_col]) else None
            if c is None:
                continue
            o = float(row[open_col]) if open_col and pd.notna(row[open_col]) else None
            h = float(row[high_col]) if high_col and pd.notna(row[high_col]) else None
            l = float(row[low_col])  if low_col  and pd.notna(row[low_col])  else None
            v = float(row[vol_col])  if vol_col  and pd.notna(row[vol_col])  else None
            rows.append((sym, date_str, o, h, l, c, v))
        except Exception:
            continue

    logger.info(f"Bhavcopy {target_date}: file OK, {len(rows)} of {len(watch)} matched")
    return rows

def batch_fetch_daily_bars(symbols, days=5, include_today=False):
    """
    Multi-day bhavcopy fetch. Skips weekends. Returns tuples
    (symbol, date_str, o, h, l, c, v) for the last `days` available trading days.
    """
    if not symbols:
        return []

    symbols = list({s.upper() for s in symbols})
    today = datetime.now(config.TIMEZONE).date()

    # Build candidate dates
    candidates = []
    d = today
    lookback = 0
    while len(candidates) < days and lookback < days * 3 + 10:
        if d.weekday() < 5:
            if not (d == today and not include_today):
                candidates.append(d)
        d = d - timedelta(days=1)
        lookback += 1

    all_rows = []
    days_found = 0
    for cand in candidates:
        if days_found >= days:
            break
        rows = fetch_bhavcopy_for_date(cand, symbols)
        if rows:  # None or [] → skip
            all_rows.extend(rows)
            days_found += 1
        time.sleep(0.3)

    logger.info(f"batch_fetch_daily_bars: {len(all_rows)} rows across {days_found} days")
    return all_rows

# ------------------------------------------------------------------
#  YAHOO — fallback only, single date
# ------------------------------------------------------------------
def batch_fetch_daily_bars_yahoo(symbols, days=5, include_today=False):
    """Original Yahoo-based fetcher. Used only as midnight fallback."""
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
            logger.warning(f"Yahoo extract failed for {sym}: {e}")

    return rows

# ------------------------------------------------------------------
#  TELEGRAM
# ------------------------------------------------------------------
def send_telegram(message, retries=3):
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
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
#  WORKER LOOP
# ------------------------------------------------------------------
def worker_loop():
    global _last_tick

    logger.info(f"🚀 Worker started. Poll interval: {config.POLL_INTERVAL}s.")
    send_telegram(f"🟢 System online — {datetime.now(config.TIMEZONE).strftime('%Y-%m-%d %H:%M:%S IST')}")

    while True:
        try:
            _last_tick = time.time()
            now = datetime.now(config.TIMEZONE)

            while not (now.weekday() < 5
                       and datetime.strptime(config.START_TIME, "%H:%M").time()
                           <= now.time()
                           <= datetime.strptime(config.STOP_TIME, "%H:%M").time()):
                time.sleep(60)
                _last_tick = time.time()
                now = datetime.now(config.TIMEZONE)

            _last_tick = time.time()

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