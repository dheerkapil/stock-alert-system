import os
import sqlite3
import threading
import time
import logging
import random
import hmac
import hashlib
import base64
import requests
import pandas as pd
import json
from io import StringIO
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, redirect, make_response
import config
import stock_alert

logging.basicConfig(level="INFO", format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

SESSION_SECRET = os.environ.get("SESSION_SECRET_KEY", "change-me-please")
HEALTHCHECK_URL = os.environ.get("HEALTHCHECK_PING_URL", "")

GITHUB_TOKEN  = os.environ.get("GITHUB_BACKUP_TOKEN", "")
GITHUB_REPO   = os.environ.get("GITHUB_REPO", "dheerkapil/stock-alert-system")
GH_WATCHLIST  = "watchlist_backup.json"
GH_EOD        = "eod_backup.json"
GH_NSE        = "nse_symbols.json"
GH_CACHE      = "tv_cache.json"
GH_CACHE_BRANCH = "cache"
GH_API        = "https://api.github.com"

# ------------------------------------------------------------------
#  AUTH
# ------------------------------------------------------------------
_OTP_FILE = os.path.join(os.path.dirname(os.path.abspath(config.DB_FILE)), "otp_state.json")
_OTP_LOCK = threading.Lock()

SESSIONS_DURATION = 30 * 24 * 3600
OTP_VALIDITY = 300
OTP_THROTTLE = 60
MAX_OTP_ATTEMPTS = 5

def _read_otp_file():
    try:
        if not os.path.exists(_OTP_FILE):
            return {}
        with _OTP_LOCK:
            with open(_OTP_FILE) as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"OTP file read failed: {e}")
        return {}

def _write_otp_file(data):
    try:
        with _OTP_LOCK:
            tmp = _OTP_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, _OTP_FILE)
    except Exception as e:
        logger.error(f"OTP file write failed: {e}")

def _ip():
    xff = request.headers.get('X-Forwarded-For', '')
    return xff.split(',')[0].strip() if xff else (request.remote_addr or 'unknown')

def _mk_session():
    expiry = int(time.time()) + SESSIONS_DURATION
    sig = hmac.new(SESSION_SECRET.encode(), str(expiry).encode(), hashlib.sha256).hexdigest()
    return f"{expiry}.{sig}"

def _valid_session(tok):
    try:
        exp, sig = tok.rsplit(".", 1)
        expected = hmac.new(SESSION_SECRET.encode(), exp.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected) and int(exp) > time.time()
    except Exception:
        return False

def _is_auth():
    sid = request.cookies.get('session_id')
    return bool(sid and _valid_session(sid))

def _is_worker():
    return bool(config.WORKER_API_KEY
                and request.headers.get('X-API-Key') == config.WORKER_API_KEY)

@app.before_request
def _gate():
    p = request.path
    if p in ('/login', '/api/send_otp', '/api/verify_otp', '/favicon.ico', '/api/health'):
        return None
    if p == '/api/alerts' or p.startswith('/api/mark_triggered'):
        if _is_worker():
            return None
    if not _is_auth():
        if p.startswith('/api/'):
            return jsonify({'error': 'Unauthorized'}), 401
        return redirect('/login')
    return None

@app.route('/login')
def login_page():
    return redirect('/') if _is_auth() else render_template('login.html')

@app.route('/api/send_otp', methods=['POST'])
def send_otp():
    ip = _ip()
    now = time.time()

    otp_state = _read_otp_file()
    existing = otp_state.get(ip)

    if existing and now - existing.get('sent_at', 0) < OTP_THROTTLE:
        wait = int(OTP_THROTTLE - (now - existing['sent_at']))
        return jsonify({'status': 'error', 'message': f'Wait {wait}s'}), 429

    code = f"{random.randint(0, 9999):04d}"
    otp_state[ip] = {
        'code': code,
        'expiry': now + OTP_VALIDITY,
        'sent_at': now,
        'attempts': 0,
    }
    _write_otp_file(otp_state)

    stock_alert.send_telegram(f"🔐 <b>Login OTP</b>\nCode: <b>{code}</b>\nValid 5 min.")
    return jsonify({'status': 'ok'})

@app.route('/api/verify_otp', methods=['POST'])
def verify_otp():
    code = str((request.json or {}).get('code', '')).strip()
    ip = _ip()
    now = time.time()

    otp_state = _read_otp_file()
    e = otp_state.get(ip)

    if not e or e['expiry'] < now:
        return jsonify({'status': 'error', 'message': 'No OTP or expired'}), 401

    e['attempts'] = e.get('attempts', 0) + 1

    if e['attempts'] > MAX_OTP_ATTEMPTS:
        otp_state.pop(ip, None)
        _write_otp_file(otp_state)
        return jsonify({'status': 'error', 'message': 'Too many attempts'}), 401

    if e['code'] != code:
        otp_state[ip] = e
        _write_otp_file(otp_state)
        return jsonify({'status': 'error', 'message': 'Invalid code'}), 401

    otp_state.pop(ip, None)
    _write_otp_file(otp_state)

    resp = make_response(jsonify({'status': 'ok'}))
    https = (request.headers.get('X-Forwarded-Proto') == 'https') or request.is_secure
    resp.set_cookie('session_id', _mk_session(), max_age=SESSIONS_DURATION,
                    httponly=True, samesite='Lax', secure=https, path='/')
    stock_alert.send_telegram(f"✅ Login from <code>{ip}</code>")
    return resp

