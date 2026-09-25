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

logging.basicConfig(level=config.LOG_LEVEL)
logger = logging.getLogger(__name__)

app = Flask(__name__)

SESSION_SECRET_KEY = os.environ.get("SESSION_SECRET_KEY", "change-this-to-a-long-random-string")

# ------------------------------------------------------------------
#  GITHUB BACKUP
# ------------------------------------------------------------------
GITHUB_TOKEN         = os.environ.get("GITHUB_BACKUP_TOKEN", "")
GITHUB_REPO          = os.environ.get("GITHUB_REPO", "dheerkapil/stock-alert-system")
GITHUB_BACKUP_FILE   = os.environ.get("GITHUB_BACKUP_FILE", "watchlist_backup.json")
GITHUB_EOD_FILE      = os.environ.get("GITHUB_EOD_FILE", "eod_backup.json")
GITHUB_API           = "https://api.github.com"
BACKUP_DEBOUNCE_SECONDS       = 5
BACKUP_FAILURE_ALERT_THRESHOLD = 3
EOD_BACKUP_DAYS               = 10

_backup_timer = None
_backup_lock = threading.Lock()
_backup_failures = 0
_backup_alert_sent = False

# ------------------------------------------------------------------
#  AUTH CONSTANTS
# ------------------------------------------------------------------
PENDING_OTP = {}
SESSION_DURATION = 30 * 24 * 3600
OTP_VALIDITY = 300
OTP_THROTTLE = 60
MAX_OTP_ATTEMPTS = 5

# ------------------------------------------------------------------
#  EOD RETENTION
# ------------------------------------------------------------------
EOD_RETENTION_DAYS = 365

# ------------------------------------------------------------------
#  NSE SYMBOL CACHE
# ------------------------------------------------------------------
NSE_SYMBOLS = []
NSE_NAME_LOOKUP = {}

def refresh_nse_symbols():
    global NSE_SYMBOLS, NSE_NAME_LOOKUP
    url = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
    try:
        resp = requests.get(url, headers={"User-Agent": config.USER_AGENT}, timeout=30)
        resp.raise_for_status()
        df = pd.read_csv(StringIO(resp.text))
        sym_col    = next((c for c in df.columns if 'SYMBOL' in c.upper()), None)
        name_col   = next((c for c in df.columns if 'NAME'   in c.upper()), None)
        series_col = next((c for c in df.columns if 'SERIES' in c.upper()), None)
        if not sym_col or not name_col:
            logger.error("NSE CSV: missing columns")
            return
        if series_col:
            df[series_col] = df[series_col].str.strip()
            df = df[df[series_col].isin(['EQ', 'BE'])]
        NSE_SYMBOLS = [
            {"symbol": r[sym_col].strip(), "name": r[name_col].strip()}
            for _, r in df.iterrows()
        ]
        NSE_NAME_LOOKUP = {i['symbol'].upper(): i['name'] for i in NSE_SYMBOLS}
        logger.info(f"Cached {len(NSE_SYMBOLS)} symbols.")
    except Exception as e:
        logger.error(f"NSE symbols fetch failed: {e}")

refresh_nse_symbols()

# ------------------------------------------------------------------
#  AUTH HELPERS
# ------------------------------------------------------------------
def get_client_ip():
    xff = request.headers.get('X-Forwarded-For', '')
    return xff.split(',')[0].strip() if xff else (request.remote_addr or 'unknown')

