# web.py — исправлен под PostgreSQL
from flask import Flask, render_template, request, redirect, url_for, jsonify
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime
import os

app = Flask(__name__, template_folder="templates")

def get_db():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise ValueError("❌ DATABASE_URL не установлен!")
    # Railway даёт URL с `postgresql://`, но psycopg2 требует `postgres://`
    url = url.replace("postgresql://", "postgres://")
    conn = psycopg2.connect(url, cursor_factory=RealDictCursor)
    return conn

def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS couriers (
                    tg_id BIGINT PRIMARY KEY,
                    name TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS queue (
                    id SERIAL PRIMARY KEY,
                    tg_id BIGINT NOT NULL,
                    join_time TIMESTAMPTZ DEFAULT NOW(),
                    FOREIGN KEY (tg_id) REFERENCES couriers(tg_id) ON DELETE CASCADE
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id SERIAL PRIMARY KEY,
                    courier_tg_id BIGINT NOT NULL,
                    assigned_at TIMESTAMPTZ DEFAULT NOW(),
                    completed_at TIMESTAMPTZ,
                    FOREIGN KEY (courier_tg_id) REFERENCES couriers(tg_id) ON DELETE CASCADE
                )
            """)
            conn.commit()

# Инициализируем БД
init_db()

@app.route("/api/queue")
def api_queue():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.name
                FROM queue q
                JOIN couriers c ON q.tg_id = c.tg_id
                ORDER BY q.join_time
            """)
            rows = cur.fetchall()
    return jsonify([{"name": row["name"]} for row in rows])

@app.route("/", methods=["GET"])
def index():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.name, q.tg_id, q.join_time
                FROM queue q
                JOIN couriers c ON q.tg_id = c.tg_id
                ORDER BY q.join_time
            """)
            queue = cur.fetchall()

            today = datetime.now().strftime("%Y-%m-%d")
            cur.execute("""
                SELECT c.name,
                       COUNT(o.id) AS total,
                       SUM(CASE WHEN DATE(o.assigned_at) = %s THEN 1 ELSE 0 END) AS today
                FROM couriers c
                LEFT JOIN orders o ON c.tg_id = o.courier_tg_id
                GROUP BY c.tg_id, c.name
                ORDER BY total DESC
            """, (today,))
            stats = cur.fetchall()

    return render_template("index.html", queue=queue, stats=stats)

@app.route("/assign", methods=["POST"])
def assign_order():
    tg_id = request.form.get("tg_id")
    if tg_id:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO orders (courier_tg_id) VALUES (%s)",
                    (tg_id,)
                )
                cur.execute("DELETE FROM queue WHERE tg_id = %s", (tg_id,))
                conn.commit()
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
