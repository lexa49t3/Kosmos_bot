# web.py
from flask import Flask, render_template_string, request, redirect, url_for
import sqlite3
from datetime import datetime
import os

app = Flask(__name__)

def get_db():
    conn = sqlite3.connect("/tmp/couriers.db")
    conn.row_factory = sqlite3.Row
    return conn

# 🎨 HTML-шаблон (всё в одном файле — удобно для старта)
HTML = '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>🍕 Панель управления доставкой</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { font-family: sans-serif; margin: 20px; background: #f5f5f5; }
        .container { max-width: 800px; margin: 0 auto; background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        h1 { color: #d32f2f; text-align: center; }
        .card { margin: 15px 0; padding: 15px; border: 1px solid #eee; border-radius: 8px; }
        .queue-item { background: #e3f2fd; }
        .btn { background: #d32f2f; color: white; border: none; padding: 8px 12px; border-radius: 4px; cursor: pointer; }
        .btn:hover { opacity: 0.9; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        th, td { padding: 10px; text-align: left; border-bottom: 1px solid #ddd; }
        .empty { color: #666; font-style: italic; }
    </style>
</head>
<body>
<div class="container">
    <h1>🍕 Панель доставки</h1>

    <!-- Очередь -->
    <div class="card">
        <h3>📋 Текущая очередь</h3>
        {% if queue %}
            <p>Всего: {{ queue|count }}</p>
            <table>
                <tr><th>№</th><th>Курьер</th><th>Время</th><th>Действие</th></tr>
                {% for item in queue %}
                <tr class="queue-item">
                    <td>{{ loop.index }}</td>
                    <td><strong>{{ item.name }}</strong></td>
                    <td>{{ item.join_time.split('T')[0] }} {{ item.join_time.split('T')[1][:5] }}</td>
                    <td>
                        <form method="POST" action="/assign" style="display:inline;">
                            <input type="hidden" name="tg_id" value="{{ item.tg_id }}">
                            <button class="btn" type="submit">📦 Назначить</button>
                        </form>
                    </td>
                </tr>
                {% endfor %}
            </table>
        {% else %}
            <p class="empty">📭 Очередь пуста</p>
        {% endif %}
    </div>

    <!-- Статистика -->
    <div class="card">
        <h3>📊 Статистика по курьерам</h3>
        {% if stats %}
            <table>
                <tr><th>Курьер</th><th>Всего заказов</th><th>Сегодня</th></tr>
                {% for s in stats %}
                <tr>
                    <td><strong>{{ s.name }}</strong></td>
                    <td>{{ s.total or 0 }}</td>
                    <td>{{ s.today or 0 }}</td>
                </tr>
                {% endfor %}
            </table>
        {% else %}
            <p class="empty">Нет зарегистрированных курьеров</p>
        {% endif %}
    </div>

    <form method="POST" action="/refresh">
        <button class="btn" type="submit">🔄 Обновить</button>
    </form>
</div>
</body>
</html>
'''

@app.route("/", methods=["GET", "POST"])
def index():
    db = get_db()
    
    # Текущая очередь
    queue = db.execute('''
        SELECT c.name, q.tg_id, q.join_time
        FROM queue q
        JOIN couriers c ON q.tg_id = c.tg_id
        ORDER BY q.join_time
    ''').fetchall()
    
    # Статистика
    today = datetime.now().strftime("%Y-%m-%d")
    stats = db.execute('''
        SELECT 
            c.name,
            COUNT(o.id) as total,
            COUNT(CASE WHEN date(o.assigned_at) = ? THEN 1 END) as today
        FROM couriers c
        LEFT JOIN orders o ON c.tg_id = o.courier_tg_id
        GROUP BY c.tg_id, c.name
        ORDER BY total DESC
    ''', (today,)).fetchall()
    

    return render_template_string(HTML, queue=queue, stats=stats)