def make_session_token():
    expiry = int(time.time()) + SESSION_DURATION
    payload = str(expiry)
    sig = hmac.new(SESSION_SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"

def verify_session_token(token):
    try:
        payload, sig = token.rsplit(".", 1)
        expected = hmac.new(SESSION_SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return False
        return int(payload) > time.time()
    except Exception:
        return False

def is_authenticated():
    sid = request.cookies.get('session_id')
    return verify_session_token(sid) if sid else False

def is_valid_worker_key():
    return bool(config.WORKER_API_KEY) and request.headers.get('X-API-Key', '') == config.WORKER_API_KEY

@app.before_request
def check_auth():
    path = request.path
    if path in ('/login', '/api/send_otp', '/api/verify_otp', '/favicon.ico'):
        return None
    if path.startswith('/static/'):
        return None
    if path == '/api/alerts' or path.startswith('/api/mark_triggered'):
        if is_valid_worker_key():
            return None
    if not is_authenticated():
        if path.startswith('/api/'):
            return jsonify({'error': 'Unauthorized'}), 401
        return redirect('/login')
    return None

# ------------------------------------------------------------------
#  AUTH ROUTES
# ------------------------------------------------------------------
@app.route('/login')
def login_page():
    if is_authenticated():
        return redirect('/')
    return render_template('login.html')

@app.route('/api/send_otp', methods=['POST'])
def send_otp():
    ip = get_client_ip()
    now = time.time()
    existing = PENDING_OTP.get(ip)
    if existing and now - existing.get('sent_at', 0) < OTP_THROTTLE:
        wait = int(OTP_THROTTLE - (now - existing['sent_at']))
        return jsonify({'status': 'error', 'message': f'Wait {wait}s before retrying.'}), 429

    code = f"{random.randint(0, 9999):04d}"
    PENDING_OTP[ip] = {'code': code, 'expiry': now + OTP_VALIDITY, 'sent_at': now, 'attempts': 0}

    stock_alert.send_telegram(
        f"🔐 <b>Login OTP</b>\nCode: <b>{code}</b>\nIP: <code>{ip}</code>\nValid 5 min."
    )
    logger.info(f"OTP sent for IP {ip}")
    return jsonify({'status': 'ok'})

@app.route('/api/verify_otp', methods=['POST'])
def verify_otp():
    data = request.json or {}
    code = str(data.get('code', '')).strip()
    ip = get_client_ip()
    now = time.time()
    entry = PENDING_OTP.get(ip)

    if not entry or entry['expiry'] < now:
        return jsonify({'status': 'error', 'message': 'No OTP or expired. Request a new one.'}), 401

    entry['attempts'] = entry.get('attempts', 0) + 1
    if entry['attempts'] > MAX_OTP_ATTEMPTS:
        PENDING_OTP.pop(ip, None)
        stock_alert.send_telegram(f"🚨 Too many failed login attempts. IP: <code>{ip}</code>")
        return jsonify({'status': 'error', 'message': 'Too many attempts.'}), 401

    if entry['code'] != code:
        stock_alert.send_telegram(f"❌ Failed login. IP: <code>{ip}</code> Attempt {entry['attempts']}/{MAX_OTP_ATTEMPTS}")
        return jsonify({'status': 'error', 'message': 'Invalid code.'}), 401

    PENDING_OTP.pop(ip, None)
    session_id = make_session_token()
    ist = datetime.now(config.TIMEZONE).strftime('%Y-%m-%d %H:%M:%S IST')
    stock_alert.send_telegram(f"✅ Login success\nIP: <code>{ip}</code>\nTime: {ist}")

    resp = make_response(jsonify({'status': 'ok'}))
    is_https = (request.headers.get('X-Forwarded-Proto') == 'https') or request.is_secure
    resp.set_cookie('session_id', session_id, max_age=SESSION_DURATION,
                    httponly=True, samesite='Lax', secure=is_https, path='/')
    return resp

@app.route('/api/logout', methods=['POST'])
def logout():
    ip = get_client_ip()
    ist = datetime.now(config.TIMEZONE).strftime('%Y-%m-%d %H:%M:%S IST')
    stock_alert.send_telegram(f"👋 Logout\nIP: <code>{ip}</code>\nTime: {ist}")
    resp = make_response(jsonify({'status': 'ok'}))
    resp.set_cookie('session_id', '', max_age=0, path='/')
    return resp

# ------------------------------------------------------------------
#  DATABASE
# ------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(config.DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = sqlite3.connect(config.DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            condition TEXT NOT NULL CHECK(condition IN ('>', '<')),
            trigger_price REAL NOT NULL,
            is_active INTEGER DEFAULT 1,
            is_triggered INTEGER DEFAULT 0,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            notes TEXT DEFAULT ''
        )
    ''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_symbol ON watchlist (symbol)')

    c.execute('''
        CREATE TABLE IF NOT EXISTS eod_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            UNIQUE(symbol, trade_date)
        )
    ''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_eod_symbol_date ON eod_snapshots (symbol, trade_date)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_eod_date ON eod_snapshots (trade_date)')

    conn.commit()
    conn.close()
    logger.info("Database initialized.")

def migrate_conditions():
    conn = sqlite3.connect(config.DB_FILE)
    c = conn.cursor()
    c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='watchlist'")
    row = c.fetchone()
    if not row:
        conn.close()
        return
    schema = row[0]
    if "'>='" not in schema and "'<='" not in schema:
        conn.close()
        return

    logger.info("Migrating condition operators: >= → >, <= → <")
    c.execute('BEGIN TRANSACTION')
    c.execute('''
        CREATE TABLE watchlist_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            condition TEXT NOT NULL CHECK(condition IN ('>', '<')),
            trigger_price REAL NOT NULL,
            is_active INTEGER DEFAULT 1,
            is_triggered INTEGER DEFAULT 0,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            notes TEXT DEFAULT ''
        )
    ''')
    c.execute('''
        INSERT INTO watchlist_new (id, symbol, condition, trigger_price, is_active, is_triggered, added_at)
        SELECT id, symbol,
            CASE WHEN condition = '>=' THEN '>' WHEN condition = '<=' THEN '<' ELSE condition END,
            trigger_price, is_active, is_triggered, added_at
        FROM watchlist
    ''')
    c.execute('DROP TABLE watchlist')
    c.execute('ALTER TABLE watchlist_new RENAME TO watchlist')
    c.execute('CREATE INDEX IF NOT EXISTS idx_symbol ON watchlist (symbol)')
    c.execute('COMMIT')
    conn.close()
    logger.info("✅ Migration completed.")

def migrate_notes_column():
    conn = sqlite3.connect(config.DB_FILE)
    c = conn.cursor()
    c.execute("PRAGMA table_info(watchlist)")
    cols = [r[1] for r in c.fetchall()]
    if 'notes' not in cols:
        c.execute("ALTER TABLE watchlist ADD COLUMN notes TEXT DEFAULT ''")
        conn.commit()
        logger.info("✅ notes column added.")
    conn.close()

# ------------------------------------------------------------------
#  PREV CLOSE — read from eod_snapshots (SQL)
# ------------------------------------------------------------------
def get_prev_closes_from_db(symbols):
    """
    Most recent completed close before today, per symbol.
    Returns {symbol: close}. Missing symbols simply absent from result.
    """
    if not symbols:
        return {}
    symbols = [s.upper() for s in symbols]
    today_str = datetime.now(config.TIMEZONE).strftime('%Y-%m-%d')
    placeholders = ','.join('?' * len(symbols))

    conn = sqlite3.connect(config.DB_FILE)
    rows = conn.execute(f'''
        SELECT e.symbol, e.close
        FROM eod_snapshots e
        INNER JOIN (
            SELECT symbol, MAX(trade_date) AS max_date
            FROM eod_snapshots
            WHERE trade_date < ? AND symbol IN ({placeholders})
            GROUP BY symbol
        ) latest
        ON e.symbol = latest.symbol AND e.trade_date = latest.max_date
    ''', [today_str] + symbols).fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows}

# ------------------------------------------------------------------
#  EOD PERSISTENCE + BACKFILL
# ------------------------------------------------------------------
def _persist_bars(rows):
    if not rows:
        return 0
    conn = sqlite3.connect(config.DB_FILE)
    for (sym, trade_date, o, h, l, c, v) in rows:
        conn.execute('''
            INSERT OR REPLACE INTO eod_snapshots
            (symbol, trade_date, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (sym, trade_date, o, h, l, c, v))
    conn.commit()
    conn.close()
    return len(rows)

def _prune_eod_snapshots():
    cutoff = (datetime.now(config.TIMEZONE).date() - timedelta(days=EOD_RETENTION_DAYS)).strftime('%Y-%m-%d')
    conn = sqlite3.connect(config.DB_FILE)
    removed = conn.execute('DELETE FROM eod_snapshots WHERE trade_date < ?', (cutoff,)).rowcount
    conn.commit()
    conn.close()
    if removed:
        logger.info(f"🧹 Pruned {removed} EOD rows older than {cutoff}.")

def backfill_symbols(symbols):
    """Fetch and store last 5 days of bars for the given symbols."""
    if not symbols:
        return
    rows = stock_alert.batch_fetch_daily_bars(symbols, days=5, include_today=False)
    if not rows:
        logger.warning(f"Backfill returned no data for {len(symbols)} symbols.")
        return
    n = _persist_bars(rows)
    logger.info(f"✅ Backfilled {n} bars for {len(symbols)} symbols.")
    _push_eod_to_github()

def ensure_eod_backfill():
    """If eod_snapshots is empty or stale, backfill from Yahoo."""
    conn = sqlite3.connect(config.DB_FILE)
    row = conn.execute('SELECT MAX(trade_date) FROM eod_snapshots').fetchone()
    max_date = row[0] if row else None
    symbols = [r[0].upper() for r in conn.execute('SELECT DISTINCT symbol FROM watchlist').fetchall() if r[0]]
    conn.close()

    if not symbols:
        logger.info("Watchlist empty. Nothing to backfill.")
        return

    today = datetime.now(config.TIMEZONE).date()
    cutoff = (today - timedelta(days=5)).strftime('%Y-%m-%d')

    if max_date and max_date >= cutoff:
        logger.info(f"EOD table is fresh (max={max_date}). Skipping backfill.")
        return

    logger.info(f"EOD table stale or empty (max={max_date}). Backfilling {len(symbols)} symbols...")
    backfill_symbols(symbols)

# ------------------------------------------------------------------
#  GITHUB BACKUP — WATCHLIST + EOD
# ------------------------------------------------------------------
def schedule_backup():
    global _backup_timer
    if not GITHUB_TOKEN:
        return
    with _backup_lock:
        if _backup_timer is not None:
            _backup_timer.cancel()
        _backup_timer = threading.Timer(BACKUP_DEBOUNCE_SECONDS, _do_backup)
        _backup_timer.daemon = True
        _backup_timer.start()

def _do_backup():
    global _backup_timer, _backup_failures
    try:
        conn = sqlite3.connect(config.DB_FILE)
        conn.row_factory = sqlite3.Row
        rows = conn.execute('SELECT * FROM watchlist ORDER BY id').fetchall()
        conn.close()
        _push_to_github(json.dumps([dict(r) for r in rows], indent=2, default=str))
    except Exception as e:
        logger.error(f"Backup failed: {e}")
        _backup_failures += 1
        if _backup_failures >= BACKUP_FAILURE_ALERT_THRESHOLD:
            _alert_backup_failure(str(e))
    finally:
        with _backup_lock:
            _backup_timer = None

def _alert_backup_failure(reason):
    global _backup_alert_sent
    if _backup_alert_sent:
        return
    _backup_alert_sent = True
    stock_alert.send_telegram(
        f"⚠️ <b>GitHub Backup Failing</b>\n"
        f"Reason: {reason}\nConsecutive failures: {_backup_failures}"
    )

def _github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "stock-alert-backup",
    }

