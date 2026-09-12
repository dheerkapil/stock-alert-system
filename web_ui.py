import sqlite3
import threading
import time
import logging
import secrets
import random
import requests
import pandas as pd
import json
from io import StringIO
from datetime import datetime, date
from flask import Flask, render_template, request, jsonify, redirect, make_response
import config
import stock_alert

logging.basicConfig(level=config.LOG_LEVEL)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ------------------------------------------------------------------
#  AUTH STATE (in-memory)
# ------------------------------------------------------------------
SESSIONS = {}          # {session_id: expiry_unix_ts}
PENDING_OTP = {}       # {ip: {'code': '1234', 'expiry': ts, 'sent_at': ts, 'attempts': int}}
SESSION_DURATION = 30 * 24 * 3600   # 30 days
OTP_VALIDITY = 300                  # 5 minutes
OTP_THROTTLE = 60                   # 1 minute between OTP requests per IP
MAX_OTP_ATTEMPTS = 5

# ------------------------------------------------------------------
#  GLOBAL CACHE FOR NSE SYMBOLS
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

def is_authenticated():
    sid = request.cookies.get('session_id')
    if not sid:
        return False
    expiry = SESSIONS.get(sid)
    if not expiry:
        return False
    if expiry < time.time():
        SESSIONS.pop(sid, None)
        return False
    return True

def is_valid_worker_key():
    supplied = request.headers.get('X-API-Key', '')
    return bool(config.WORKER_API_KEY) and supplied == config.WORKER_API_KEY

@app.before_request
def check_auth():
    path = request.path

    # Public routes
    if path in ('/login', '/api/send_otp', '/api/verify_otp', '/favicon.ico'):
        return None
    if path.startswith('/static/'):
        return None

    # Worker routes: allow if API key matches
    if path in ('/api/alerts',) or path.startswith('/api/mark_triggered'):
        if is_valid_worker_key():
            return None
        # else fall through to session check

    # Everything else requires a valid session
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
    PENDING_OTP[ip] = {
        'code': code,
        'expiry': now + OTP_VALIDITY,
        'sent_at': now,
        'attempts': 0
    }

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
        stock_alert.send_telegram(
            f"🚨 <b>Too many failed login attempts</b>\n"
            f"IP: <code>{ip}</code>\n"
            f"OTP invalidated."
        )
        return jsonify({'status': 'error', 'message': 'Too many attempts. Request a new OTP.'}), 401

    if entry['code'] != code:
        stock_alert.send_telegram(
            f"❌ <b>Failed login attempt</b>\n"
            f"IP: <code>{ip}</code>\n"
            f"Attempt: {entry['attempts']} of {MAX_OTP_ATTEMPTS}"
        )
        return jsonify({'status': 'error', 'message': 'Invalid code.'}), 401

    # Success
    PENDING_OTP.pop(ip, None)
    session_id = secrets.token_urlsafe(32)
    SESSIONS[session_id] = now + SESSION_DURATION

    ist_time = datetime.now(config.TIMEZONE).strftime('%Y-%m-%d %H:%M:%S IST')
    stock_alert.send_telegram(
        f"✅ <b>Login Success</b>\n"
        f"IP: <code>{ip}</code>\n"
        f"Time: {ist_time}"
    )

    resp = make_response(jsonify({'status': 'ok'}))
    is_https = (request.headers.get('X-Forwarded-Proto', '') == 'https') or request.is_secure
    resp.set_cookie(
        'session_id',
        session_id,
        max_age=SESSION_DURATION,
        httponly=True,
        samesite='Lax',
        secure=is_https,
        path='/'
    )
    logger.info(f"Login success from {ip}")
    return resp