@app.route('/api/logout', methods=['POST'])
def logout():
    resp = make_response(jsonify({'status': 'ok'}))
    resp.set_cookie('session_id', '', max_age=0, path='/')
    return resp

@app.route('/api/health')
def health():
    age = time.time() - stock_alert.get_last_tick()
    if age > 900:
        return jsonify({'status': 'error', 'age': int(age)}), 503
    return jsonify({'status': 'ok', 'age': int(age)}), 200

# ------------------------------------------------------------------
#  DATABASE
# ------------------------------------------------------------------
def db():
    c = sqlite3.connect(config.DB_FILE)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c = sqlite3.connect(config.DB_FILE)
    c.executescript('''
        CREATE TABLE IF NOT EXISTS watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            condition TEXT NOT NULL CHECK(condition IN ('>', '<')),
            trigger_price REAL NOT NULL,
            is_active INTEGER DEFAULT 1,
            is_triggered INTEGER DEFAULT 0,
            triggered_at TIMESTAMP,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            notes TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_symbol ON watchlist (symbol);

        CREATE TABLE IF NOT EXISTS eod_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            UNIQUE(symbol, trade_date)
        );
        CREATE INDEX IF NOT EXISTS idx_eod_symbol_date ON eod_snapshots (symbol, trade_date);
        CREATE INDEX IF NOT EXISTS idx_eod_date ON eod_snapshots (trade_date);
    ''')
    c.commit()
    c.close()
    logger.info("Database ready")

def migrate_triggered_at_column():
    try:
        c = sqlite3.connect(config.DB_FILE)
        cur = c.cursor()
        cur.execute("PRAGMA table_info(watchlist)")
        cols = [r[1] for r in cur.fetchall()]
        if 'triggered_at' not in cols:
            cur.execute("ALTER TABLE watchlist ADD COLUMN triggered_at TIMESTAMP")
            c.commit()
            logger.info("Added triggered_at column to watchlist")
        c.close()
    except Exception as e:
        logger.error(f"triggered_at migration error: {e}")

def get_prev_closes(symbols):
    if not symbols:
        return {}
    today = datetime.now(config.TIMEZONE).strftime('%Y-%m-%d')
    ph = ','.join('?' * len(symbols))
    c = sqlite3.connect(config.DB_FILE)
    rows = c.execute(f'''
        SELECT e.symbol, e.close
        FROM eod_snapshots e
        INNER JOIN (
            SELECT symbol, MAX(trade_date) AS d
            FROM eod_snapshots
            WHERE trade_date < ? AND symbol IN ({ph})
            GROUP BY symbol
        ) m ON e.symbol = m.symbol AND e.trade_date = m.d
    ''', [today] + symbols).fetchall()
    c.close()
    return {r[0]: r[1] for r in rows}

def get_last_two_closes(symbols):
    if not symbols:
        return {}
    ph = ','.join('?' * len(symbols))
    c = sqlite3.connect(config.DB_FILE)
    rows = c.execute(f'''
        WITH r AS (
            SELECT symbol, trade_date, close,
                   ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY trade_date DESC) AS n
            FROM eod_snapshots WHERE symbol IN ({ph})
        )
        SELECT symbol, trade_date, close, n FROM r WHERE n <= 2
    ''', symbols).fetchall()
    c.close()

    out = {}
    for sym, d, cl, n in rows:
        e = out.setdefault(sym, {})
        e['last' if n == 1 else 'prior'] = cl
    return out

def get_eod_volume_stats(symbols):
    if not symbols:
        return {}
    ph = ','.join('?' * len(symbols))
    c = sqlite3.connect(config.DB_FILE)
    rows = c.execute(f'''
        WITH r AS (
            SELECT symbol, trade_date, volume,
                   ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY trade_date DESC) AS n
            FROM eod_snapshots
            WHERE symbol IN ({ph}) AND volume IS NOT NULL AND volume > 0
        )
        SELECT symbol,
               MAX(CASE WHEN n = 1 THEN volume END) AS last_vol,
               AVG(CASE WHEN n BETWEEN 2 AND 11 THEN volume END) AS avg_prior_10d
        FROM r WHERE n <= 11
        GROUP BY symbol
    ''', symbols).fetchall()
    c.close()
    return {r[0]: {"last_vol": r[1], "avg_prior_10d": r[2]} for r in rows}