def _github_put(filename, content, message):
    """Push content to a file in the GitHub repo. Returns True on success."""
    global _backup_failures, _backup_alert_sent
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{filename}"
    headers = _github_headers()

    sha = None
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            sha = r.json().get("sha")
        elif r.status_code != 404:
            _backup_failures += 1
            if _backup_failures >= BACKUP_FAILURE_ALERT_THRESHOLD:
                _alert_backup_failure(f"HTTP {r.status_code} on GET {filename}")
            return False
    except Exception as e:
        _backup_failures += 1
        if _backup_failures >= BACKUP_FAILURE_ALERT_THRESHOLD:
            _alert_backup_failure(f"GET {filename}: {e}")
        return False

    body = {
        "message": message,
        "content": base64.b64encode(content.encode('utf-8')).decode('ascii'),
    }
    if sha:
        body["sha"] = sha

    try:
        r = requests.put(url, headers=headers, json=body, timeout=15)
        if r.status_code in (200, 201):
            _backup_failures = 0
            _backup_alert_sent = False
            return True
        _backup_failures += 1
        if _backup_failures >= BACKUP_FAILURE_ALERT_THRESHOLD:
            _alert_backup_failure(f"HTTP {r.status_code} on PUT {filename}")
        return False
    except Exception as e:
        _backup_failures += 1
        if _backup_failures >= BACKUP_FAILURE_ALERT_THRESHOLD:
            _alert_backup_failure(f"PUT {filename}: {e}")
        return False

