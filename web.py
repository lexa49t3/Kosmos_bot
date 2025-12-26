# web.py
from flask import Flask, render_template, request, redirect, url_for
import sqlite3
from datetime import datetime
import os

app = Flask(__name__, template_folder="templates")

def get_db():
    conn = sqlite3.connect("/tmp/couriers.db")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS couriers (
                tg_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER,
                join_time TEXT,
                FOREIGN KEY(tg_id) REFERENCES couriers(tg_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                courier_tg_id INTEGER,
                assigned_at TEXT,
                completed_at TEXT,
                FOREIGN KEY(courier_tg_id) REFERENCES couriers(tg_id)
            )
        """)

# Создаём БД при старте
init_db()

from flask import jsonify

@app.route("/api/queue")
def api_queue():
    db = get_db()
    rows = db.execute('''
        SELECT c.name
        FROM queue q
        JOIN couriers c ON q.tg_id = c.tg_id
        ORDER BY q.join_time
    ''').fetchall()
    # Конвертируем в список словарей
    return jsonify([{"name": r["name"]} for r in rows])

@app.route("/", methods=["GET"])
def index():
    db = get_db()
    queue = db.execute('''
        SELECT c.name, q.tg_id, q.join_time
        FROM queue q
        JOIN couriers c ON q.tg_id = c.tg_id
        ORDER BY q.join_time
    ''').fetchall()

    today = datetime.now().strftime("%Y-%m-%d")
    stats = db.execute('''
        SELECT c.name,
               COUNT(o.id) as total,
               SUM(CASE WHEN date(o.assigned_at) = ? THEN 1 ELSE 0 END) as today
        FROM couriers c
        LEFT JOIN orders o ON c.tg_id = o.courier_tg_id
        GROUP BY c.tg_id, c.name
        ORDER BY total DESC
    ''', (today,)).fetchall()

    return render_template("index.html", queue=queue, stats=stats)

@app.route("/assign", methods=["POST"])
def assign_order():
    tg_id = request.form.get("tg_id")
    if tg_id:
        db = get_db()
        db.execute(
            "INSERT INTO orders (courier_tg_id, assigned_at) VALUES (?, ?)",
            (tg_id, datetime.now().isoformat())
        )
        db.execute("DELETE FROM queue WHERE tg_id = ?", (tg_id,))
        db.commit()
    return redirect(url_for("index"))

@app.route("/cashier")
def cashier():
    return render_template("cashier.html")

@app.route("/refresh", methods=["POST"])
def refresh():
    return redirect(url_for("index"))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)