def persist_bars(rows):
    if not rows:
        return 0
    c = sqlite3.connect(config.DB_FILE)
    for r in rows:
        c.execute('''
            INSERT OR REPLACE INTO eod_snapshots
            (symbol, trade_date, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', r)
    c.commit()
    c.close()
    return len(rows)

def prune_eod():
    cutoff = (datetime.now(config.TIMEZONE).date() - timedelta(days=365)).strftime('%Y-%m-%d')
    c = sqlite3.connect(config.DB_FILE)
    n = c.execute('DELETE FROM eod_snapshots WHERE trade_date < ?', (cutoff,)).rowcount
    c.commit()
    c.close()
    if n:
        logger.info(f"Pruned {n} old EOD rows")

def _watchlist_symbols():
    try:
        c = sqlite3.connect(config.DB_FILE)
        syms = [r[0].upper() for r in c.execute('SELECT DISTINCT symbol FROM watchlist') if r[0]]
        c.close()
        return syms
    except Exception:
        return []

def _read_last_eod_date():
    try:
        c = sqlite3.connect(config.DB_FILE)
        row = c.execute('SELECT MAX(trade_date) FROM eod_snapshots').fetchone()
        c.close()
        if row and row[0]:
            return datetime.strptime(row[0], '%Y-%m-%d').date()
    except Exception:
        pass
    return None

# ------------------------------------------------------------------
#  GITHUB
# ------------------------------------------------------------------
_gh_headers = lambda: {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "User-Agent": "stock-alert",
}

def gh_put(filename, content, message, branch=None):
    if not GITHUB_TOKEN:
        return False
    url = f"{GH_API}/repos/{GITHUB_REPO}/contents/{filename}"
    if branch:
        url += f"?ref={branch}"

    sha = None
    try:
        r = requests.get(url, headers=_gh_headers(), timeout=10)
        if r.status_code == 200:
            sha = r.json().get("sha")
        elif r.status_code != 404:
            return False
    except Exception:
        return False

    body = {
        "message": message,
        "content": base64.b64encode(content.encode()).decode(),
    }
    if sha:
        body["sha"] = sha
    if branch:
        body["branch"] = branch

    try:
        r = requests.put(url, headers=_gh_headers(), json=body, timeout=15)
        return r.status_code in (200, 201)
    except Exception:
        return False

def gh_get(filename, branch=None):
    if not GITHUB_TOKEN:
        return None
    url = f"{GH_API}/repos/{GITHUB_REPO}/contents/{filename}"
    if branch:
        url += f"?ref={branch}"
    try:
        r = requests.get(url, headers=_gh_headers(), timeout=15)
        if r.status_code != 200:
            return None
        return base64.b64decode(r.json().get("content", "")).decode()
    except Exception:
        return None

# ------------------------------------------------------------------
#  NSE SYMBOLS
# ------------------------------------------------------------------
def refresh_nse():
    try:
        r = requests.get(
            "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
            headers={"User-Agent": config.USER_AGENT}, timeout=30)
        r.raise_for_status()
        df = pd.read_csv(StringIO(r.text))
        sym_col = next(c for c in df.columns if 'SYMBOL' in c.upper())
        name_col = next(c for c in df.columns if 'NAME' in c.upper())
        series_col = next((c for c in df.columns if 'SERIES' in c.upper()), None)
        if series_col:
            df = df[df[series_col].str.strip().isin(['EQ', 'BE'])]

        symbols = [{"symbol": row[sym_col].strip(), "name": row[name_col].strip()}
                   for _, row in df.iterrows()]
        if not symbols:
            logger.error("NSE fetch returned empty list")
            return False

        old_content = gh_get(GH_NSE)
        old_symbols = None
        if old_content:
            try:
                old_symbols = json.loads(old_content)
            except Exception:
                old_symbols = None

        changed = (old_symbols != symbols)
        stock_alert.save_nse_symbols(symbols)

        if changed:
            logger.info(f"NSE: {len(symbols)} symbols saved (CHANGED, will push)")
            threading.Thread(
                target=lambda: gh_put(GH_NSE, json.dumps(symbols),
                                      f"NSE symbols: {len(symbols)}"),
                daemon=True).start()
        else:
            logger.info(f"NSE: {len(symbols)} symbols saved (no change, no push)")
        return True
    except Exception as e:
        logger.error(f"NSE fetch failed: {e}")
        return False

# ------------------------------------------------------------------
#  GITHUB RESTORE
# ------------------------------------------------------------------
def restore_nse():
    content = gh_get(GH_NSE)
    if not content:
        return
    try:
        data = json.loads(content)
        if isinstance(data, list) and data:
            stock_alert.save_nse_symbols(data)
            logger.info(f"✅ Restored {len(data)} NSE symbols from GitHub")
    except Exception as e:
        logger.error(f"NSE restore: {e}")

def restore_watchlist():
    c = sqlite3.connect(config.DB_FILE)
    count = c.execute('SELECT COUNT(*) FROM watchlist').fetchone()[0]
    c.close()
    if count > 0:
        logger.info(f"Watchlist already has {count} rows")
        return
    content = gh_get(GH_WATCHLIST)
    if not content:
        return
    try:
        data = json.loads(content)
        c = sqlite3.connect(config.DB_FILE)
        n = 0
        for item in data:
            try:
                c.execute('''
                    INSERT INTO watchlist
                    (symbol, condition, trigger_price, is_active, is_triggered, triggered_at, added_at, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    (item.get('symbol') or '').upper(),
                    item.get('condition', '>'),
                    float(item.get('trigger_price', 0)),
                    int(item.get('is_active', 1)),
                    int(item.get('is_triggered', 0)),
                    item.get('triggered_at'),
                    item.get('added_at') or datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    item.get('notes') or '',
                ))
                n += 1
            except Exception:
                pass
        c.commit()
        c.close()
        logger.info(f"✅ Restored {n} alerts")
    except Exception as e:
        logger.error(f"Watchlist restore: {e}")