def _push_to_github(content):
    try:
        count = len(json.loads(content))
    except Exception:
        count = 0
    if _github_put(GITHUB_BACKUP_FILE, content, f"Auto-backup: {count} alerts"):
        logger.info(f"Backed up {count} alerts to GitHub.")

def _push_eod_to_github():
    """Push the last EOD_BACKUP_DAYS days of eod_snapshots to GitHub."""
    if not GITHUB_TOKEN:
        return
    try:
        cutoff = (datetime.now(config.TIMEZONE).date() - timedelta(days=EOD_BACKUP_DAYS)).strftime('%Y-%m-%d')
        conn = sqlite3.connect(config.DB_FILE)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            'SELECT symbol, trade_date, open, high, low, close, volume '
            'FROM eod_snapshots WHERE trade_date >= ? ORDER BY symbol, trade_date',
            (cutoff,)
        ).fetchall()
        conn.close()

        if not rows:
            return

        payload = json.dumps([dict(r) for r in rows], default=str)
        if _github_put(GITHUB_EOD_FILE, payload, f"EOD backup: {len(rows)} rows"):
            logger.info(f"Backed up {len(rows)} EOD rows to GitHub.")
    except Exception as e:
        logger.error(f"EOD backup failed: {e}")

def _restore_eod_from_github():
    """Read eod_backup.json from GitHub and load rows into eod_snapshots."""
    if not GITHUB_TOKEN:
        return
    try:
        url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{GITHUB_EOD_FILE}"
        r = requests.get(url, headers=_github_headers(), timeout=15)
        if r.status_code == 404:
            logger.info("No EOD backup on GitHub yet.")
            return
        if r.status_code != 200:
            logger.warning(f"EOD restore: GitHub returned {r.status_code}.")
            return

        decoded = base64.b64decode(r.json().get("content", "")).decode('utf-8')
        data = json.loads(decoded)
        if not isinstance(data, list):
            return

        conn = sqlite3.connect(config.DB_FILE)
        inserted = 0
        for item in data:
            try:
                conn.execute('''
                    INSERT OR REPLACE INTO eod_snapshots
                    (symbol, trade_date, open, high, low, close, volume)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (
                    item.get('symbol'),
                    item.get('trade_date'),
                    item.get('open'),
                    item.get('high'),
                    item.get('low'),
                    item.get('close'),
                    item.get('volume'),
                ))
                inserted += 1
            except Exception as e:
                logger.warning(f"EOD restore skip {item.get('symbol')}/{item.get('trade_date')}: {e}")
        conn.commit()
        conn.close()
        logger.info(f"✅ Restored {inserted} EOD rows from GitHub backup.")
    except Exception as e:
        logger.error(f"EOD restore failed: {e}")

def restore_from_github():
    if not GITHUB_TOKEN:
        return
    try:
        conn = sqlite3.connect(config.DB_FILE)
        count = conn.execute('SELECT COUNT(*) FROM watchlist').fetchone()[0]
        conn.close()

        url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{GITHUB_BACKUP_FILE}"
        r = requests.get(url, headers=_github_headers(), timeout=15)

        if r.status_code == 401:
            stock_alert.send_telegram("🚨 GitHub backup token is invalid (401). Backups will fail.")
            return
        if r.status_code != 200:
            logger.warning(f"Restore: GitHub returned {r.status_code}.")
            return
        if count > 0:
            logger.info(f"Watchlist has {count} rows — skipping restore.")
            return

        decoded = base64.b64decode(r.json().get("content", "")).decode('utf-8')
        data = json.loads(decoded)
        if not isinstance(data, list):
            return

        conn = sqlite3.connect(config.DB_FILE)
        restored = 0
        for item in data:
            try:
                conn.execute('''
                    INSERT INTO watchlist (symbol, condition, trigger_price, is_active, is_triggered, added_at, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (
                    (item.get('symbol') or '').upper(),
                    item.get('condition', '>'),
                    float(item.get('trigger_price', 0)),
                    int(item.get('is_active', 1)),
                    int(item.get('is_triggered', 0)),
                    item.get('added_at') or datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    item.get('notes') or '',
                ))
                restored += 1
            except Exception as e:
                logger.warning(f"Restore skip {item.get('symbol')}: {e}")
        conn.commit()
        conn.close()
        logger.info(f"✅ Restored {restored} alerts from GitHub backup.")
    except Exception as e:
        logger.error(f"Restore failed: {e}")

# ------------------------------------------------------------------
#  API ROUTES
# ------------------------------------------------------------------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/search')
def search_symbols():
    q = request.args.get('q', '').strip().upper()
    if not q:
        return jsonify([])
    out = []
    for item in NSE_SYMBOLS:
        if q in item['symbol'] or q in item['name'].upper():
            out.append(item)
            if len(out) >= 50:
                break
    return jsonify(out)

@app.route('/api/price/<symbol>')
def get_price(symbol):
    try:
        prices = stock_alert.get_prices([symbol.upper()])
        return jsonify({"symbol": symbol, "price": prices.get(symbol)})
    except Exception as e:
        return jsonify({"symbol": symbol, "error": str(e)}), 500

@app.route('/api/add', methods=['POST'])
def add_alert():
    data = request.json
    symbol = data.get('symbol', '').upper()
    condition = data.get('condition')
    trigger_price = data.get('price')
    notes = (data.get('notes') or '').strip()
    force_duplicate = data.get('force_duplicate', False)
    force_trigger = data.get('force_trigger', False)

    if not symbol or condition not in ('>', '<') or not trigger_price:
        return jsonify({'status': 'error', 'message': 'Invalid data'}), 400
    try:
        trigger_price = float(trigger_price)
    except ValueError:
        return jsonify({'status': 'error', 'message': 'Invalid price'}), 400

    conn = get_db()
    if conn.execute('SELECT id FROM watchlist WHERE symbol = ?', (symbol,)).fetchone() and not force_duplicate:
        conn.close()
        return jsonify({'status': 'duplicate', 'message': f"{symbol} already in list."}), 200

    current_price = None
    try:
        current_price = stock_alert.get_prices([symbol]).get(symbol)
    except Exception:
        pass

    if current_price is not None and not force_trigger:
        if condition == '>' and current_price > trigger_price:
            conn.close()
            return jsonify({'status': 'warning',
                            'message': f"Current price {current_price} already meets the condition."}), 200
        if condition == '<' and current_price < trigger_price:
            conn.close()
            return jsonify({'status': 'warning',
                            'message': f"Current price {current_price} already meets the condition."}), 200

    try:
        conn.execute('INSERT INTO watchlist (symbol, condition, trigger_price, notes) VALUES (?, ?, ?, ?)',
                     (symbol, condition, trigger_price, notes))
        conn.commit()
        conn.close()
        threading.Thread(target=backfill_symbols, args=([symbol],), daemon=True).start()
        schedule_backup()
        return jsonify({'status': 'ok', 'symbol': symbol, 'condition': condition, 'price': trigger_price})
    except Exception as e:
        conn.close()
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/update/<int:alert_id>', methods=['POST'])
def update_alert(alert_id):
    data = request.json
    new_price = data.get('price')
    new_condition = data.get('condition')
    new_notes = data.get('notes')

    conn = get_db()
    row = conn.execute('SELECT symbol, is_triggered FROM watchlist WHERE id = ?', (alert_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Alert not found'}), 404
    symbol = row['symbol']
    was_triggered = (row['is_triggered'] == 1)

    if new_price is not None:
        try:
            conn.execute('UPDATE watchlist SET trigger_price = ? WHERE id = ?', (float(new_price), alert_id))
        except ValueError:
            conn.close()
            return jsonify({'status': 'error', 'message': 'Invalid price'}), 400
    if new_condition is not None:
        if new_condition not in ('>', '<'):
            conn.close()
            return jsonify({'status': 'error', 'message': 'Invalid condition'}), 400
        conn.execute('UPDATE watchlist SET condition = ? WHERE id = ?', (new_condition, alert_id))
    if new_notes is not None:
        conn.execute('UPDATE watchlist SET notes = ? WHERE id = ?', (new_notes.strip(), alert_id))

    conn.commit()
    final = conn.execute('SELECT condition, trigger_price, notes FROM watchlist WHERE id = ?', (alert_id,)).fetchone()
    conn.close()

    triggered_now = False
    if was_triggered:
        current_price = None
        try:
            current_price = stock_alert.get_prices([symbol]).get(symbol)
        except Exception:
            pass

        cond_met = (
            current_price is not None and (
                (final['condition'] == '>' and current_price > final['trigger_price']) or
                (final['condition'] == '<' and current_price < final['trigger_price'])
            )
        )

        conn2 = get_db()
        if cond_met:
            conn2.execute('UPDATE watchlist SET is_triggered = 1, is_active = 1 WHERE id = ?', (alert_id,))
            stock_alert.send_telegram(
                f"🔔 ALERT (Edited)\n{symbol} {final['condition']} {final['trigger_price']}\nCurrent: {current_price}"
            )
            triggered_now = True
        else:
            conn2.execute('UPDATE watchlist SET is_triggered = 0, is_active = 1 WHERE id = ?', (alert_id,))
        conn2.commit()
        conn2.close()

    schedule_backup()
    return jsonify({
        'status': 'ok', 'symbol': symbol,
        'condition': final['condition'], 'price': final['trigger_price'],
        'notes': final['notes'],
        'triggered_now': triggered_now, 'was_triggered': was_triggered,
    })

@app.route('/api/reactivate/<int:alert_id>', methods=['POST'])
def reactivate_alert(alert_id):
    data = request.json or {}
    dry_run = data.get('dry_run', False)

    conn = get_db()
    alert = conn.execute('SELECT symbol, condition, trigger_price FROM watchlist WHERE id = ?', (alert_id,)).fetchone()
    if not alert:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Alert not found'}), 404

    try:
        current_price = stock_alert.get_prices([alert['symbol']]).get(alert['symbol'])
    except Exception:
        current_price = None

    would_trigger = (
        current_price is not None and (
            (alert['condition'] == '>' and current_price > alert['trigger_price']) or
            (alert['condition'] == '<' and current_price < alert['trigger_price'])
        )
    )

    if dry_run:
        conn.close()
        return jsonify({
            'status': 'ok', 'dry_run': True, 'would_trigger': would_trigger,
            'symbol': alert['symbol'], 'condition': alert['condition'],
            'trigger_price': alert['trigger_price'], 'current_price': current_price,
        })

    conn.execute('UPDATE watchlist SET is_triggered = 0, is_active = 1 WHERE id = ?', (alert_id,))
    if would_trigger:
        conn.execute('UPDATE watchlist SET is_triggered = 1 WHERE id = ?', (alert_id,))
    conn.commit()
    conn.close()

    if would_trigger:
        stock_alert.send_telegram(
            f"🔔 ALERT (Reactivated)\n{alert['symbol']} {alert['condition']} {alert['trigger_price']}\nCurrent: {current_price}"
        )
    schedule_backup()
    return jsonify({'status': 'ok', 'triggered': would_trigger})

@app.route('/api/alerts')
def get_alerts():
    conn = get_db()
    alerts = [dict(r) for r in conn.execute('SELECT * FROM watchlist ORDER BY symbol').fetchall()]
    conn.close()

    if not alerts:
        return jsonify([])

    symbols = list({a['symbol'] for a in alerts})
    pv = stock_alert.get_prices_with_volume(symbols)
    prev_closes = get_prev_closes_from_db(symbols)

    for a in alerts:
        entry = pv.get(a['symbol'], {})
        cmp = entry.get('price')
        a['cmp'] = cmp
        a['rvol'] = entry.get('rvol')

        vol      = entry.get('volume')
        avg_vol  = entry.get('avg_vol_10d')
        a['vol_pct'] = (vol / avg_vol * 100) if (vol and avg_vol and avg_vol > 0) else None

        prev_close = prev_closes.get(a['symbol'])
        a['pct_chg'] = ((cmp - prev_close) / prev_close * 100) if (cmp is not None and prev_close) else None

        a['company_name'] = NSE_NAME_LOOKUP.get(a['symbol'].upper(), '')
        if a.get('notes') is None:
            a['notes'] = ''

    return jsonify(alerts)

@app.route('/api/toggle/<int:alert_id>', methods=['POST'])
def toggle_alert(alert_id):
    conn = get_db()
    row = conn.execute('SELECT is_active FROM watchlist WHERE id = ?', (alert_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'status': 'error'}), 404
    new_val = 0 if row['is_active'] else 1
    conn.execute('UPDATE watchlist SET is_active = ? WHERE id = ?', (new_val, alert_id))
    conn.commit()
    conn.close()
    schedule_backup()
    return jsonify({'status': 'ok', 'is_active': new_val})

@app.route('/api/mark_triggered/<int:alert_id>', methods=['POST'])
def mark_triggered(alert_id):
    conn = get_db()
    conn.execute('UPDATE watchlist SET is_triggered = 1 WHERE id = ?', (alert_id,))
    conn.commit()
    conn.close()
    schedule_backup()
    return jsonify({'status': 'ok'})

@app.route('/api/delete/<int:alert_id>', methods=['DELETE'])
def delete_alert(alert_id):
    conn = get_db()
    conn.execute('DELETE FROM watchlist WHERE id = ?', (alert_id,))
    conn.commit()
    conn.close()
    schedule_backup()
    return jsonify({'status': 'ok'})

@app.route('/api/export')
def export_alerts():
    conn = get_db()
    data = [dict(r) for r in conn.execute('SELECT * FROM watchlist').fetchall()]
    conn.close()
    return jsonify(data)

@app.route('/api/import', methods=['POST'])
def import_alerts():
    data = request.json
    if not isinstance(data, list):
        return jsonify({'status': 'error', 'message': 'Invalid data'}), 400

    conn = get_db()
    conn.execute('DELETE FROM watchlist')
    for item in data:
        cond = item.get('condition', '>')
        cond = '>' if cond == '>=' else ('<' if cond == '<=' else cond)
        if cond not in ('>', '<'):
            cond = '>'

        raw = item.get('added_at')
        added_at = None
        if raw:
            try:
                s = str(raw).strip()
                for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d'):
                    try:
                        added_at = datetime.strptime(s, fmt).strftime('%Y-%m-%d %H:%M:%S')
                        break
                    except ValueError:
                        continue
            except Exception:
                added_at = None

        notes = (item.get('notes') or '').strip()
        cols = '(symbol, condition, trigger_price, is_active, is_triggered, notes' + (', added_at' if added_at else '') + ')'
        vals = [item.get('symbol', '').upper(), cond, float(item.get('trigger_price', 0)),
                int(item.get('is_active', 1)), int(item.get('is_triggered', 0)), notes]
        if added_at:
            vals.append(added_at)
        conn.execute(f'INSERT INTO watchlist {cols} VALUES ({",".join("?" * len(vals))})', vals)
    conn.commit()
    conn.close()

    all_symbols = list({item.get('symbol', '').upper() for item in data if item.get('symbol')})
    if all_symbols:
        threading.Thread(target=backfill_symbols, args=(all_symbols,), daemon=True).start()

    schedule_backup()
    return jsonify({'status': 'ok', 'count': len(data)})

# ------------------------------------------------------------------
#  BACKGROUND THREADS
# ------------------------------------------------------------------
def start_worker():
    time.sleep(5)
    while True:
        try:
            stock_alert.main()
        except Exception as e:
            logger.exception(f"Worker died, restarting in 30s: {e}")
            time.sleep(30)

def eod_fetcher():
    """Once per weekday at/after 4:30 PM IST: fetch today's bar, persist, backup."""
    last_run_date = None
    while True:
        try:
            now = datetime.now(config.TIMEZONE)
            today = now.date()

            if (now.weekday() < 5 and now.hour >= 16 and now.minute >= 30
                    and last_run_date != today):
                logger.info("🕟 4:30 PM EOD fetch starting...")
                try:
                    conn = sqlite3.connect(config.DB_FILE)
                    symbols = [r[0].upper() for r in conn.execute('SELECT DISTINCT symbol FROM watchlist').fetchall() if r[0]]
                    conn.close()

                    if symbols:
                        rows = stock_alert.batch_fetch_daily_bars(symbols, days=5, include_today=True)
                        n = _persist_bars(rows)
                        logger.info(f"✅ EOD: stored {n} rows for {len(symbols)} symbols.")
                        _prune_eod_snapshots()
                        _push_eod_to_github()
                    last_run_date = today
                except Exception as e:
                    logger.error(f"EOD fetch failed: {e}")

            time.sleep(60)
        except Exception as e:
            logger.error(f"EOD fetcher error: {e}")
            time.sleep(60)

def cleanup_sessions():
    while True:
        try:
            now = time.time()
            for k in [k for k, v in list(PENDING_OTP.items()) if v.get('expiry', 0) < now]:
                PENDING_OTP.pop(k, None)
            time.sleep(300)
        except Exception:
            time.sleep(300)

# ------------------------------------------------------------------
#  INIT  — synchronous DB setup, then background threads
# ------------------------------------------------------------------
init_db()
migrate_conditions()
migrate_notes_column()
restore_from_github()
_restore_eod_from_github()

threading.Thread(target=ensure_eod_backfill, daemon=True).start()
threading.Thread(target=start_worker,        daemon=True).start()
threading.Thread(target=eod_fetcher,         daemon=True).start()
threading.Thread(target=cleanup_sessions,    daemon=True).start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)