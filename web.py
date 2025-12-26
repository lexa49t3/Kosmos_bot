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

@app.route("/")
def redirect_to_cashier():
    return '<meta http-equiv="refresh" content="0;url=/cashier" />'

@app.route("/cashier")
def cashier():
    return render_template("cashier.html")

@app.route("/api/queue")
def api_queue():
    db = get_db()
    rows = db.execute('''
        SELECT c.name
        FROM queue q
        LEFT JOIN couriers c ON q.tg_id = c.tg_id
        WHERE c.name IS NOT NULL AND c.name != ''
        ORDER BY q.join_time
    ''').fetchall()
    return jsonify([{"name": row["name"]} for row in rows if row["name"]])