def restore_eod():
    content = gh_get(GH_EOD)
    if not content:
        return
    try:
        data = json.loads(content)
        c = sqlite3.connect(config.DB_FILE)
        n = 0
        for item in data:
            try:
                c.execute('''
                    INSERT OR REPLACE INTO eod_snapshots
                    (symbol, trade_date, open, high, low, close, volume)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (item.get('symbol'), item.get('trade_date'),
                      item.get('open'), item.get('high'), item.get('low'),
                      item.get('close'), item.get('volume')))
                n += 1
            except Exception:
                pass
        c.commit()
        c.close()
        logger.info(f"✅ Restored {n} EOD rows")
    except Exception as e:
        logger.error(f"EOD restore: {e}")

def restore_tv_cache():
    content = gh_get(GH_CACHE, branch=GH_CACHE_BRANCH)
    if not content:
        return
    try:
        data = json.loads(content)
        saved = data.get("saved_at", 0)
        age = time.time() - saved
        if age > 1800:
            logger.info(f"TV cache {int(age)}s old — skipping")
            return
        n = stock_alert.cache_restore(data.get("entries", {}), max_age=1800)
        logger.info(f"✅ Restored {n} TV cache entries")
    except Exception as e:
        logger.error(f"TV cache restore: {e}")

# ------------------------------------------------------------------
#  PUSHERS
# ------------------------------------------------------------------
def push_watchlist():
    c = sqlite3.connect(config.DB_FILE)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute('SELECT * FROM watchlist ORDER BY id')]
    c.close()
    if gh_put(GH_WATCHLIST, json.dumps(rows, indent=2, default=str),
              f"Watchlist: {len(rows)} alerts"):
        logger.info(f"Backed up {len(rows)} alerts")

def push_eod():
    cutoff = (datetime.now(config.TIMEZONE).date() - timedelta(days=10)).strftime('%Y-%m-%d')
    c = sqlite3.connect(config.DB_FILE)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        'SELECT symbol, trade_date, open, high, low, close, volume '
        'FROM eod_snapshots WHERE trade_date >= ? ORDER BY symbol, trade_date',
        (cutoff,))]
    c.close()
    if rows:
        gh_put(GH_EOD, json.dumps(rows, default=str),
               f"EOD backup: {len(rows)} rows")

def push_tv_cache():
    entries = stock_alert.cache_snapshot()
    if not entries:
        return
    payload = json.dumps({"saved_at": time.time(), "entries": entries}, default=str)
    if gh_put(GH_CACHE, payload, f"TV cache: {len(entries)}",
              branch=GH_CACHE_BRANCH):
        logger.info(f"TV cache → GitHub ({len(entries)})")

# ------------------------------------------------------------------
#  API ROUTES
# ------------------------------------------------------------------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/search')
def search():
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify([])
    return jsonify(stock_alert.get_search_results(q))

@app.route('/api/alerts')
def api_alerts():
    c = sqlite3.connect(config.DB_FILE)
    c.row_factory = sqlite3.Row
    alerts = [dict(r) for r in c.execute('SELECT * FROM watchlist ORDER BY symbol')]
    c.close()

    if not alerts:
        return jsonify([])

    symbols = list({a['symbol'] for a in alerts})
    tv = stock_alert.get_cached(symbols)
    last_two = get_last_two_closes(symbols)
    eod_vol = get_eod_volume_stats(symbols)
    company_names = stock_alert.get_company_names(symbols)

    no_data = []

    for a in alerts:
        sym = a['symbol']
        e = tv.get(sym, {})
        eod = eod_vol.get(sym, {})
        lt = last_two.get(sym, {})

        cmp_price = e.get('price')
        if cmp_price is None:
            cmp_price = lt.get('last')
        a['cmp'] = round(cmp_price, 2) if cmp_price is not None else None

        if a['cmp'] is None:
            no_data.append(sym)

        if a.get('trigger_price') is not None:
            a['trigger_price'] = round(a['trigger_price'], 2)

        tv_prev = e.get('prev_close')

        if tv_prev is not None and cmp_price is not None:
            a['pct_chg'] = round((cmp_price - tv_prev) / tv_prev * 100, 2) if tv_prev else None
        else:
            last_c  = lt.get('last')
            prior_c = lt.get('prior')
            if last_c and prior_c and prior_c > 0:
                a['pct_chg'] = round((last_c - prior_c) / prior_c * 100, 2)
            else:
                a['pct_chg'] = None

        vol = e.get('volume')
        avg = e.get('avg_vol_10d')
        if not vol or vol == 0:
            vol = eod.get('last_vol')
        if not avg or avg == 0:
            avg = eod.get('avg_prior_10d')

        if vol and avg and avg > 0:
            ratio = vol / avg
            a['vol_pct'] = round(ratio * 100, 1)
            a['rvol'] = round(ratio, 2)
        else:
            a['vol_pct'] = None
            a['rvol'] = None

        a['company_name'] = company_names.get(sym.upper(), '')
        if a.get('notes') is None:
            a['notes'] = ''

    if no_data:
        logger.warning(f"No price data for {len(no_data)} symbols: {no_data}")

    return jsonify(alerts)

def _refresh_new_symbol(sym):
    """Background warm for a newly added symbol: TV + bhavcopy."""
    try:
        stock_alert.refresh_tv_cache([sym])
        logger.info(f"Warm: TV cache updated for {sym}")
    except Exception as e:
        logger.warning(f"TV warm failed for {sym}: {e}")
    try:
        backfill_symbol(sym)
    except Exception as e:
        logger.warning(f"Backfill failed for {sym}: {e}")

@app.route('/api/add', methods=['POST'])
def api_add():
    d = request.json
    sym = d.get('symbol', '').upper()
    cond = d.get('condition')
    price = d.get('price')
    notes = (d.get('notes') or '').strip()
    force_dup = d.get('force_duplicate', False)
    force_trig = d.get('force_trigger', False)

    if not sym or cond not in ('>', '<') or not price:
        return jsonify({'status': 'error', 'message': 'Invalid data'}), 400
    try:
        price = float(price)
    except ValueError:
        return jsonify({'status': 'error', 'message': 'Invalid price'}), 400

    c = db()
    if c.execute('SELECT id FROM watchlist WHERE symbol = ?', (sym,)).fetchone() and not force_dup:
        c.close()
        return jsonify({'status': 'duplicate', 'message': f'{sym} already in list'}), 200

    live = stock_alert.get_cached([sym]).get(sym, {}).get('price')
    if live is not None and not force_trig:
        if (cond == '>' and live > price) or (cond == '<' and live < price):
            c.close()
            return jsonify({'status': 'warning',
                            'message': f'Current {round(live, 2)} already meets condition',
                            'current_price': round(live, 2)}), 200

    try:
        c.execute('INSERT INTO watchlist (symbol, condition, trigger_price, notes) VALUES (?, ?, ?, ?)',
                  (sym, cond, price, notes))
        c.commit()
        c.close()

        threading.Thread(target=_refresh_new_symbol, args=(sym,), daemon=True).start()
        threading.Thread(target=push_watchlist, daemon=True).start()

        return jsonify({'status': 'ok', 'symbol': sym, 'condition': cond, 'price': price})
    except Exception as e:
        c.close()
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/update/<int:aid>', methods=['POST'])
def api_update(aid):
    d = request.json
    c = db()
    row = c.execute('SELECT symbol, is_triggered FROM watchlist WHERE id = ?', (aid,)).fetchone()
    if not row:
        c.close()
        return jsonify({'status': 'error'}), 404
    sym = row['symbol']
    was_trig = (row['is_triggered'] == 1)

    if d.get('price') is not None:
        try:
            c.execute('UPDATE watchlist SET trigger_price = ? WHERE id = ?',
                      (float(d['price']), aid))
        except ValueError:
            c.close()
            return jsonify({'status': 'error', 'message': 'Bad price'}), 400
    if d.get('condition') in ('>', '<'):
        c.execute('UPDATE watchlist SET condition = ? WHERE id = ?', (d['condition'], aid))
    if d.get('notes') is not None:
        c.execute('UPDATE watchlist SET notes = ? WHERE id = ?', (d['notes'].strip(), aid))
    c.commit()

    final = c.execute('SELECT condition, trigger_price, notes FROM watchlist WHERE id = ?',
                      (aid,)).fetchone()
    c.close()

    triggered_now = False
    if was_trig:
        live = stock_alert.get_cached([sym]).get(sym, {}).get('price')
        cond_met = live is not None and (
            (final['condition'] == '>' and live > final['trigger_price']) or
            (final['condition'] == '<' and live < final['trigger_price']))
        c2 = db()
        if cond_met:
            c2.execute('UPDATE watchlist SET is_triggered = 1, is_active = 1 WHERE id = ?', (aid,))
            stock_alert.send_telegram(stock_alert.format_alert({
                'symbol': sym, 'condition': final['condition'],
                'trigger_price': final['trigger_price'], 'notes': final['notes'],
                'pct_chg': None, 'rvol': None,
            }))
            triggered_now = True
        else:
            c2.execute('UPDATE watchlist SET is_triggered = 0, is_active = 1, triggered_at = NULL WHERE id = ?', (aid,))
        c2.commit()
        c2.close()

    threading.Thread(target=push_watchlist, daemon=True).start()
    return jsonify({'status': 'ok', 'triggered_now': triggered_now, 'was_triggered': was_trig})

@app.route('/api/reactivate/<int:aid>', methods=['POST'])
def api_reactivate(aid):
    dry = (request.json or {}).get('dry_run', False)
    c = db()
    a = c.execute('SELECT symbol, condition, trigger_price, notes FROM watchlist WHERE id = ?',
                  (aid,)).fetchone()
    if not a:
        c.close()
        return jsonify({'status': 'error'}), 404

    live = stock_alert.get_cached([a['symbol']]).get(a['symbol'], {}).get('price')
    would = live is not None and (
        (a['condition'] == '>' and live > a['trigger_price']) or
        (a['condition'] == '<' and live < a['trigger_price']))

    if dry:
        c.close()
        return jsonify({'status': 'ok', 'would_trigger': would,
                        'symbol': a['symbol'], 'condition': a['condition'],
                        'trigger_price': a['trigger_price'],
                        'current_price': round(live, 2) if live is not None else None})

    c.execute('UPDATE watchlist SET is_triggered = 0, is_active = 1, triggered_at = NULL WHERE id = ?', (aid,))
    if would:
        c.execute('UPDATE watchlist SET is_triggered = 1, triggered_at = CURRENT_TIMESTAMP WHERE id = ?', (aid,))
    c.commit()
    c.close()

    if would:
        stock_alert.send_telegram(stock_alert.format_alert({
            'symbol': a['symbol'], 'condition': a['condition'],
            'trigger_price': a['trigger_price'], 'notes': a['notes'],
            'pct_chg': None, 'rvol': None,
        }))
    threading.Thread(target=push_watchlist, daemon=True).start()
    return jsonify({'status': 'ok', 'triggered': would})

@app.route('/api/toggle/<int:aid>', methods=['POST'])
def api_toggle(aid):
    c = db()
    row = c.execute('SELECT is_active FROM watchlist WHERE id = ?', (aid,)).fetchone()
    if not row:
        c.close()
        return jsonify({'status': 'error'}), 404
    v = 0 if row['is_active'] else 1
    c.execute('UPDATE watchlist SET is_active = ? WHERE id = ?', (v, aid))
    c.commit()
    c.close()
    threading.Thread(target=push_watchlist, daemon=True).start()
    return jsonify({'status': 'ok', 'is_active': v})

@app.route('/api/delete/<int:aid>', methods=['DELETE'])
def api_delete(aid):
    c = db()
    c.execute('DELETE FROM watchlist WHERE id = ?', (aid,))
    c.commit()
    c.close()
    threading.Thread(target=push_watchlist, daemon=True).start()
    return jsonify({'status': 'ok'})

@app.route('/api/mark_triggered/<int:aid>', methods=['POST'])
def api_mark_triggered(aid):
    c = db()
    c.execute('UPDATE watchlist SET is_triggered = 1, triggered_at = CURRENT_TIMESTAMP WHERE id = ?', (aid,))
    c.commit()
    c.close()
    threading.Thread(target=push_watchlist, daemon=True).start()
    return jsonify({'status': 'ok'})

@app.route('/api/export')
def api_export():
    c = db()
    data = [dict(r) for r in c.execute('SELECT * FROM watchlist')]
    c.close()
    return jsonify(data)

@app.route('/api/import', methods=['POST'])
def api_import():
    data = request.json
    if not isinstance(data, list):
        return jsonify({'status': 'error'}), 400

    c = db()
    c.execute('DELETE FROM watchlist')
    for item in data:
        cond = item.get('condition', '>')
        cond = '>' if cond == '>=' else ('<' if cond == '<=' else cond)
        if cond not in ('>', '<'):
            cond = '>'
        try:
            c.execute('INSERT INTO watchlist (symbol, condition, trigger_price, is_active, is_triggered, triggered_at, notes) '
                      'VALUES (?, ?, ?, ?, ?, ?, ?)',
                      (item.get('symbol', '').upper(), cond, float(item.get('trigger_price', 0)),
                       int(item.get('is_active', 1)), int(item.get('is_triggered', 0)),
                       item.get('triggered_at'),
                       (item.get('notes') or '').strip()))
        except Exception:
            pass
    c.commit()
    c.close()

    syms = list({i.get('symbol', '').upper() for i in data if i.get('symbol')})
    if syms:
        threading.Thread(target=backfill_symbols, args=(syms,), daemon=True).start()
    threading.Thread(target=push_watchlist, daemon=True).start()
    return jsonify({'status': 'ok', 'count': len(data)})

# ------------------------------------------------------------------
#  BACKFILL
# ------------------------------------------------------------------
def backfill_symbol(sym):
    rows = stock_alert.batch_fetch_daily_bars([sym], days=5, include_today=False)
    if rows:
        persist_bars(rows)
        logger.info(f"Backfilled {len(rows)} rows for {sym}")
    else:
        logger.warning(f"Backfill FAILED for {sym}")

def backfill_symbols(symbols):
    rows = stock_alert.batch_fetch_daily_bars(symbols, days=5, include_today=False)
    if rows:
        n = persist_bars(rows)
        covered = {r[0] for r in rows}
        missing = set(s.upper() for s in symbols) - covered
        logger.info(f"Backfilled {n} rows; no data for: {sorted(missing)}")

def ensure_eod_backfill():
    c = sqlite3.connect(config.DB_FILE)
    rows = c.execute('''
        SELECT DISTINCT w.symbol FROM watchlist w
        LEFT JOIN eod_snapshots e ON w.symbol = e.symbol
        WHERE e.symbol IS NULL
    ''').fetchall()
    uncovered = [r[0].upper() for r in rows if r[0]]

    max_row = c.execute('SELECT MAX(trade_date) FROM eod_snapshots').fetchone()
    max_date = max_row[0] if max_row else None
    syms = [r[0].upper() for r in c.execute('SELECT DISTINCT symbol FROM watchlist')]
    c.close()

    if uncovered:
        logger.info(f"Backfilling {len(uncovered)} symbols with no EOD: {uncovered[:10]}")
        threading.Thread(target=backfill_symbols, args=(uncovered,), daemon=True).start()

    today = datetime.now(config.TIMEZONE).date()
    cutoff = (today - timedelta(days=5)).strftime('%Y-%m-%d')
    if max_date and max_date >= cutoff:
        logger.info(f"EOD table fresh (max={max_date})")
        return

    if syms:
        logger.info(f"EOD table stale, backfilling {len(syms)} symbols")
        backfill_symbols(syms)

# ------------------------------------------------------------------
#  BACKGROUND THREADS
# ------------------------------------------------------------------
def startup_thread():
    time.sleep(2)
    restore_nse()
    restore_watchlist()
    restore_eod()
    restore_tv_cache()
    ensure_eod_backfill()
    logger.info("Startup restore complete")

def market_loop():
    time.sleep(30)

    last_tv_push = 0
    last_nse_fetch = datetime.now(config.TIMEZONE).date()
    last_otp_cleanup = 0
    first_run_done = False

    while True:
        try:
            now = datetime.now(config.TIMEZONE)
            today = now.date()
            in_market = (now.weekday() < 5
                         and datetime.strptime(config.START_TIME, "%H:%M").time()
                             <= now.time()
                             <= datetime.strptime(config.STOP_TIME, "%H:%M").time())

            if (not first_run_done) or in_market:
                c = sqlite3.connect(config.DB_FILE)
                syms = [r[0].upper() for r in c.execute('SELECT DISTINCT symbol FROM watchlist')]
                c.close()
                if syms:
                    stock_alert.refresh_tv_cache(syms)
                first_run_done = True

            push_interval = 300 if in_market else 1800
            if time.time() - last_tv_push >= push_interval:
                push_tv_cache()
                last_tv_push = time.time()

            if last_nse_fetch != today:
                refresh_nse()
                last_nse_fetch = today

            if time.time() - last_otp_cleanup >= 300:
                now_t = time.time()
                otp_state = _read_otp_file()
                cleaned = {ip: e for ip, e in otp_state.items() if e.get('expiry', 0) >= now_t}
                if len(cleaned) != len(otp_state):
                    _write_otp_file(cleaned)
                last_otp_cleanup = now_t

            time.sleep(55 if in_market else 300)

        except Exception as e:
            logger.error(f"market_loop error: {e}")
            time.sleep(60)

def eod_fetcher():
    last_completed_date = _read_last_eod_date()
    pending_date = None
    next_attempt_ts = None

    logger.info(f"eod_fetcher: starting, last_completed={last_completed_date}")

    while True:
        try:
            now = datetime.now(config.TIMEZONE)
            today = now.date()

            if pending_date is not None and today > pending_date:
                logger.info(f"eod_fetcher: midnight crossed, Yahoo fallback for {pending_date}")
                _eod_fallback_yahoo(pending_date)
                last_completed_date = pending_date
                pending_date = None
                next_attempt_ts = None
                time.sleep(60)
                continue

            if (pending_date is None
                    and now.weekday() < 5
                    and now.hour >= 19
                    and today != last_completed_date):
                today_str = today.strftime('%Y-%m-%d')
                if today_str in config.NSE_HOLIDAYS:
                    logger.info(f"eod_fetcher: {today_str} is a holiday, skipping")
                    last_completed_date = today
                else:
                    logger.info(f"eod_fetcher: starting EOD fetch for {today_str}")
                    pending_date = today
                    next_attempt_ts = time.time()

            if pending_date is not None and time.time() >= (next_attempt_ts or 0):
                syms = _watchlist_symbols()
                if not syms:
                    logger.info("eod_fetcher: no watchlist symbols")
                    last_completed_date = pending_date
                    pending_date = None
                    next_attempt_ts = None
                else:
                    prev_closes = get_prev_closes(syms)
                    status, rows = stock_alert.fetch_bhavcopy_for_date(
                        pending_date, syms, compare_closes=prev_closes
                    )

                    if status == "unavailable":
                        next_attempt_ts = time.time() + 900
                        logger.info(f"eod_fetcher: bhavcopy for {pending_date} not yet available, "
                                    f"next attempt in 15 min")

                    elif status == "empty":
                        logger.warning(f"eod_fetcher: bhavcopy for {pending_date} has no matches")
                        stock_alert.send_telegram(
                            f"⚠️ EOD bhavcopy for {pending_date.strftime('%Y-%m-%d')} "
                            f"has no rows for our watchlist symbols"
                        )
                        last_completed_date = pending_date
                        pending_date = None
                        next_attempt_ts = None

                    elif status == "stale":
                        logger.info(f"eod_fetcher: bhavcopy for {pending_date} is stale")
                        stock_alert.send_telegram(
                            f"⏭️ EOD skip for {pending_date.strftime('%Y-%m-%d')} — market holiday"
                        )
                        last_completed_date = pending_date
                        pending_date = None
                        next_attempt_ts = None

                    else:
                        n = persist_bars(rows)
                        push_eod()
                        prune_eod()
                        logger.info(f"eod_fetcher: stored {n} rows for {pending_date}")
                        stock_alert.send_telegram(
                            f"✅ EOD captured for {pending_date.strftime('%Y-%m-%d')} — {n} symbols"
                        )
                        last_completed_date = pending_date
                        pending_date = None
                        next_attempt_ts = None

            time.sleep(60)
        except Exception as e:
            logger.error(f"eod_fetcher error: {e}")
            time.sleep(60)

def _eod_fallback_yahoo(target_date):
    date_str = target_date.strftime('%Y-%m-%d')
    try:
        syms = _watchlist_symbols()
        if not syms:
            return
        rows = stock_alert.batch_fetch_daily_bars_yahoo(syms, days=5, include_today=False)
        target_rows = [r for r in rows if r[1] == date_str]
        if target_rows:
            n = persist_bars(target_rows)
            push_eod()
            prune_eod()
            logger.info(f"eod_fetcher: yahoo stored {n} rows for {date_str}")
            stock_alert.send_telegram(
                f"✅ EOD captured (Yahoo fallback) for {date_str} — {n} symbols"
            )
        else:
            logger.warning(f"eod_fetcher: yahoo returned no rows for {date_str}")
            stock_alert.send_telegram(
                f"⚠️ EOD missing for {date_str} — bhavcopy and Yahoo both failed"
            )
    except Exception as e:
        logger.error(f"eod_fetcher yahoo fallback failed: {e}")
        stock_alert.send_telegram(
            f"⚠️ EOD missing for {date_str} — Yahoo fallback error"
        )

def healthcheck_pinger():
    if not HEALTHCHECK_URL:
        return
    time.sleep(60)
    while True:
        try:
            age = time.time() - stock_alert.get_last_tick()
            if age < 900:
                try:
                    requests.get(HEALTHCHECK_URL, timeout=10)
                except Exception:
                    pass
            time.sleep(300)
        except Exception:
            time.sleep(300)

def worker_thread():
    time.sleep(5)
    while True:
        try:
            stock_alert.worker_loop()
        except Exception as e:
            logger.exception(f"Worker crashed, restarting in 30s: {e}")
            time.sleep(30)

# ------------------------------------------------------------------
#  INIT
# ------------------------------------------------------------------
init_db()
migrate_triggered_at_column()

threading.Thread(target=startup_thread,      daemon=True).start()
threading.Thread(target=market_loop,         daemon=True).start()
threading.Thread(target=eod_fetcher,         daemon=True).start()
threading.Thread(target=worker_thread,       daemon=True).start()
threading.Thread(target=healthcheck_pinger,  daemon=True).start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)