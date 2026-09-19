import os
import sqlite3
import threading
import time
import logging
import secrets
import random
import hmac
import hashlib
import base64
import requests
import pandas as pd
import json
from io import StringIO
from datetime import datetime, date, timedelta
from flask import Flask, render_template, request, jsonify, redirect, make_response
import config
import stock_alert

logging.basicConfig(level=config.LOG_LEVEL)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ------------------------------------------------------------------
#  SESSION SECRET
# ------------------------------------------------------------------
SESSION_SECRET_KEY = os.environ.get("SESSION_SECRET_KEY", "change-this-to-a-long-random-string")

# ------------------------------------------------------------------
#  GITHUB AUTO-BACKUP
# ------------------------------------------------------------------
GITHUB_TOKEN = os.environ.get("GITHUB_BACKUP_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "dheerkapil/stock-alert-system")
GITHUB_BACKUP_FILE = os.environ.get("GITHUB_BACKUP_FILE", "watchlist_backup.json")
GITHUB_API = "https://api.github.com"
BACKUP_DEBOUNCE_SECONDS = 5
BACKUP_FAILURE_ALERT_THRESHOLD = 3

_backup_timer = None
_backup_lock = threading.Lock()
_backup_failures = 0
_backup_alert_sent = False

# ------------------------------------------------------------------
#  AUTH STATE
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
#  NSE SYMBOLS CACHE
# ------------------------------------------------------------------
NSE_SYMBOLS = []
NSE_NAME_LOOKUP = {}

def refresh_nse_symbols():
    global NSE_SYMBOLS, NSE_NAME_LOOKUP
    url = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
    headers = {"User-Agent": config.USER_AGENT}
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        df = pd.read_csv(StringIO(resp.text))
        symbol_col = next((c for c in df.columns if 'SYMBOL' in c.upper()), None)
        name_col = next((c for c in df.columns if 'NAME' in c.upper()), None)
        series_col = next((c for c in df.columns if 'SERIES' in c.upper()), None)
        if not symbol_col or not name_col:
            logger.error("Could not find columns in NSE CSV")
            return
        if series_col:
            df[series_col] = df[series_col].str.strip()
            df = df[df[series_col].isin(['EQ', 'BE'])]
        NSE_SYMBOLS = [
            {"symbol": row[symbol_col].strip(), "name": row[name_col].strip()}
            for _, row in df.iterrows()
        ]
        NSE_NAME_LOOKUP = {item['symbol'].upper(): item['name'] for item in NSE_SYMBOLS}
        logger.info(f"Cached {len(NSE_SYMBOLS)} symbols.")
    except Exception as e:
        logger.error(f"Failed to fetch NSE symbols: {e}")
        NSE_SYMBOLS = []
        NSE_NAME_LOOKUP = {}

refresh_nse_symbols()

# ------------------------------------------------------------------
#  AUTH HELPERS
# ------------------------------------------------------------------
def get_client_ip():
    xff = request.headers.get('X-Forwarded-For', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.remote_addr or 'unknown'

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
    if not sid:
        return False
    return verify_session_token(sid)

def is_valid_worker_key():
    supplied = request.headers.get('X-API-Key', '')
    return bool(config.WORKER_API_KEY) and supplied == config.WORKER_API_KEY

@app.before_request
def check_auth():
    path = request.path
    if path in ('/login', '/api/send_otp', '/api/verify_otp', '/favicon.ico'):
        return None
    if path.startswith('/static/'):
        return None
    if path in ('/api/alerts',) or path.startswith('/api/mark_triggered'):
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
        remaining = int(OTP_THROTTLE - (now - existing['sent_at']))
        return jsonify({'status': 'error', 'message': f'Please wait {remaining}s before requesting again.'}), 429
    code = f"{random.randint(0, 9999):04d}"
    PENDING_OTP[ip] = {'code': code, 'expiry': now + OTP_VALIDITY, 'sent_at': now, 'attempts': 0}
    msg = (f"🔐 <b>Login OTP Requested</b>\n"
           f"Code: <b>{code}</b>\n"
           f"IP: <code>{ip}</code>\n"
           f"Valid for 5 minutes.")
    stock_alert.send_telegram(msg)
    logger.info(f"OTP sent to Telegram for IP {ip}")
    return jsonify({'status': 'ok', 'message': 'OTP sent to Telegram'})

@app.route('/api/verify_otp', methods=['POST'])
def verify_otp():
    data = request.json or {}
    code = str(data.get('code', '')).strip()
    ip = get_client_ip()
    now = time.time()
    entry = PENDING_OTP.get(ip)
    if not entry or entry['expiry'] < now:
        return jsonify({'status': 'error', 'message': 'No OTP requested or expired. Request a new one.'}), 401
    entry['attempts'] = entry.get('attempts', 0) + 1
    if entry['attempts'] > MAX_OTP_ATTEMPTS:
        PENDING_OTP.pop(ip, None)
        stock_alert.send_telegram(f"🚨 <b>Too many failed login attempts</b>\nIP: <code>{ip}</code>\nOTP invalidated.")
        return jsonify({'status': 'error', 'message': 'Too many attempts. Request a new OTP.'}), 401
    if entry['code'] != code:
        stock_alert.send_telegram(f"❌ <b>Failed login attempt</b>\nIP: <code>{ip}</code>\nAttempt: {entry['attempts']} of {MAX_OTP_ATTEMPTS}")
        return jsonify({'status': 'error', 'message': 'Invalid code.'}), 401
    PENDING_OTP.pop(ip, None)
    session_id = make_session_token()
    ist_time = datetime.now(config.TIMEZONE).strftime('%Y-%m-%d %H:%M:%S IST')
    stock_alert.send_telegram(f"✅ <b>Login Success</b>\nIP: <code>{ip}</code>\nTime: {ist_time}")
    resp = make_response(jsonify({'status': 'ok'}))
    is_https = (request.headers.get('X-Forwarded-Proto', '') == 'https') or request.is_secure
    resp.set_cookie('session_id', session_id, max_age=SESSION_DURATION,
                    httponly=True, samesite='Lax', secure=is_https, path='/')
    logger.info(f"Login success from {ip}")
    return resp

@app.route('/api/logout', methods=['POST'])
def logout():
    ip = get_client_ip()
    ist_time = datetime.now(config.TIMEZONE).strftime('%Y-%m-%d %H:%M:%S IST')
    stock_alert.send_telegram(f"👋 <b>Logout</b>\nIP: <code>{ip}</code>\nTime: {ist_time}")
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
    try:
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
    except Exception as e:
        logger.error(f"Database init error: {e}")

def migrate_conditions():
    try:
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
        logger.info("Migrating condition operators: >= → >, <= → < ...")
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
                CASE WHEN condition = '>=' THEN '>'
                     WHEN condition = '<=' THEN '<'
                     ELSE condition END,
                trigger_price, is_active, is_triggered, added_at
            FROM watchlist
        ''')
        c.execute('DROP TABLE watchlist')
        c.execute('ALTER TABLE watchlist_new RENAME TO watchlist')
        c.execute('CREATE INDEX IF NOT EXISTS idx_symbol ON watchlist (symbol)')
        c.execute('COMMIT')
        logger.info("✅ Migration completed.")
        conn.close()
    except Exception as e:
        logger.error(f"Migration error: {e}")

def migrate_notes_column():
    try:
        conn = sqlite3.connect(config.DB_FILE)
        c = conn.cursor()
        c.execute("PRAGMA table_info(watchlist)")
        cols = [r[1] for r in c.fetchall()]
        if 'notes' not in cols:
            logger.info("Adding 'notes' column to watchlist...")
            c.execute("ALTER TABLE watchlist ADD COLUMN notes TEXT DEFAULT ''")
            conn.commit()
            logger.info("✅ notes column added.")
        conn.close()
    except Exception as e:
        logger.error(f"Notes migration error: {e}")

# ------------------------------------------------------------------
#  EOD PERSISTENCE
# ------------------------------------------------------------------
def _persist_eod_snapshots(eod_data):
    """Insert today's EOD bar per symbol. Safe to re-run (REPLACE)."""
    if not eod_data:
        return
    today_str = datetime.now(config.TIMEZONE).strftime('%Y-%m-%d')
    try:
        conn = sqlite3.connect(config.DB_FILE)
        inserted = 0
        for sym, bar in eod_data.items():
            if not bar or bar.get('close') is None:
                continue
            conn.execute('''
                INSERT OR REPLACE INTO eod_snapshots
                (symbol, trade_date, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (sym, today_str, bar.get('open'), bar.get('high'),
                  bar.get('low'), bar.get('close'), bar.get('volume')))
            inserted += 1
        conn.commit()
        conn.close()
        logger.info(f"✅ Persisted {inserted} EOD snapshots for {today_str}.")
    except Exception as e:
        logger.error(f"EOD persist failed: {e}")

def _prune_eod_snapshots():
    """Delete EOD rows older than EOD_RETENTION_DAYS."""
    try:
        cutoff = (datetime.now(config.TIMEZONE).date() - timedelta(days=EOD_RETENTION_DAYS)).strftime('%Y-%m-%d')
        conn = sqlite3.connect(config.DB_FILE)
        cur = conn.execute('DELETE FROM eod_snapshots WHERE trade_date < ?', (cutoff,))
        removed = cur.rowcount
        conn.commit()
        conn.close()
        if removed:
            logger.info(f"🧹 Pruned {removed} EOD rows older than {cutoff}.")
    except Exception as e:
        logger.error(f"EOD prune failed: {e}")

# ------------------------------------------------------------------
#  GITHUB AUTO-BACKUP
# ------------------------------------------------------------------
def _alert_backup_failure(reason):
    global _backup_alert_sent
    if _backup_alert_sent:
        return
    _backup_alert_sent = True
    msg = (f"⚠️ <b>GitHub Backup Failing</b>\n"
           f"Reason: {reason}\n"
           f"Backups have failed {_backup_failures} times in a row.\n"
           f"Likely cause: GITHUB_BACKUP_TOKEN expired or revoked.\n"
           f"Action: generate a new fine-grained token and update the env var on Render.")
    stock_alert.send_telegram(msg)
    logger.error(f"Backup failure alert sent to Telegram: {reason}")

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
    global _backup_timer
    try:
        conn = sqlite3.connect(config.DB_FILE)
        conn.row_factory = sqlite3.Row
        rows = conn.execute('SELECT * FROM watchlist ORDER BY id').fetchall()
        conn.close()
        data = [dict(r) for r in rows]
        content = json.dumps(data, indent=2, default=str)
        _push_to_github(content)
    except Exception as e:
        logger.error(f"Backup failed: {e}")
        global _backup_failures
        _backup_failures += 1
        if _backup_failures >= BACKUP_FAILURE_ALERT_THRESHOLD:
            _alert_backup_failure(str(e))
    finally:
        with _backup_lock:
            _backup_timer = None

def _push_to_github(content):
    global _backup_failures, _backup_alert_sent
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{GITHUB_BACKUP_FILE}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "stock-alert-backup",
    }

    sha = None
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            sha = r.json().get("sha")
        elif r.status_code == 401:
            _backup_failures += 1
            if _backup_failures >= BACKUP_FAILURE_ALERT_THRESHOLD:
                _alert_backup_failure("401 Unauthorized on GET")
            return
        elif r.status_code != 404:
            logger.warning(f"GitHub GET returned {r.status_code}: {r.text[:200]}")
            _backup_failures += 1
            if _backup_failures >= BACKUP_FAILURE_ALERT_THRESHOLD:
                _alert_backup_failure(f"HTTP {r.status_code} on GET")
            return
    except Exception as e:
        logger.error(f"GitHub GET failed: {e}")
        _backup_failures += 1
        if _backup_failures >= BACKUP_FAILURE_ALERT_THRESHOLD:
            _alert_backup_failure(f"GET exception: {e}")
        return

    try:
        alert_count = len(json.loads(content))
    except Exception:
        alert_count = 0

    body = {
        "message": f"Auto-backup: {alert_count} alerts",
        "content": base64.b64encode(content.encode('utf-8')).decode('ascii'),
    }
    if sha:
        body["sha"] = sha

    try:
        r = requests.put(url, headers=headers, json=body, timeout=15)
        if r.status_code in (200, 201):
            logger.info(f"Backed up {alert_count} alerts to GitHub.")
            if _backup_failures > 0:
                logger.info("Backup succeeded after previous failures — resetting counter.")
            _backup_failures = 0
            _backup_alert_sent = False
        else:
            logger.error(f"GitHub PUT failed {r.status_code}: {r.text[:300]}")
            _backup_failures += 1
            if _backup_failures >= BACKUP_FAILURE_ALERT_THRESHOLD:
                reason = "401 Unauthorized on PUT" if r.status_code == 401 else f"HTTP {r.status_code} on PUT"
                _alert_backup_failure(reason)
    except Exception as e:
        logger.error(f"GitHub PUT exception: {e}")
        _backup_failures += 1
        if _backup_failures >= BACKUP_FAILURE_ALERT_THRESHOLD:
            _alert_backup_failure(f"PUT exception: {e}")

def restore_from_github():
    if not GITHUB_TOKEN:
        logger.info("No GITHUB_BACKUP_TOKEN set — skipping restore.")
        return
    try:
        conn = sqlite3.connect(config.DB_FILE)
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM watchlist')
        count = c.fetchone()[0]
        conn.close()

        url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{GITHUB_BACKUP_FILE}"
        headers = {
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "stock-alert-backup",
        }
        r = requests.get(url, headers=headers, timeout=15)

        if r.status_code == 401:
            stock_alert.send_telegram(
                "🚨 <b>GitHub Backup Token Invalid</b>\n"
                "The GITHUB_BACKUP_TOKEN is expired or revoked.\n"
                "Backups will fail until a new token is configured.\n"
                "Action: generate a new fine-grained token and update the env var on Render."
            )
            logger.error("Startup: GitHub token returned 401.")
            return

        if r.status_code != 200:
            logger.warning(f"Restore: GitHub returned {r.status_code}. No backup to restore.")
            return

        if count > 0:
            logger.info(f"DB already has {count} alerts — skipping restore.")
            return

        content_b64 = r.json().get("content", "")
        decoded = base64.b64decode(content_b64).decode('utf-8')
        data = json.loads(decoded)
        if not isinstance(data, list):
            logger.error("Restore: Backup file is not a list.")
            return

        conn = sqlite3.connect(config.DB_FILE)
        c = conn.cursor()
        restored = 0
        for item in data:
            try:
                c.execute('''
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
    query = request.args.get('q', '').strip().upper()
    if len(query) < 1:
        return jsonify([])
    results = []
    for item in NSE_SYMBOLS:
        symbol = item['symbol']
        name = item['name']
        if query in symbol or query in name.upper():
            results.append(item)
            if len(results) >= 50:
                break
    return jsonify(results)

@app.route('/api/price/<symbol>')
def get_price(symbol):
    try:
        prices = stock_alert.get_prices([symbol.upper()])
        if symbol in prices:
            return jsonify({"symbol": symbol, "price": prices[symbol]})
        else:
            return jsonify({"symbol": symbol, "price": None}), 404
    except Exception as e:
        logger.error(f"Price fetch error: {e}")
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
    existing = conn.execute('SELECT id FROM watchlist WHERE symbol = ?', (symbol,)).fetchone()
    if existing and not force_duplicate:
        conn.close()
        return jsonify({'status': 'duplicate', 'message': f"{symbol} is already in your list."}), 200

    current_price = None
    try:
        prices = stock_alert.get_prices([symbol])
        current_price = prices.get(symbol)
    except Exception as e:
        logger.warning(f"Could not fetch price for {symbol}: {e}")

    warning = None
    if current_price is not None:
        if condition == '>' and current_price > trigger_price:
            warning = f"Current price is {current_price}, which already meets the condition."
        elif condition == '<' and current_price < trigger_price:
            warning = f"Current price is {current_price}, which already meets the condition."

    if warning and not force_trigger:
        conn.close()
        return jsonify({'status': 'warning', 'message': warning, 'current_price': current_price}), 200

    try:
        conn.execute('INSERT INTO watchlist (symbol, condition, trigger_price, notes) VALUES (?, ?, ?, ?)',
                     (symbol, condition, trigger_price, notes))
        conn.commit()
        conn.close()

        def _warm_new_symbol(sym):
            try:
                stock_alert.add_symbol_to_cache(sym)
            except Exception as e:
                logger.warning(f"prev-close warm failed for {sym}: {e}")
            try:
                stock_alert.get_avg_volumes([sym])
            except Exception as e:
                logger.warning(f"avg-volume warm failed for {sym}: {e}")

        threading.Thread(target=_warm_new_symbol, args=(symbol,), daemon=True).start()
        schedule_backup()
        return jsonify({'status': 'ok', 'symbol': symbol, 'condition': condition, 'price': trigger_price})
    except Exception as e:
        conn.close()
        logger.error(f"Add alert DB error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/update/<int:alert_id>', methods=['POST'])
def update_alert(alert_id):
    try:
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
                new_price = float(new_price)
                conn.execute('UPDATE watchlist SET trigger_price = ? WHERE id = ?', (new_price, alert_id))
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
        final_row = conn.execute('SELECT condition, trigger_price, notes FROM watchlist WHERE id = ?', (alert_id,)).fetchone()
        final_condition = final_row['condition']
        final_price = final_row['trigger_price']
        final_notes = final_row['notes']
        conn.close()

        triggered_now = False

        if was_triggered:
            current_price = None
            try:
                prices = stock_alert.get_prices([symbol])
                current_price = prices.get(symbol)
            except Exception as e:
                logger.warning(f"Could not fetch price for {symbol} during edit re-eval: {e}")

            condition_met = False
            if current_price is not None:
                if final_condition == '>' and current_price > final_price:
                    condition_met = True
                elif final_condition == '<' and current_price < final_price:
                    condition_met = True

            conn2 = get_db()
            if condition_met:
                conn2.execute('UPDATE watchlist SET is_triggered = 1, is_active = 1 WHERE id = ?', (alert_id,))
                conn2.commit()
                conn2.close()
                msg = (f"🔔 ALERT (Edited)\n{symbol} {final_condition} {final_price}\nCurrent: {current_price}")
                stock_alert.send_telegram(msg)
                triggered_now = True
            else:
                conn2.execute('UPDATE watchlist SET is_triggered = 0, is_active = 1 WHERE id = ?', (alert_id,))
                conn2.commit()
                conn2.close()

        schedule_backup()
        return jsonify({
            'status': 'ok',
            'symbol': symbol,
            'condition': final_condition,
            'price': final_price,
            'notes': final_notes,
            'triggered_now': triggered_now,
            'was_triggered': was_triggered
        })
    except Exception as e:
        logger.error(f"Error in /api/update: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/reactivate/<int:alert_id>', methods=['POST'])
def reactivate_alert(alert_id):
    try:
        data = request.json or {}
        dry_run = data.get('dry_run', False)
        conn = get_db()
        alert = conn.execute('SELECT symbol, condition, trigger_price FROM watchlist WHERE id = ?', (alert_id,)).fetchone()
        if not alert:
            conn.close()
            return jsonify({'status': 'error', 'message': 'Alert not found'}), 404
        prices = stock_alert.get_prices([alert['symbol']])
        current_price = prices.get(alert['symbol'])
        would_trigger = False
        if current_price is not None:
            if alert['condition'] == '>' and current_price > alert['trigger_price']:
                would_trigger = True
            elif alert['condition'] == '<' and current_price < alert['trigger_price']:
                would_trigger = True
        if dry_run:
            conn.close()
            return jsonify({'status': 'ok', 'dry_run': True, 'would_trigger': would_trigger,
                            'symbol': alert['symbol'], 'condition': alert['condition'],
                            'trigger_price': alert['trigger_price'], 'current_price': current_price})
        conn.execute('UPDATE watchlist SET is_triggered = 0, is_active = 1 WHERE id = ?', (alert_id,))
        conn.commit()
        if would_trigger:
            conn.execute('UPDATE watchlist SET is_triggered = 1 WHERE id = ?', (alert_id,))
            conn.commit()
            conn.close()
            msg = (f"🔔 ALERT (Reactivated)\n{alert['symbol']} {alert['condition']} {alert['trigger_price']}\nCurrent: {current_price}")
            stock_alert.send_telegram(msg)
            schedule_backup()
            return jsonify({'status': 'ok', 'triggered': True})
        else:
            conn.close()
            schedule_backup()
            return jsonify({'status': 'ok', 'triggered': False})
    except Exception as e:
        logger.error(f"Error in /api/reactivate: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/alerts')
def get_alerts():
    try:
        conn = get_db()
        alerts = conn.execute('SELECT * FROM watchlist ORDER BY symbol').fetchall()
        conn.close()
        alerts_list = [dict(row) for row in alerts]
        if alerts_list:
            symbols = list(set(a['symbol'] for a in alerts_list))
            try:
                pv = stock_alert.get_prices_with_volume(symbols)
                prev_closes = stock_alert.get_prev_closes_cached_only(symbols)
                avg_volumes = stock_alert.get_avg_volumes_cached_only(symbols)

                for alert in alerts_list:
                    entry = pv.get(alert['symbol'], {})
                    cmp = entry.get('price')
                    current_vol = entry.get('volume')
                    alert['cmp'] = cmp

                    prev_close = prev_closes.get(alert['symbol'])
                    if prev_close and cmp:
                        alert['pct_chg'] = ((cmp - prev_close) / prev_close) * 100
                    else:
                        alert['pct_chg'] = None

                    avg_vol = avg_volumes.get(alert['symbol'])
                    if current_vol and avg_vol and avg_vol > 0:
                        alert['vol_pct'] = (current_vol / avg_vol) * 100
                    else:
                        alert['vol_pct'] = None

                    alert['company_name'] = NSE_NAME_LOOKUP.get(alert['symbol'].upper(), '')
                    if alert.get('notes') is None:
                        alert['notes'] = ''
            except Exception as e:
                logger.error(f"Failed to fetch prices: {e}")
                for alert in alerts_list:
                    alert['cmp'] = None
                    alert['pct_chg'] = None
                    alert['vol_pct'] = None
                    alert['company_name'] = NSE_NAME_LOOKUP.get(alert['symbol'].upper(), '')
                    if alert.get('notes') is None:
                        alert['notes'] = ''
        return jsonify(alerts_list)
    except Exception as e:
        logger.error(f"Error in /api/alerts: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/toggle/<int:alert_id>', methods=['POST'])
def toggle_alert(alert_id):
    try:
        conn = get_db()
        cur = conn.execute('SELECT is_active FROM watchlist WHERE id = ?', (alert_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            return jsonify({'status': 'error'}), 404
        new_val = 0 if row['is_active'] else 1
        conn.execute('UPDATE watchlist SET is_active = ? WHERE id = ?', (new_val, alert_id))
        conn.commit()
        conn.close()
        schedule_backup()
        return jsonify({'status': 'ok', 'is_active': new_val})
    except Exception as e:
        logger.error(f"Error in /api/toggle: {e}")
        return jsonify({'status': 'error'}), 500

@app.route('/api/mark_triggered/<int:alert_id>', methods=['POST'])
def mark_triggered(alert_id):
    try:
        conn = get_db()
        conn.execute('UPDATE watchlist SET is_triggered = 1 WHERE id = ?', (alert_id,))
        conn.commit()
        conn.close()
        schedule_backup()
        return jsonify({'status': 'ok'})
    except Exception as e:
        logger.error(f"Error in /api/mark_triggered: {e}")
        return jsonify({'status': 'error'}), 500

@app.route('/api/delete/<int:alert_id>', methods=['DELETE'])
def delete_alert(alert_id):
    try:
        conn = get_db()
        conn.execute('DELETE FROM watchlist WHERE id = ?', (alert_id,))
        conn.commit()
        conn.close()
        schedule_backup()
        return jsonify({'status': 'ok'})
    except Exception as e:
        logger.error(f"Error in /api/delete: {e}")
        return jsonify({'status': 'error'}), 500

@app.route('/api/export')
def export_alerts():
    try:
        conn = get_db()
        alerts = conn.execute('SELECT * FROM watchlist').fetchall()
        conn.close()
        data = [dict(row) for row in alerts]
        return jsonify(data)
    except Exception as e:
        logger.error(f"Export error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/import', methods=['POST'])
def import_alerts():
    try:
        data = request.json
        if not isinstance(data, list):
            return jsonify({'status': 'error', 'message': 'Invalid data format'}), 400
        conn = get_db()
        conn.execute('DELETE FROM watchlist')
        for item in data:
            cond = item.get('condition', '>')
            if cond == '>=':
                cond = '>'
            elif cond == '<=':
                cond = '<'
            if cond not in ('>', '<'):
                cond = '>'

            raw_added_at = item.get('added_at')
            added_at = None
            if raw_added_at:
                try:
                    s = str(raw_added_at).strip()
                    parsed = None
                    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M:%S.%f',
                                '%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d'):
                        try:
                            parsed = datetime.strptime(s, fmt)
                            break
                        except ValueError:
                            continue
                    if parsed is None:
                        s2 = s.replace('Z', '+00:00')
                        parsed = datetime.fromisoformat(s2).replace(tzinfo=None)
                    added_at = parsed.strftime('%Y-%m-%d %H:%M:%S')
                except Exception as e:
                    logger.warning(f"Could not parse added_at '{raw_added_at}': {e}")
                    added_at = None

            notes = (item.get('notes') or '').strip()

            if added_at:
                conn.execute('''
                    INSERT INTO watchlist (symbol, condition, trigger_price, is_active, is_triggered, added_at, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (item.get('symbol', '').upper(), cond, float(item.get('trigger_price', 0)),
                      int(item.get('is_active', 1)), int(item.get('is_triggered', 0)), added_at, notes))
            else:
                conn.execute('''
                    INSERT INTO watchlist (symbol, condition, trigger_price, is_active, is_triggered, notes)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (item.get('symbol', '').upper(), cond, float(item.get('trigger_price', 0)),
                      int(item.get('is_active', 1)), int(item.get('is_triggered', 0)), notes))
        conn.commit()
        conn.close()

        def _warm_after_import(symbols):
            try:
                stock_alert.get_prev_closes(symbols)
            except Exception as e:
                logger.warning(f"prev-close warm after import failed: {e}")
            try:
                stock_alert.get_avg_volumes(symbols)
            except Exception as e:
                logger.warning(f"avg-volume warm after import failed: {e}")

        try:
            all_symbols = list(set([item.get('symbol', '').upper() for item in data if item.get('symbol')]))
            if all_symbols:
                threading.Thread(target=_warm_after_import, args=(all_symbols,), daemon=True).start()
        except Exception as e:
            logger.warning(f"Could not start warm after import: {e}")

        schedule_backup()
        return jsonify({'status': 'ok', 'count': len(data)})
    except Exception as e:
        logger.error(f"Import error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ------------------------------------------------------------------
#  BACKGROUND THREADS
# ------------------------------------------------------------------
def _warm_reference_data(symbols):
    if not symbols:
        return
    try:
        stock_alert.get_prev_closes(symbols)
    except Exception as e:
        logger.error(f"prev-close warm failed: {e}")
    try:
        stock_alert.get_avg_volumes(symbols)
    except Exception as e:
        logger.error(f"avg-volume warm failed: {e}")

def daily_cache_warmer():
    last_run_date = None
    while True:
        try:
            now = datetime.now(config.TIMEZONE)
            today = now.date()
            if (now.hour == 8 and now.minute < 5 and last_run_date != today):
                logger.info("🕗 8 AM: warming reference caches...")
                try:
                    conn = sqlite3.connect(config.DB_FILE)
                    rows = conn.execute('SELECT DISTINCT symbol FROM watchlist').fetchall()
                    conn.close()
                    symbols = [r[0].upper() for r in rows if r[0]]
                    if symbols:
                        _warm_reference_data(symbols)
                        logger.info(f"✅ Cache warmed for {len(symbols)} symbols.")
                    last_run_date = today
                except Exception as e:
                    logger.error(f"Cache warmer failed: {e}")
            time.sleep(60)
        except Exception as e:
            logger.error(f"Cache warmer error: {e}")
            time.sleep(60)

def reference_data_refresher():
    """Keeps prev-close + avg-volume caches warm. Never fetches already-cached symbols."""
    time.sleep(60)
    while True:
        try:
            conn = sqlite3.connect(config.DB_FILE)
            rows = conn.execute('SELECT DISTINCT symbol FROM watchlist').fetchall()
            conn.close()
            symbols = [r[0].upper() for r in rows if r[0]]
            if symbols:
                logger.info(f"Reference refresher: warming {len(symbols)} symbols")
                _warm_reference_data(symbols)
                logger.info("Reference refresher: done")
            time.sleep(1800)
        except Exception as e:
            logger.exception(f"Reference refresher error: {e}")
            time.sleep(300)

def eod_fetcher():
    """
    Fires once per weekday at/after 4:30 PM IST.
    Pulls today's completed OHLCV bar, refreshes caches, persists to SQLite, prunes old rows.
    """
    last_run_date = None
    while True:
        try:
            now = datetime.now(config.TIMEZONE)
            today = now.date()

            if (now.weekday() < 5
                    and now.hour == 16 and now.minute >= 30
                    and last_run_date != today):

                logger.info("🕟 4:30 PM: fetching end-of-day data...")
                try:
                    conn = sqlite3.connect(config.DB_FILE)
                    rows = conn.execute('SELECT DISTINCT symbol FROM watchlist').fetchall()
                    conn.close()
                    symbols = [r[0].upper() for r in rows if r[0]]

                    if symbols:
                        eod = stock_alert.batch_fetch_eod(symbols)

                        refreshed = 0
                        for sym, bar in eod.items():
                            if bar and bar.get('close') is not None:
                                stock_alert._PREV_CLOSE_CACHE[sym] = bar['close']
                                refreshed += 1
                        logger.info(f"✅ EOD: refreshed {refreshed}/{len(symbols)} prev closes.")

                        _persist_eod_snapshots(eod)
                        _prune_eod_snapshots()

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
            expired_otps = [k for k, v in list(PENDING_OTP.items()) if v.get('expiry', 0) < now]
            for k in expired_otps:
                PENDING_OTP.pop(k, None)
            time.sleep(300)
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
            time.sleep(300)

def start_worker():
    time.sleep(5)
    while True:
        try:
            stock_alert.main()
        except Exception as e:
            logger.exception(f"Worker thread died, restarting in 30s: {e}")
            time.sleep(30)

threading.Thread(target=start_worker, daemon=True).start()
threading.Thread(target=daily_cache_warmer, daemon=True).start()
threading.Thread(target=reference_data_refresher, daemon=True).start()
threading.Thread(target=eod_fetcher, daemon=True).start()
threading.Thread(target=cleanup_sessions, daemon=True).start()

# ------------------------------------------------------------------
#  INIT
# ------------------------------------------------------------------
init_db()
migrate_conditions()
migrate_notes_column()
restore_from_github()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)