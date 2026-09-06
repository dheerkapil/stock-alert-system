import sqlite3
import threading
import time
from flask import Flask, render_template, request, jsonify
import config
import stock_alert

app = Flask(__name__)

def get_db():
    conn = sqlite3.connect(config.DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/alerts')
def get_alerts():
    conn = get_db()
    alerts = conn.execute('SELECT * FROM watchlist ORDER BY symbol').fetchall()
    conn.close()
    return jsonify([dict(row) for row in alerts])

@app.route('/api/add', methods=['POST'])
def add_alert():
    data = request.json
    symbol = data.get('symbol', '').upper()
    condition = data.get('condition')
    price = data.get('price')
    if not symbol or condition not in ('>=', '<=') or not price:
        return jsonify({'status': 'error', 'message': 'Invalid data'}), 400
    try:
        price = float(price)
    except ValueError:
        return jsonify({'status': 'error', 'message': 'Invalid price'}), 400
    conn = get_db()
    try:
        conn.execute('INSERT INTO watchlist (symbol, condition, trigger_price) VALUES (?, ?, ?)',
                     (symbol, condition, price))
        conn.commit()
        return jsonify({'status': 'ok'})
    except sqlite3.IntegrityError:
        return jsonify({'status': 'error', 'message': 'Duplicate alert'}), 400
    finally:
        conn.close()

@app.route('/api/toggle/<int:alert_id>', methods=['POST'])
def toggle_alert(alert_id):
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

@app.route('/api/reactivate/<int:alert_id>', methods=['POST'])
def reactivate_alert(alert_id):
    conn = get_db()
    conn.execute('UPDATE watchlist SET is_triggered = 0, is_active = 1 WHERE id = ?', (alert_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

@app.route('/api/mark_triggered/<int:alert_id>', methods=['POST'])
def mark_triggered(alert_id):
    conn = get_db()
    conn.execute('UPDATE watchlist SET is_triggered = 1 WHERE id = ?', (alert_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

@app.route('/api/delete/<int:alert_id>', methods=['DELETE'])
def delete_alert(alert_id):
    conn = get_db()
    conn.execute('DELETE FROM watchlist WHERE id = ?', (alert_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

# ------------------------------------------------------------------
#  START THE ALERT WORKER IN A BACKGROUND THREAD
# ------------------------------------------------------------------
def start_worker():
    # Give the web server a moment to start
    time.sleep(5)
    stock_alert.main()

# Run the worker as a daemon thread (will exit when main process ends)
worker_thread = threading.Thread(target=start_worker, daemon=True)
worker_thread.start()

# ------------------------------------------------------------------
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)