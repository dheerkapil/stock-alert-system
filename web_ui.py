import sqlite3
import threading
import time
import logging
import requests
import pandas as pd
from io import StringIO
from flask import Flask, render_template, request, jsonify
import config
import stock_alert

logging.basicConfig(level=config.LOG_LEVEL)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ---------- GLOBAL CACHE FOR NSE SYMBOLS (for autocomplete) ----------
NSE_SYMBOLS = []  # List of {"symbol": "RELIANCE", "name": "Reliance Industries Ltd"}

def refresh_nse_symbols():
    """Fetch the NSE master list and cache it for autocomplete."""
    global NSE_SYMBOLS
    url = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
    headers = {"User-Agent": config.USER_AGENT}
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        df = pd.read_csv(StringIO(resp.text))
        # Find columns
        symbol_col = next((c for c in df.columns if 'SYMBOL' in c.upper()), None)
        name_col = next((c for c in df.columns if 'NAME' in c.upper()), None)
        series_col = next((c for c in df.columns if 'SERIES' in c.upper()), None)
        if not symbol_col or not name_col:
            logger.error("Could not find columns in NSE CSV")
            return
        # Filter for EQ and BE
        if series_col:
            df[series_col] = df[series_col].str.strip()
            df = df[df[series_col].isin(['EQ', 'BE'])]
        # Store as list of dicts
        NSE_SYMBOLS = [
            {"symbol": row[symbol_col].strip(), "name": row[name_col].strip()}
            for _, row in df.iterrows()
        ]
        logger.info(f"Cached {len(NSE_SYMBOLS)} symbols for autocomplete.")
    except Exception as e:
        logger.error(f"Failed to fetch NSE symbols for autocomplete: {e}")
        NSE_SYMBOLS = []

# Refresh on startup
refresh_nse_symbols()

# ---------- DATABASE ----------
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
                condition TEXT NOT NULL CHECK(condition IN ('>=', '<=')),
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

# ---------- ROUTES ----------
@app.route('/')
def index():
    return render_template('index.html')

# ---------- AUTOCOMPLETE ----------
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
            if len(results) >= 20:  # Limit results for performance
                break
    return jsonify(results)

# ---------- GET CURRENT PRICE (for smart check) ----------
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

# ---------- ADD ALERT (with smart check) ----------
@app.route('/api/add', methods=['POST'])
def add_alert():
    data = request.json
    symbol = data.get('symbol', '').upper()
    condition = data.get('condition')
    trigger_price = data.get('price')
    force = data.get('force', False)  # If user confirms to add even if triggered

    if not symbol or condition not in ('>=', '<=') or not trigger_price:
        return jsonify({'status': 'error', 'message': 'Invalid data'}), 400

    try:
        trigger_price = float(trigger_price)
    except ValueError:
        return jsonify({'status': 'error', 'message': 'Invalid price'}), 400

    # Smart check: fetch current price
    current_price = None
    try:
        prices = stock_alert.get_prices([symbol])
        current_price = prices.get(symbol)
    except Exception as e:
        logger.warning(f"Could not fetch price for {symbol}: {e}")

    # If we got a price and the condition is already met
    warning = None
    if current_price is not None:
        if condition == '>=' and current_price >= trigger_price:
            warning = f"Current price is {current_price}, which already meets the condition."
        elif condition == '<=' and current_price <= trigger_price:
            warning = f"Current price is {current_price}, which already meets the condition."

    # If not forcing and there is a warning, ask the frontend to confirm
    if warning and not force:
        return jsonify({
            'status': 'warning',
            'message': warning,
            'current_price': current_price
        }), 200

    # Add the alert
    conn = get_db()
    try:
        conn.execute('INSERT INTO watchlist (symbol, condition, trigger_price) VALUES (?, ?, ?)',
                     (symbol, condition, trigger_price))
        conn.commit()
        conn.close()
        return jsonify({'status': 'ok'})
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Duplicate alert'}), 400
    except Exception as e:
        conn.close()
        logger.error(f"Add alert DB error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ---------- OTHER API ROUTES (unchanged) ----------
@app.route('/api/alerts')
def get_alerts():
    try:
        conn = get_db()
        alerts = conn.execute('SELECT * FROM watchlist ORDER BY symbol').fetchall()
        conn.close()
        return jsonify([dict(row) for row in alerts])
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

@app.route('/api/reactivate/<int:alert_id>', methods=['POST'])
def reactivate_alert(alert_id):
    try:
        conn = get_db()
        conn.execute('UPDATE watchlist SET is_triggered = 0, is_active = 1 WHERE id = ?', (alert_id,))
        conn.commit()
        conn.close()
        return jsonify({'status': 'ok'})
    except Exception as e:
        logger.error(f"Error in /api/reactivate: {e}")
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

# ---------- WORKER THREAD ----------
def start_worker():
    time.sleep(5)
    stock_alert.main()

worker_thread = threading.Thread(target=start_worker, daemon=True)
worker_thread.start()

# ---------- INIT DB AND RUN ----------
init_db()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)