@app.route('/api/logout', methods=['POST'])
def logout():
    sid = request.cookies.get('session_id')
    if sid:
        SESSIONS.pop(sid, None)
    ip = get_client_ip()
    ist_time = datetime.now(config.TIMEZONE).strftime('%Y-%m-%d %H:%M:%S IST')
    stock_alert.send_telegram(
        f"👋 <b>Logout</b>\n"
        f"IP: <code>{ip}</code>\n"
        f"Time: {ist_time}"
    )
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
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_symbol ON watchlist (symbol)')
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
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
        return jsonify({
            'status': 'duplicate',
            'message': f"{symbol} is already in your list."
        }), 200

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
        return jsonify({
            'status': 'warning',
            'message': warning,
            'current_price': current_price
        }), 200

    try:
        conn.execute('INSERT INTO watchlist (symbol, condition, trigger_price) VALUES (?, ?, ?)',
                     (symbol, condition, trigger_price))
        conn.commit()
        conn.close()

        try:
            stock_alert.add_symbol_to_cache(symbol)
        except Exception as e:
            logger.warning(f"Could not pre-cache prev close for {symbol}: {e}")

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

        conn = get_db()
        row = conn.execute('SELECT symbol FROM watchlist WHERE id = ?', (alert_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({'status': 'error', 'message': 'Alert not found'}), 404
        symbol = row['symbol']

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

        conn.commit()
        conn.close()

        final_row = get_db().execute('SELECT condition, trigger_price FROM watchlist WHERE id = ?', (alert_id,)).fetchone()
        return jsonify({
            'status': 'ok',
            'symbol': symbol,
            'condition': final_row['condition'],
            'price': final_row['trigger_price']
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
        alert = conn.execute(
            'SELECT symbol, condition, trigger_price FROM watchlist WHERE id = ?',
            (alert_id,)
        ).fetchone()
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
            return jsonify({
                'status': 'ok',
                'dry_run': True,
                'would_trigger': would_trigger,
                'symbol': alert['symbol'],
                'condition': alert['condition'],
                'trigger_price': alert['trigger_price'],
                'current_price': current_price
            })

        conn.execute('UPDATE watchlist SET is_triggered = 0, is_active = 1 WHERE id = ?', (alert_id,))
        conn.commit()

        if would_trigger:
            conn.execute('UPDATE watchlist SET is_triggered = 1 WHERE id = ?', (alert_id,))
            conn.commit()
            conn.close()
            msg = (f"🔔 ALERT (Reactivated)\n{alert['symbol']} {alert['condition']} {alert['trigger_price']}\nCurrent: {current_price}")
            stock_alert.send_telegram(msg)
            return jsonify({'status': 'ok', 'triggered': True})
        else:
            conn.close()
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
                prices = stock_alert.get_prices(symbols)
                prev_closes = stock_alert.get_prev_closes(symbols)

                for alert in alerts_list:
                    cmp = prices.get(alert['symbol'])
                    alert['cmp'] = cmp

                    prev_close = prev_closes.get(alert['symbol'])
                    if prev_close and cmp:
                        alert['pct_chg'] = ((cmp - prev_close) / prev_close) * 100
                    else:
                        alert['pct_chg'] = None

                    alert['company_name'] = NSE_NAME_LOOKUP.get(alert['symbol'].upper(), '')
            except Exception as e:
                logger.error(f"Failed to fetch prices: {e}")
                for alert in alerts_list:
                    alert['cmp'] = None
                    alert['pct_chg'] = None
                    alert['company_name'] = NSE_NAME_LOOKUP.get(alert['symbol'].upper(), '')
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
            conn.execute('''
                INSERT INTO watchlist (symbol, condition, trigger_price, is_active, is_triggered)
                VALUES (?, ?, ?, ?, ?)
            ''', (
                item.get('symbol', '').upper(),
                cond,
                float(item.get('trigger_price', 0)),
                int(item.get('is_active', 1)),
                int(item.get('is_triggered', 0))
            ))
        conn.commit()
        conn.close()

        try:
            all_symbols = list(set([item.get('symbol', '').upper() for item in data if item.get('symbol')]))
            if all_symbols:
                stock_alert.get_prev_closes(all_symbols)
        except Exception as e:
            logger.warning(f"Could not pre-cache prev closes after import: {e}")

        return jsonify({'status': 'ok', 'count': len(data)})
    except Exception as e:
        logger.error(f"Import error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ------------------------------------------------------------------
#  BACKGROUND THREADS
# ------------------------------------------------------------------
def daily_cache_warmer():
    last_run_date = None
    while True:
        try:
            now = datetime.now(config.TIMEZONE)
            today = now.date()
            if (now.hour == 8 and now.minute < 5 and last_run_date != today):
                logger.info("🕗 8 AM: warming previous-close cache...")
                try:
                    conn = sqlite3.connect(config.DB_FILE)
                    rows = conn.execute('SELECT DISTINCT symbol FROM watchlist').fetchall()
                    conn.close()
                    symbols = [r[0].upper() for r in rows if r[0]]
                    if symbols:
                        stock_alert.get_prev_closes(symbols)
                        logger.info(f"✅ Cache warmed for {len(symbols)} symbols.")
                    last_run_date = today
                except Exception as e:
                    logger.error(f"Cache warmer failed: {e}")
            time.sleep(60)
        except Exception as e:
            logger.error(f"Cache warmer error: {e}")
            time.sleep(60)

def cleanup_sessions():
    while True:
        try:
            now = time.time()
            expired_sessions = [k for k, v in list(SESSIONS.items()) if v < now]
            for k in expired_sessions:
                SESSIONS.pop(k, None)
            expired_otps = [k for k, v in list(PENDING_OTP.items()) if v.get('expiry', 0) < now]
            for k in expired_otps:
                PENDING_OTP.pop(k, None)
            time.sleep(300)
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
            time.sleep(300)

def start_worker():
    time.sleep(5)
    stock_alert.main()

threading.Thread(target=start_worker, daemon=True).start()
threading.Thread(target=daily_cache_warmer, daemon=True).start()
threading.Thread(target=cleanup_sessions, daemon=True).start()

# ------------------------------------------------------------------
#  INIT
# ------------------------------------------------------------------
init_db()
migrate_conditions()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)