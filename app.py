# app.py - чистый aiohttp сервер с Telegram ботом (только API и касса)
import asyncio
import logging
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Union
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.exceptions import TelegramBadRequest
from aiohttp import web
from aiohttp.web import Request, Response
from datetime import datetime, timedelta
# --- Добавляем aiocron ---
import aiocron

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === НАСТРОЙКИ ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("❌ BOT_TOKEN не установлен в Variables!")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("❌ DATABASE_URL не установлен в Variables!")

# Новый параметр для ID чата
CALL_CHAT_ID = os.getenv("CALL_CHAT_ID")
if not CALL_CHAT_ID:
    raise RuntimeError("❌ CALL_CHAT_ID не установлен в Variables!")
try:
    CALL_CHAT_ID = int(CALL_CHAT_ID)
except ValueError:
    raise RuntimeError("❌ CALL_CHAT_ID должен быть числом!")

BASE_URL = os.getenv("BASE_URL", "https://your-app-name.up.railway.app").rstrip("/")
WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = "courier_bot_secret_2025"

# === БАЗА ===
def get_db():
    url = DATABASE_URL.replace("postgresql://", "postgres://")
    try:
        return psycopg2.connect(url, cursor_factory=RealDictCursor)
    except Exception as e:
        logger.error(f"Ошибка подключения к БД: {e}")
        raise
def init_db():
    try:
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
                # --- Таблица для логов ---
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS logs (
                        log_id SERIAL PRIMARY KEY,
                        tg_id BIGINT NOT NULL,
                        courier_name TEXT NOT NULL DEFAULT '',
                        action TEXT NOT NULL, -- 'joined_queue', 'left_queue', 'removed_by_cashier', 'removed_by_daily_clear', 'started_lunch', 'ended_lunch'
                        timestamp TIMESTAMPTZ DEFAULT NOW(),
                        FOREIGN KEY (tg_id) REFERENCES couriers(tg_id) ON DELETE CASCADE
                    )
                """)
                # --- НОВАЯ ТАБЛИЦА ДЛЯ СЕАНСОВ ОБЕДА (исправленная) ---
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS lunch_sessions (
                        session_id SERIAL PRIMARY KEY,
                        tg_id BIGINT NOT NULL,
                        start_time TIMESTAMPTZ DEFAULT NOW(),
                        end_time TIMESTAMPTZ, -- NULL, если не закончен
                        date DATE DEFAULT CURRENT_DATE, -- Просто сохраняем дату начала сеанса
                        FOREIGN KEY (tg_id) REFERENCES couriers(tg_id) ON DELETE CASCADE
                    )
                """)
                # --- /НОВАЯ ТАБЛИЦА ---
                conn.commit()
        logger.info("База данных инициализирована/проверена успешно.")
    except Exception as e:
        logger.error(f"Ошибка инициализации БД: {e}")
        raise

# Инициализация БД при старте
init_db()

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
def format_time_for_display(seconds):
    """Форматирует время в формате MM:SS для отображения в боте."""
    mins = seconds // 60
    secs = seconds % 60
    return f"{mins:02}:{secs:02}"

def add_to_queue(tg_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO queue (tg_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (tg_id,)
            )
            conn.commit()

def remove_from_queue(tg_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM queue WHERE tg_id = %s", (tg_id,))
            # Получаем rowcount ДО commit
            affected = cur.rowcount
            conn.commit()
            # Возвращаем значение rowcount
            return affected

def get_courier_logs(tg_id, limit=50):
    """Получить последние N логов для курьера с отформатированным временем."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT action, timestamp
                FROM logs
                WHERE tg_id = %s
                ORDER BY timestamp DESC
                LIMIT %s
            """, (tg_id, limit))
            rows = cur.fetchall()

    # Преобразуем timestamp в нужный формат
    formatted_rows = []
    tz = ZoneInfo("Europe/Moscow") # Укажите нужный часовой пояс
    for row in rows:
        # Преобразуем timestamp (в UTC) в Moscow time и форматируем
        local_dt = row['timestamp'].astimezone(tz)
        formatted_time = local_dt.strftime("%H:%M %d.%m.%Y")
        # Добавляем отформатированное время в строку результата
        formatted_row = dict(row) # Создаем копию строки
        formatted_row['formatted_timestamp'] = formatted_time
        formatted_rows.append(formatted_row)

    return formatted_rows

def get_courier_name(tg_id):
    """Получить имя курьера по его tg_id."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM couriers WHERE tg_id = %s", (tg_id,))
            row = cur.fetchone()
            if row:
                return row['name']
            else:
                return None

def clear_queue():
    """Функция для очистки всей очереди."""
    with get_db() as conn:
        with conn.cursor() as cur:
            # Сначала получим всех, кто был в очереди, вместе с именами
            cur.execute("""
                SELECT q.tg_id, c.name
                FROM queue q
                JOIN couriers c ON q.tg_id = c.tg_id;
            """)
            queued_couriers = cur.fetchall()
            
            # Удалим всех
            cur.execute("DELETE FROM queue;")
            affected = cur.rowcount
            
            # Залогируем для каждого из них
            for courier_row in queued_couriers:
                log_action(courier_row['tg_id'], courier_row['name'], "Ежедневная очистка очереди") # Передаём name
            
            conn.commit()
            logger.info(f"Очередь очищена. Удалено {affected} записей. Залогированы участники.")
            return affected

def get_queue_and_lunching():
    """Получает очередь и курьеров на обеде."""
    with get_db() as conn:
        with conn.cursor() as cur:
            # Основная очередь
            cur.execute("""
                SELECT c.name, c.tg_id, q.join_time as time_info, 'queue' as source
                FROM queue q
                JOIN couriers c ON q.tg_id = c.tg_id
                ORDER BY q.join_time ASC
            """)
            queue_rows = cur.fetchall()

            # Курьеры на обеде (уже с 'time_info' и 'source' благодаря изменению в get_lunching_couriers)
            lunching_rows = get_lunching_couriers() # <-- Теперь возвращает {'name', 'tg_id', 'time_info', 'source'}

    # Объединяем и сортируем: сначала очередь, потом обедающие
    all_rows = queue_rows + lunching_rows # <-- lunching_rows уже содержит 'time_info' и 'source'
    # Сортировка: сначала очередь ('queue' == False, 'lunch' == True), потом обедающие по времени начала обеда
    all_rows.sort(key=lambda x: (x['source'] == 'lunch', x['time_info']))
    return all_rows

def get_queue():
    """Получает только курьеров, находящихся в очереди."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.name, c.tg_id
                FROM queue q
                JOIN couriers c ON q.tg_id = c.tg_id
                ORDER BY q.join_time
            """)
            return cur.fetchall()

def get_queue_with_details():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.name, q.tg_id, q.join_time
                FROM queue q
                JOIN couriers c ON q.tg_id = c.tg_id
                ORDER BY q.join_time
            """)
            return cur.fetchall()

def get_queue_position(tg_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM queue
                WHERE join_time <= (SELECT join_time FROM queue WHERE tg_id = %s)
            """, (tg_id,))
            res = cur.fetchone()
            return res["count"] if res else 1

def get_stats():
    with get_db() as conn:
        with conn.cursor() as cur:
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
            return cur.fetchall()
            
def log_action(tg_id, courier_name, action):
    """Записывает действие курьера в базу данных."""
    tz = ZoneInfo("Asia/Yekaterinburg") # Укажите нужный часовой пояс

    # Получаем текущее время в нужном часовом поясе и форматируем его
    current_time_local = datetime.now(tz)
    formatted_time_str = current_time_local.strftime("%H:%M %d.%m.%Y")

    with get_db() as conn:
        with conn.cursor() as cur:
            # Вставляем как tg_id, courier_name, action, так и отформатированное время
            cur.execute(
                "INSERT INTO logs (tg_id, courier_name, action, formatted_time) VALUES (%s, %s, %s, %s)",
                (tg_id, courier_name, action, formatted_time_str)
            )
            conn.commit()
        logger.info(f"Лог: Курьер {courier_name} (ID: {tg_id}) {action} в {formatted_time_str}.")

#Функция обеда
def get_current_lunch_session(tg_id):
    """Проверяет, находится ли курьер на обеде, и возвращает сессию, если да."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT session_id, start_time, end_time
                FROM lunch_sessions
                WHERE tg_id = %s AND end_time IS NULL
                ORDER BY start_time DESC
                LIMIT 1
            """, (tg_id,))
            return cur.fetchone()

def get_lunch_count_today(tg_id):
    """Возвращает количество сеансов обеда за сегодня."""
    today = datetime.now().date()
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) as count
                FROM lunch_sessions
                WHERE tg_id = %s AND date = %s
            """, (tg_id, today))
            res = cur.fetchone()
            return res['count'] if res else 0

def start_lunch_session(tg_id, courier_name):
    """Создаёт новую сессию обеда."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO lunch_sessions (tg_id) VALUES (%s)
                RETURNING session_id
            """, (tg_id,))
            session_id = cur.fetchone()['session_id']
            conn.commit()
            logger.info(f"Курьер {courier_name} (ID: {tg_id}) начал обед (ID сессии: {session_id}).")
            log_action(tg_id, courier_name, "started_lunch")
            return session_id

def end_lunch_session(session_id, tg_id, courier_name):
    """Завершает сессию обеда."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE lunch_sessions
                SET end_time = NOW()
                WHERE session_id = %s AND tg_id = %s AND end_time IS NULL
            """, (session_id, tg_id))
            updated = cur.rowcount
            conn.commit()
            if updated > 0:
                logger.info(f"Курьер {courier_name} (ID: {tg_id}) закончил обед (ID сессии: {session_id}).")
                log_action(tg_id, courier_name, "ended_lunch")
                return True
            else:
                logger.warning(f"Попытка завершить несуществующую или уже завершённую сессию обеда {session_id} для курьера {tg_id}.")
                return False

def get_lunching_couriers():
    """Получает список курьеров, находящихся на обеде."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.name, ls.tg_id, ls.start_time
                FROM lunch_sessions ls
                JOIN couriers c ON ls.tg_id = c.tg_id
                WHERE ls.end_time IS NULL
                ORDER BY ls.start_time ASC -- Сортировка по времени начала
            """)
            rows = cur.fetchall()
            # Преобразуем результат, чтобы ключ start_time был под ключом time_info
            # Это нужно, чтобы соответствовать структуре queue_rows в get_queue_and_lunching
            formatted_rows = []
            for row in rows:
                formatted_row = {
                    'name': row['name'],
                    'tg_id': row['tg_id'],
                    'time_info': row['start_time'], # <-- Вот тут
                    'source': 'lunch'
                }
                formatted_rows.append(formatted_row)
            return formatted_rows

# === HTML шаблон для кассы ===
CASHIER_HTML = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Очередь</title>
    <style>
    :root {
        /* Светлая тема по умолчанию */
        --bg: #f8f9fa;
        --card-bg: #ffffff;
        --header-bg: #2c3e50;
        --header-text: #ecf0f1;
        --text: #2c3e50;
        --text-secondary: #7f8c8d;
        --border: #e0e0e0;
        --accent: #3498db; /* Синий акцент */
        --accent-hover: #2980b9;
        --success: #27ae60;
        --success-hover: #219653;
        --danger: #e74c3c;
        --danger-hover: #c0392b;
        --lunch-bg: #f39c12;
        --lunch-text: #2c3e50;
        --number-bg: #3498db;
        --number-text: #ffffff;
        --btn-primary-bg: var(--accent);
        --btn-primary-hover: var(--accent-hover);
        --btn-secondary-bg: #f1f3f4;
        --btn-secondary-hover: #e4e7ea;
    }

    /* Тёмная тема */
    [data-theme="dark"] {
       --bg: #121212;
        --card-bg: #1e1e1e;
        --header-bg: #1a1a1a;
        --header-text: #f0f0f0;
        --text: #e0e0e0;
        --text-secondary: #95a5a6;
        --border: #333333;
        --accent: #3498db;
        --accent-hover: #2980b9;
        --success: #2ecc71;
        --success-hover: #27ae60;
        --danger: #e74c3c;
        --danger-hover: #c0392b;
        --lunch-bg: #f39c12;
        --lunch-text: #f5f5f5;
        --number-bg: #3498db;
        --number-text: #ffffff;
        --btn-primary-bg: var(--accent);
        --btn-primary-hover: var(--accent-hover);
        --btn-secondary-bg: #2d2d2d;
        --btn-secondary-hover: #3c3c3c;
    }

    * {
        margin: 0;
        padding: 0;
        box-sizing: border-box;
    }

    body {
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        background: var(--bg);
        color: var(--text);
        padding: 16px;
        min-height: 100vh;
    }

    .container {
        max-width: 600px;
        margin: 0 auto;
    }

    /* === HEADER === */
    .app-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 16px;
        background: var(--header-bg);
        border-radius: 12px;
        box-shadow: 0 4px 12px rgba(0,0,0,0.15);
        margin-bottom: 24px;
        position: relative;
    }

    .header-left h1 {
        font-size: 1.8rem;
        font-weight: 600;
        color: var(--header-text);
    }

    .header-right {
        display: flex;
        flex-direction: row-reverse;
        align-items: flex-end;
        gap: 8px;
    }

    .header-time-date {
        display: flex;
        flex-direction: column;
        align-items: flex-end;
        gap: 4px;
    }

    #date-display {
        font-size: 0.9rem;
        color: var(--text-secondary);
        font-weight: 500;
    }

    #time-display {
        font-size: 1.1rem;
        font-weight: 600;
        color: var(--header-text);
    }

    .theme-toggle {
        padding: 0px 0px 2px 1px;
        width: 44px;
        height: 44px;
        border-radius: 50%;
        background: var(--btn-secondary-bg);
        border: none;
        color: var(--text);
        font-size: 1.2rem;
        cursor: pointer;
        display: flex;
        align-items: center;
        justify-content: center;
        box-shadow: 0 2px 6px rgba(0,0,0,0.1);
        transition: all 0.3s ease;
        user-select: none;
    }

    .theme-toggle:hover {
        background: var(--btn-secondary-hover);
        transform: scale(1.05);
    }

    .theme-toggle:active {
        transform: scale(0.95);
    }

    /* === QUEUE LIST === */
    .queue-list {
        list-style: none;
    }

    .queue-item {
        background: var(--card-bg);
        margin-bottom: 12px;
        padding: 16px;
        border-radius: 12px;
        box-shadow: 0 3px 10px rgba(0,0,0,0.08);
        display: flex;
        align-items: center;
        font-size: 1.3rem;
        font-weight: 500;
        border-left: 4px solid transparent;
    }

    .queue-item.lunching {
        border-left-color: var(--lunch-bg);
        background: rgba(243, 156, 17, 0.05); /* Очень светлый оранжевый фон */
    }

    .number {
        display: flex;
        align-items: center;
        justify-content: center;
        width: 44px;
        height: 44px;
        background: var(--number-bg);
        color: var(--number-text);
        border-radius: 50%;
        margin-right: 16px;
        font-size: 1.4rem;
        flex-shrink: 0;
        font-weight: 600;
    }

    .name {
        flex-grow: 1;
        font-weight: 500;
    }

    .lunch-badge {
        display: inline-flex;
        align-items: baseline;
        gap: 6px;
        background: var(--lunch-bg);
        color: var(--lunch-text);
        padding: 4px 10px;
        border-radius: 6px;
        font-size: 0.85rem;
        font-weight: 600;
        margin-right: 12px;
    }

    .lunch-badge span:first-child {
        white-space: nowrap;
    }

    .lunch-badge .lunch-timer {
        background: rgba(0,0,0,0.1);
        padding: 2px 6px;
        border-radius: 4px;
        font-family: 'Courier New', monospace;
        font-size: 0.8rem;
    }

    .btn-group {
        display: flex;
        gap: 8px;
    }

    .btn {
        border: none;
        border-radius: 8px;
        padding: 6px 12px;
        font-size: 0.9rem;
        font-weight: 500;
        cursor: pointer;
        display: inline-flex;
        align-items: center;
        gap: 4px;
        transition: all 0.2s ease;
    }

    .btn-call {
        background: var(--success);
        color: white;
    }

    .btn-call:hover {
        background: var(--success-hover);
    }

    .btn-remove {
        background: var(--danger);
        color: white;
    }

    .btn-remove:hover {
        background: var(--danger-hover);
    }

    .empty {
        text-align: center;
        color: var(--text-secondary);
        font-size: 1.2rem;
        padding: 60px 20px;
        font-style: italic;
    }

    .last-update {
        text-align: center;
        color: var(--text-secondary);
        font-size: 0.85rem;
        margin-top: 24px;
    }

    /* === RESPONSIVE === */
    @media (max-width: 480px) {
        .header-right {
            flex-direction: row;
            gap: 12px;
        }
        .header-time-date {
            flex-direction: row;
            gap: 12px;
        }
        .btn-group {
            flex-wrap: wrap;
        }
    }
    </style>
</head>
<body>
    <div class="container">
        <header class="app-header">
    <div class="header-left">
        <h1>Очередь</h1>
    </div>
    <div class="header-right">
        <div class="header-time-date">
            <span id="date-display"></span>
            <span id="time-display"></span>
        </div>
        <button id="theme-toggle" class="theme-toggle" title="Переключить тему">
            🌙
        </button>
    </div>
</header>

        <ul class="queue-list" id="queue-list">
            <!-- Сюда подгрузится очередь -->
        </ul>

        <div class="last-update">
            Обновлено: <span id="update-time">—</span>
        </div>
    </div>

    <script>
        function updateTime() {
    const now = new Date();
    
    // Форматируем дату как DD.MM.YYYY
    const day = String(now.getDate()).padStart(2, '0');
    const month = String(now.getMonth() + 1).padStart(2, '0');
    const year = now.getFullYear();
    document.getElementById('date-display').textContent = `${day}.${month}.${year}`;

    // Форматируем время как HH:MM:SS
    const hours = String(now.getHours()).padStart(2, '0');
    const minutes = String(now.getMinutes()).padStart(2, '0');
    const seconds = String(now.getSeconds()).padStart(2, '0');
    document.getElementById('time-display').textContent = `${hours}:${minutes}:${seconds}`;
}

        function formatTime(seconds) {
            const mins = Math.floor(seconds / 60);
            const secs = seconds % 60;
            return `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
        }
        
        function updateQueue() {
            fetch('/api/queue')
                .then(response => {
                    if (!response.ok) throw new Error('HTTP ' + response.status);
                    return response.json();
                })
                .then(data => {
                    const list = document.getElementById('queue-list');
                    const updateTimeEl = document.getElementById('update-time');
                    
                    if (data.length === 0) {
                        list.innerHTML = '<li class="empty">Очередь пуста</li>';
                    } else {
                        // Разделяем очередь и обедающих
                        const queueItems = data.filter(item => item.source === 'queue');
                        const lunchItems = data.filter(item => item.source === 'lunch');

                       // Генерируем HTML для очереди (с кнопками)
                    const queueHtml = queueItems.map((item, index) => 
                            `<li class="queue-item">
                                <div class="number">${index + 1}</div>
                                <div class="name">${item.name}</div>
                                <div class="btn-group">
                                    <button class="btn btn-call" onclick="callCourier(${item.tg_id})">Позвать</button>
                                    <button class="btn btn-remove" onclick="removeCourier(${item.tg_id})">Удалить</button>
                                </div>
                            </li>`
                        ).join('');

                        // Генерируем HTML для обедающих (с кнопками)
                        const lunchHtml = lunchItems.map(item => 
                    `<li class="queue-item lunching">
                        <div class="number">-</div>
                        <div class="name">${item.name}</div>
                        <div class="lunch-badge">
                            <span>Обед</span>
                            <span class="lunch-timer" data-tg-id="${item.tg_id}">${formatTime(item.remaining_seconds)}</span>
                        </div>
                        <div class="btn-group">
                            <button class="btn btn-call" onclick="callCourier(${item.tg_id})">🐾</button>
                            <button class="btn btn-remove" onclick="removeCourier(${item.tg_id})">🗑️</button>
                        </div>
                    </li>`
                ).join('');

                        list.innerHTML = queueHtml + lunchHtml;
                    }

                    const now = new Date();
                    updateTimeEl.textContent = now.toLocaleTimeString('ru-RU', { 
                        hour: '2-digit', 
                        minute: '2-digit',
                        second: '2-digit'
                    });
                })
                .catch(err => {
                    console.error('Ошибка загрузки очереди:', err);
                    document.getElementById('queue-list').innerHTML = 
                        '<li class="empty">⚠️ Ошибка загрузки, напишите Алексею))</li>';
                });
        }

        // Функция для обновления таймеров обеда
        function updateLunchTimers() {
            document.querySelectorAll('.lunch-timer').forEach(timerElement => {
                const tgId = timerElement.getAttribute('data-tg-id');
                // Найдем соответствующий элемент данных в последнем обновлении
                // (Это менее эффективно, чем хранить данные в JS, но проще для начальной реализации)
                fetch('/api/queue')
                    .then(response => response.json())
                    .then(data => {
                        const item = data.find(d => d.tg_id == tgId && d.source === 'lunch');
                        if (item) {
                            timerElement.textContent = formatTime(item.remaining_seconds);
                        }
                    })
                    .catch(console.error);
            });
        }
        
        function removeCourier(tgId) {
            // Убрано подтверждение
            // if (confirm(`Вы уверены, что хотите удалить курьера с ID ${tgId} из очереди?`)) {
                fetch('/api/remove_courier', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({ tg_id: tgId })
                })
                .then(response => {
                    if (response.ok) {
                        console.log(`Курьер ${tgId} удален.`);
                        updateQueue(); // Обновляем очередь после удаления
                    } else {
                        // Попробуем получить текст ошибки из ответа
                        return response.text().then(text => {
                            console.error('Ошибка при удалении:', response.status, text);
                            alert(`Ошибка при удалении курьера: ${text}`);
                        });
                    }
                })
                .catch(err => {
                    console.error('Ошибка сети при удалении:', err);
                    alert(`Ошибка сети при удалении курьера: ${err.message}`);
                });
            // }
        }

        function callCourier(tgId) {
            fetch('/api/call_courier', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ tg_id: tgId })
            })
            .then(response => {
                if (response.ok) {
                    console.log(`Курьер ${tgId} вызван.`);
                    // Можно добавить визуальный эффект или уведомление об успехе
                } else {
                    // Попробуем получить текст ошибки из ответа
                    return response.text().then(text => {
                        console.error('Ошибка при вызове курьера:', response.status, text);
                        alert(`Ошибка при вызове курьера: ${text}`);
                    });
                }
            })
            .catch(err => {
                console.error('Ошибка сети при вызове курьера:', err);
                alert(`Ошибка сети при вызове курьера: ${err.message}`);
            });
        }


        // Обновляем сразу при загрузке
        updateTime();
        updateQueue();

        // Автообновление
        setInterval(updateTime, 1000);
        setInterval(updateQueue, 5000);
        // Обновляем таймеры обеда чаще
        setInterval(updateLunchTimers, 1000);

        // --- Тема ---
document.addEventListener('DOMContentLoaded', () => {
    const body = document.body;
    const toggleBtn = document.getElementById('theme-toggle');

    // Загружаем сохранённую тему
    const savedTheme = localStorage.getItem('theme') || 'light';
    body.setAttribute('data-theme', savedTheme);
    toggleBtn.textContent = savedTheme === 'dark' ? '☀️' : '🌙';

    // Переключение темы
    toggleBtn.addEventListener('click', () => {
        const currentTheme = body.getAttribute('data-theme');
        const newTheme = currentTheme === 'dark' ? 'light' : 'dark';
        body.setAttribute('data-theme', newTheme);
        localStorage.setItem('theme', newTheme);
        toggleBtn.textContent = newTheme === 'dark' ? '☀️' : '🌙';
    });
});

    
         
         // Обнова
    const CURRENT_VERSION = "3.6";
    const savedVersion = localStorage.getItem('cashier_version');

    if (savedVersion !== CURRENT_VERSION) {
        localStorage.setItem('cashier_version', CURRENT_VERSION);
        // Перезагрузка только при первом запуске новой версии
        location.reload();
    }
    </script>
</body>
</html>
"""

# === Aiogram бот ===
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# === FSM ===
class Register(StatesGroup):
    waiting_for_name = State()

# --- НОВОЕ СОСТОЯНИЕ ---
class ConfirmLunch(StatesGroup):
    waiting_for_confirmation = State()

# === ХЕНДЛЕРЫ БОТА ===
router = Router() # Создайте роутер или используйте dp

@router.message(Command("menu", "refresh_menu")) # Добавляем команду /menu и /refresh_menu
@router.callback_query(F.data == "refresh_main_menu") # Или кнопку "refresh_main_menu"
async def send_refreshed_menu(event: Union[Message, CallbackQuery], state: FSMContext):
    # Получаем информацию о пользователе
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM couriers WHERE tg_id = %s", (event.from_user.id,))
            user = cur.fetchone()

    if not user:
        # Если пользователь не найден, возможно, нужно сбросить состояние и попросить регистрацию
        await state.clear()
        await event.message.answer("👋 Добро пожаловать!\nПожалуйста, укажи своё *имя и фамилию*:", parse_mode="Markdown")
        await state.set_state(Register.waiting_for_name)
        return

    # Создаём обновлённое меню
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Встать в очередь", callback_data="join")],
        [InlineKeyboardButton(text="🚪 Выйти из очереди", callback_data="leave")],
        [InlineKeyboardButton(text="🍽️ Обед", callback_data="lunch_start")], # <-- Новая кнопка
        [InlineKeyboardButton(text="📋 Список", callback_data="show_queue")]
    ])

    # Проверяем, какое событие вызвало функцию
    if isinstance(event, Message):
        # Если это команда, отправляем новое сообщение
        await event.answer(f"Привет, {user['name']}! 👋\nВыбери действие:", reply_markup=kb)
    elif isinstance(event, CallbackQuery):
        # Если это нажатие кнопки, сначала отвечаем на callback
        await event.answer()
        # Затем отправляем новое сообщение с меню
        await event.message.answer(f"Привет, {user['name']}! 👋\nВыбери действие:", reply_markup=kb)

# Не забудьте зарегистрировать роутер в диспетчере
# dp.include_router(router) # Раскомментируйте, если используете роутеры

# Или добавьте хендлер напрямую к dp
dp.message(Command("menu", "refresh_menu"))(send_refreshed_menu)
dp.callback_query(F.data == "refresh_main_menu")(send_refreshed_menu)
@dp.message(Command("start"))
async def start(m: Message, state: FSMContext):
    await state.clear()
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM couriers WHERE tg_id = %s", (m.from_user.id,))
            user = cur.fetchone()

    if user:
        # КНОПКА ОБЕД ДОБАВЛЕНА СЮДА
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Встать в очередь", callback_data="join")],
            [InlineKeyboardButton(text="🚪 Выйти из очереди", callback_data="leave")],
            [InlineKeyboardButton(text="🍽️ Обед", callback_data="lunch_start")], # <-- Новая кнопка
            [InlineKeyboardButton(text="📋 Список", callback_data="show_queue")]
        ])
        # Отправляем НОВОЕ сообщение с обновлённой клавиатурой
        await m.answer(f"Привет, {user['name']}! 👋\nВыбери действие:", reply_markup=kb)
    else:
        await m.answer("👋 Добро пожаловать!\nПожалуйста, укажи своё *имя и фамилию*:", parse_mode="Markdown")
        await state.set_state(Register.waiting_for_name)

@dp.message(Register.waiting_for_name)
async def process_name(m: Message, state: FSMContext):
    name = m.text.strip()
    if not name or len(name.split()) < 2:
        await m.answer("📌 Пожалуйста, введи *имя и фамилию* (например: Иван Затеев)", parse_mode="Markdown")
        return

    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO couriers (tg_id, name) VALUES (%s, %s) "
                    "ON CONFLICT (tg_id) DO UPDATE SET name = %s",
                    (m.from_user.id, name, name)
                )
                conn.commit()
        await m.answer(f"✅ Привет, *{name}*! Теперь ты в системе.", parse_mode="Markdown")
        await start(m, state)
    except Exception as e:
        await m.answer("❌ Ошибка регистрации. Попробуй ещё раз.")
        logger.error(f"Ошибка регистрации пользователя {m.from_user.id}: {e}")

@dp.callback_query(F.data == "join")
async def join_btn(c: CallbackQuery, state: FSMContext): # Добавляем state
    tg_id = c.from_user.id
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM couriers WHERE tg_id = %s", (tg_id,))
            user = cur.fetchone()
            if not user:
                await c.answer("⛔ Сначала зарегистрируйся", show_alert=True)
                return

            cur.execute("SELECT 1 FROM queue WHERE tg_id = %s", (tg_id,))
            if cur.fetchone():
                await c.answer("✅ Ты уже в очереди! Сначала выйди через 🚪 Выйти", show_alert=True)
                return

    add_to_queue(tg_id)
    pos = get_queue_position(tg_id)
    log_action(tg_id, user['name'], "Встал в очередь")
    await c.answer(f"✅ Ты №{pos} в очереди!", show_alert=True)

    # --- НОВОЕ: Отправляем обновлённое меню ---
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Встать в очередь", callback_data="join")],
        [InlineKeyboardButton(text="🚪 Выйти из очереди", callback_data="leave")],
        [InlineKeyboardButton(text="🍽️ Обед", callback_data="lunch_start")], # <-- Новая кнопка
        [InlineKeyboardButton(text="📋 Список", callback_data="show_queue")]
    ])
    try:
        await bot.edit_message_text(
            chat_id=c.from_user.id,
            message_id=c.message.message_id,
            text=f"Привет, {user['name']}! 👋\nВыбери действие:", # Используем имя из запроса выше
            reply_markup=kb,
            parse_mode="Markdown"
        )
    except TelegramBadRequest as e:
        if "message is not modified" in e.message:
            # Сообщение не изменилось, это не ошибка, просто логируем
            logger.info(f"Сообщение для пользователя {tg_id} не изменилось при попытке редактирования в join_btn.")
        else:
            # Другая ошибка TelegramBadRequest
            logger.error(f"Ошибка Telegram при редактировании сообщения в join_btn для {tg_id}: {e}")

@dp.callback_query(F.data == "leave")
async def leave_btn(c: CallbackQuery, state: FSMContext):
    tg_id = c.from_user.id
    # Получаем имя курьера заранее, чтобы использовать в логе
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM couriers WHERE tg_id = %s", (tg_id,))
            user = cur.fetchone()
            if not user:
                await c.answer("❌ Произошла ошибка при выходе из очереди.", show_alert=True)
                logger.error(f"Курьер {tg_id} не найден в таблице couriers при попытке выйти из очереди.")
                return

    # Логируем попытку выйти из очереди
    was_in_queue = False
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM queue WHERE tg_id = %s", (tg_id,))
            if cur.fetchone():
                was_in_queue = True

    changed = remove_from_queue(tg_id)

    # Логируем действие "ушел из очереди", только если он реально был в очереди
    if was_in_queue:
        log_action(tg_id, user['name'], "Вышел из очереди") # Передаём user['name']

    await c.answer("Ты вышел из очереди." if changed else "Тебя не было в очереди.", show_alert=True)

    # --- НОВОЕ: Отправляем обновлённое меню ---
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Встать в очередь", callback_data="join")],
        [InlineKeyboardButton(text="🚪 Выйти из очереди", callback_data="leave")],
        [InlineKeyboardButton(text="🍽️ Обед", callback_data="lunch_start")],
        [InlineKeyboardButton(text="📋 Список", callback_data="show_queue")]
    ])
    try:
        await bot.edit_message_text(
            chat_id=c.from_user.id,
            message_id=c.message.message_id,
            text=f"Привет, {user['name']}! 👋\nВыбери действие:", # Используем имя из запроса выше
            reply_markup=kb,
            parse_mode="Markdown"
        )
    except TelegramBadRequest as e:
        if "message is not modified" in e.message:
            logger.info(f"Сообщение для пользователя {tg_id} не изменилось при попытке редактирования в leave_btn.")
        else:
            logger.error(f"Ошибка Telegram при редактировании сообщения в leave_btn для {tg_id}: {e}")

# --- ИЗМЕНЕННЫЙ ХЕНДЛЕР show_queue (редактирует текущее сообщение) ---
@dp.callback_query(F.data == "show_queue")
async def show_queue(c: CallbackQuery):
    # Вместо get_queue(), используем get_queue_and_lunching()
    all_rows = get_queue_and_lunching()

    if not all_rows:
        text = "sstream пуста."
    else:
        # Разделяем очередь и обедающих
        queue_items = [row for row in all_rows if row['source'] == 'queue']
        lunch_items = [row for row in all_rows if row['source'] == 'lunch']

        # Формируем строки для очереди
        queue_lines = [f"{i+1}. {row['name']}" for i, row in enumerate(queue_items)]

        # Формируем строки для обедающих
        # Рассчитаем оставшееся время для обедающих вручную, как в api_queue
        lunch_lines = []
        for row in lunch_items:
            start_time = row["time_info"]
            elapsed = (datetime.now(start_time.tzinfo) - start_time).total_seconds()
            remaining_seconds = max(0, 20 * 60 - elapsed) # 20 минут = 1200 секунд
            formatted_time = format_time_for_display(int(remaining_seconds))
            lunch_lines.append(f"- {row['name']} (обед, осталось {formatted_time})")

        # Объединяем списки
        all_lines = queue_lines + lunch_lines

        if all_lines:
            text = "📋 *Текущая очередь и обед:* \n" + "\n".join(all_lines)
        else:
            text = "sstream пуста."

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])

    # Редактируем текущее сообщение (из которого нажали кнопку "Список")
    try:
        await bot.edit_message_text(
            chat_id=c.from_user.id,
            message_id=c.message.message_id, # ID текущего сообщения
            text=text,
            parse_mode="Markdown",
            reply_markup=kb
        )
    except Exception as e:
        # Если не удалось отредактировать (например, сообщение слишком старое), отправим новое
        logger.warning(f"Не удалось отредактировать сообщение с очередью: {e}")
        await c.message.answer(text, parse_mode="Markdown", reply_markup=kb)
    await c.answer() # Ответим на callback

# --- ИЗМЕНЕННЫЙ ХЕНДЛЕР back_to_menu (редактирует текущее сообщение) ---
@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu(c: CallbackQuery, state: FSMContext):
    # Повторяем логику start, но для редактирования текущего сообщения
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM couriers WHERE tg_id = %s", (c.from_user.id,))
            user = cur.fetchone()

    if user:
        # КНОПКА ОБЕД ДОБАВЛЕНА СЮДА
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Встать в очередь", callback_data="join")],
            [InlineKeyboardButton(text="🚪 Выйти из очереди", callback_data="leave")],
            [InlineKeyboardButton(text="🍽️ Обед", callback_data="lunch_start")], # <-- Новая кнопка
            [InlineKeyboardButton(text="📋 Список", callback_data="show_queue")]
        ])
        # Редактируем текущее сообщение (из которого нажали кнопку "Назад")
        try:
            await bot.edit_message_text(
                chat_id=c.from_user.id,
                message_id=c.message.message_id, # ID текущего сообщения
                text=f"Привет, {user['name']}! 👋\nВыбери действие:",
                reply_markup=kb,
                parse_mode="Markdown"
            )
        except TelegramBadRequest as e:
            if "message is not modified" in e.message:
                logger.info(f"Сообщение для пользователя {c.from_user.id} не изменилось при попытке редактирования в back_to_menu.")
            else:
                logger.error(f"Ошибка Telegram при редактировании сообщения в back_to_menu для {c.from_user.id}: {e}")
                # Если редактирование не удалось, отправим новое сообщение
                await c.message.edit_text(f"Привет, {user['name']}! 👋\nВыбери действие:", reply_markup=kb, parse_mode="Markdown")
    else:
        await c.message.edit_text("👋 Добро пожаловать!\nПожалуйста, укажи своё *имя и фамилию*:", parse_mode="Markdown")
        await state.set_state(Register.waiting_for_name)
    await c.answer() # Ответим на callback
# --- /ИЗМЕНЕННЫЙ ХЕНДЛЕР ---

# --- НОВЫЙ ХЕНДЛЕР ДЛЯ КНОПКИ ОБЕД ---
@dp.callback_query(F.data == "lunch_start")
async def lunch_start_request(c: CallbackQuery, state: FSMContext):
    tg_id = c.from_user.id
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM couriers WHERE tg_id = %s", (tg_id,))
            user = cur.fetchone()
            if not user:
                await c.answer("❌ Произошла ошибка.", show_alert=True)
                return
            courier_name = user['name']

    # Проверяем, не на обеде ли уже
    if get_current_lunch_session(tg_id):
        await c.answer("❌ Вы уже на обеде!", show_alert=True)
        return
    # Проверяем лимит обедов за день (2)
    lunch_count = get_lunch_count_today(tg_id)
    if lunch_count >= 2:
        await c.answer("❌ Вы уже уходили на обеды сегодня (2 раза).", show_alert=True)
        return
    # Проверяем, в очереди ли курьер
    is_in_queue = False
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM queue WHERE tg_id = %s", (tg_id,))
            if cur.fetchone():
                is_in_queue = True
    # Отправляем предупреждение и спрашиваем подтверждение
    confirmation_message = f"🍽️ Вы хотите уйти на обед?\n\n"
    if is_in_queue:
        confirmation_message += "⚠️ Вы покинете очередь.\n"
    confirmation_message += "⏱️ Обед длится 20 минут. После этого вы автоматически встанете в очередь\n"
    confirmation_message += "📌 За смену можно уходить на обед не более 2-х раз\n\n"
    confirmation_message += "Нажмите 'Да, уйти на обед' для подтверждения."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, уйти на обед", callback_data="lunch_confirm_yes")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="lunch_confirm_no")]
    ])
    await c.message.edit_text(confirmation_message, reply_markup=kb)
    await state.set_state(ConfirmLunch.waiting_for_confirmation)
    await c.answer()

@dp.callback_query(StateFilter(ConfirmLunch.waiting_for_confirmation), F.data == "lunch_confirm_yes")
async def lunch_start_confirm(c: CallbackQuery, state: FSMContext):
    tg_id = c.from_user.id
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM couriers WHERE tg_id = %s", (tg_id,))
            user = cur.fetchone()
            if not user:
                await c.answer("❌ Произошла ошибка.", show_alert=True)
                await state.clear()
                return
            courier_name = user['name']
    # Проверяем, не на обеде ли уже (на всякий случай)
    if get_current_lunch_session(tg_id):
        await c.answer("❌ Вы уже на обеде!", show_alert=True)
        await state.clear()
        return
    # Удаляем из очереди (если был)
    was_in_queue = remove_from_queue(tg_id)
    # Создаём сессию обеда
    session_id = start_lunch_session(tg_id, courier_name)
    # Отредактируем сообщение: только кнопка "С обеда"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ С обеда", callback_data="lunch_end")]
    ])
    await c.message.edit_text(f"🍽️ Вы на обеде, осталось 20 минут!", reply_markup=kb)
    # Запускаем задачу на 20 минут
    asyncio.create_task(auto_return_from_lunch(session_id, tg_id, courier_name))
    await state.clear()
    await c.answer()

@dp.callback_query(StateFilter(ConfirmLunch.waiting_for_confirmation), F.data == "lunch_confirm_no")
async def lunch_start_cancel(c: CallbackQuery, state: FSMContext):
    # Возвращаем к основному меню
    await state.clear()
    await back_to_menu(c, state) # Используем существующую функцию

@dp.callback_query(F.data == "lunch_end")
async def lunch_end_manual(c: CallbackQuery):
    tg_id = c.from_user.id
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM couriers WHERE tg_id = %s", (tg_id,))
            user = cur.fetchone()
            if not user:
                await c.answer("❌ Произошла ошибка.", show_alert=True)
                return
            courier_name = user['name']

    session_info = get_current_lunch_session(tg_id)
    if not session_info:
        # Курьер не на обеде (возможно, уже автоматически вернулся)
        # Всё равно отправим ему обновлённое меню
        await c.answer("Вы уже не на обеде!", show_alert=True) # Уведомление

        # Создаём обновлённое меню
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Встать в очередь", callback_data="join")],
            [InlineKeyboardButton(text="🚪 Выйти из очереди", callback_data="leave")],
            [InlineKeyboardButton(text="🍽️ Обед", callback_data="lunch_start")], # Возвращаем кнопку обеда
            [InlineKeyboardButton(text="📋 Список", callback_data="show_queue")]
        ])
        # Редактируем *текущее* сообщение (в котором была нажата кнопка "С обеда")
        await bot.edit_message_text(
            chat_id=c.from_user.id,
            message_id=c.message.message_id,
            text=f"Привет, {courier_name}! 👋\nВыбери действие:",
            reply_markup=kb,
            parse_mode="Markdown"
        )
        return # Завершаем выполнение функции здесь

    # --- Старая логика для ручного завершения сессии ---
    session_id = session_info['session_id']
    ended = end_lunch_session(session_id, tg_id, courier_name)

    if ended:
        # Возвращаем в очередь
        add_to_queue(tg_id)
        pos = get_queue_position(tg_id)

        # Отредактируем сообщение: обычные кнопки
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Встать в очередь", callback_data="join")],
            [InlineKeyboardButton(text="🚪 Выйти из очереди", callback_data="leave")],
            [InlineKeyboardButton(text="🍽️ Обед", callback_data="lunch_start")], # Возвращаем кнопку обеда
            [InlineKeyboardButton(text="📋 Список", callback_data="show_queue")]
        ])
        await c.message.edit_text(f"✅ Вы вернулись с обеда и встали в очередь. Ваша позиция: {pos}", reply_markup=kb)
    else:
        # Сессия уже была завершена (например, автоматически) - это случай, который теперь обрабатывается в `if not session_info:`
        # Логика выше уже сработает.
        pass

    await c.answer()

async def auto_return_from_lunch(session_id, tg_id, courier_name):
    """Фоновая задача, которая возвращает курьера в очередь через 20 минут."""
    await asyncio.sleep(20 * 60) # 20 минут в секундах

    # Проверяем, не завершена ли сессия вручную
    session_info = get_current_lunch_session(tg_id)
    if session_info and session_info['session_id'] == session_id:
        # Сессия всё ещё активна, завершаем её автоматически
        ended = end_lunch_session(session_id, tg_id, courier_name)
        if ended:
            # Возвращаем в очередь
            add_to_queue(tg_id)
            pos = get_queue_position(tg_id)
            logger.info(f"Курьер {courier_name} (ID: {tg_id}) автоматически вернулся в очередь после обеда. Позиция: {pos}.")

            # Отправляем сообщение курьеру (опционально)
            try:
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Встать в очередь", callback_data="join")],
                    [InlineKeyboardButton(text="🚪 Выйти из очереди", callback_data="leave")],
                    [InlineKeyboardButton(text="🍽️ Обед", callback_data="lunch_start")],
                    [InlineKeyboardButton(text="📋 Список", callback_data="show_queue")]
                ])
                await bot.edit_message_text(
                    chat_id=tg_id,
                    message_id=..., # Нужно хранить ID сообщения об обеде, чтобы его отредактировать
                    text=f"⏱️ Обед закончился! Вы автоматически встали в очередь. Ваша позиция: {pos}",
                    reply_markup=kb
                )
            except Exception as e:
                logger.warning(f"Не удалось отредактировать сообщение после авто-возврата из обеда для {tg_id}: {e}")
                # Альтернатива: отправить новое сообщение
                try:
                    await bot.send_message(
                        chat_id=tg_id,
                        text=f"⏱️ Обед закончился! Вы автоматически встали в очередь. Ваша позиция: {pos}"
                    )
                except Exception as e2:
                    logger.error(f"Не удалось отправить сообщение после авто-возврата из обеда для {tg_id}: {e2}")

# === AIOHTTP маршруты ===
async def api_queue(request: Request) -> Response:
    try:
        rows = get_queue_and_lunching()
        # Возвращаем список объектов с name, tg_id и source
        response_data = []
        for row in rows:
            item = {"name": row["name"], "tg_id": row["tg_id"], "source": row["source"]}
            if row["source"] == 'lunch':
                # Добавляем признак обеда и оставшееся время (в секундах)
                # row["time_info"] доступен благодаря изменению в get_lunching_couriers
                start_time = row["time_info"]
                elapsed = (datetime.now(start_time.tzinfo) - start_time).total_seconds()
                remaining_seconds = max(0, 20 * 60 - elapsed) # 20 минут = 1200 секунд
                item["remaining_seconds"] = int(remaining_seconds)
            # Не добавляем remaining_seconds для 'queue'
            response_data.append(item)

        return web.json_response(response_data)
    except Exception as e:
        logger.error(f"Ошибка в /api/queue: {e}")
        return web.json_response({"error": "Internal Server Error"}, status=500)

# --- МАРШРУТ ДЛЯ ВЫЗОВА КУРЬЕРА ---
async def api_call_courier(request: Request) -> Response:
    try:
        # Попробуем получить JSON, но обернем в try-except
        try:
            data = await request.json()
        except Exception as e:
            logger.error(f"Ошибка парсинга JSON в /api/call_courier: {e}")
            return web.json_response({"error": f"Invalid JSON format: {str(e)}"}, status=400)

        tg_id = data.get("tg_id")

        if tg_id is None: # Проверяем на None, а не на пустое значение
            return web.json_response({"error": "Missing tg_id"}, status=400)

        # Проверяем, что tg_id - число
        try:
            tg_id = int(tg_id)
        except ValueError:
            return web.json_response({"error": "Invalid tg_id format, must be an integer"}, status=400)

        # Получаем имя курьера из базы
        courier_name = get_courier_name(tg_id)
        if not courier_name:
             logger.warning(f"Попытка вызвать курьера с несуществующим ID {tg_id}")
             return web.json_response({"error": "Courier not found"}, status=404)

        # Пытаемся получить username через бота
        try:
            user_info = await bot.get_chat(tg_id)
            username = user_info.username # Может быть None
        except Exception as e:
            logger.warning(f"Не удалось получить информацию о пользователе {tg_id}: {e}")
            username = None

        # Формируем сообщение
        if username:
            message_to_send = f"{courier_name} @{username}"
        else:
            # Если username не удалось получить, отправляем только имя
            message_to_send = courier_name

        # Отправляем сообщение в чат
        try:
            await bot.send_message(chat_id=CALL_CHAT_ID, text=message_to_send)
            logger.info(f"Отправлено сообщение '{message_to_send}' в чат {CALL_CHAT_ID} для вызова курьера {tg_id}")
            return web.json_response({"status": "success", "message": f"Called {message_to_send}"})
        except Exception as e:
            logger.error(f"Ошибка при отправке сообщения в чат {CALL_CHAT_ID}: {e}")
            return web.json_response({"error": f"Failed to send message: {str(e)}"}, status=500)

    except Exception as e:
        logger.error(f"Неожиданная ошибка в /api/call_courier: {e}")
        return web.json_response({"error": "Internal Server Error"}, status=500)

# --- /МАРШРУТ ---

# --- МАРШРУТ ДЛЯ УДАЛЕНИЯ ЧЕРЕЗ САЙТ ---
async def api_remove_courier(request: Request) -> Response:
    try:
        try:
            data = await request.json()
        except Exception as e:
            logger.error(f"Ошибка парсинга JSON в /api/remove_courier: {e}")
            return web.json_response({"error": f"Invalid JSON format: {str(e)}"}, status=400)

        tg_id = data.get("tg_id")
        if tg_id is None:
            return web.json_response({"error": "Missing tg_id"}, status=400)
        try:
            tg_id = int(tg_id)
        except ValueError:
            return web.json_response({"error": "Invalid tg_id format, must be an integer"}, status=400)

        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT name FROM couriers WHERE tg_id = %s", (tg_id,))
                user = cur.fetchone()
                if not user:
                    return web.json_response({"error": "Courier not found"}, status=404)
        courier_name = user['name']

        # --- НОВАЯ ЛОГИКА: Проверяем, на обеде ли курьер ---
        session_info = get_current_lunch_session(tg_id)
        was_on_lunch = False
        if session_info:
            # Завершаем сессию обеда
            ended = end_lunch_session(session_info['session_id'], tg_id, courier_name)
            if ended:
                was_on_lunch = True
                logger.info(f"Курьер {courier_name} (ID: {tg_id}) был на обеде и сессия завершена.")

        # --- Удаляем из очереди (если есть) ---
        was_in_queue = False
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM queue WHERE tg_id = %s", (tg_id,))
                if cur.fetchone():
                    was_in_queue = True

        removed = remove_from_queue(tg_id)

        # --- Логируем действие ---
        if was_on_lunch and was_in_queue:
            log_action(tg_id, courier_name, "Удалён с обеда и из очереди")
        elif was_on_lunch:
            log_action(tg_id, courier_name, "Удалён с обеда")
        elif was_in_queue:
            log_action(tg_id, courier_name, "Удалён из очереди")
        else:
            log_action(tg_id, courier_name, "Попытка удаления: не в очереди и не на обеде")

        # Возвращаем результат
        if removed > 0 or was_on_lunch:
            return web.json_response({"status": "success", "removed": removed, "was_on_lunch": was_on_lunch})
        else:
            return web.json_response({"status": "success", "removed": 0, "was_on_lunch": False})

    except Exception as e:
        logger.error(f"Неожиданная ошибка в /api/remove_courier: {e}")
        return web.json_response({"error": "Internal Server Error"}, status=500)


# --- /МАРШРУТ ---
async def root_handler(request: Request) -> Response:
    return web.Response(text=CASHIER_HTML, content_type="text/html")

async def cashier(request: Request) -> Response:
    return web.Response(text=CASHIER_HTML, content_type="text/html")

async def healthcheck(request: Request) -> Response:
    return web.json_response({"status": "ok", "bot": "running"})

async def scheduled_queue_clear():
    """Асинхронная функция, вызываемая по расписанию."""
    logger.info("Запуск запланированной очистки очереди...")
    clear_queue()

# === Основная функция запуска ===
async def main():
    app = web.Application()
    
    # Healthcheck
    app.router.add_get("/health", healthcheck)
    
    # Главная страница - теперь возвращает кассу
    app.router.add_get("/", root_handler)
    
    # API маршруты
    app.router.add_get("/api/queue", api_queue)
    # Добавляем новые маршруты
    app.router.add_post("/api/remove_courier", api_remove_courier)
    app.router.add_post("/api/call_courier", api_call_courier) # <-- Новый маршрут
    
    # Веб-интерфейс маршруты
    app.router.add_get("/cashier", cashier)
    
    # Регистрируем обработчик вебхука aiogram
    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET,
    )
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)
    
    setup_application(app, dp, bot=bot)
    
    port = int(os.getenv("PORT", 8080))
    logger.info(f"Попытка запуска сервера на порту {port}")
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    
    logger.info(f"Сервер запущен на порту {port}")
    
    # Устанавливаем вебхук
    webhook_url = f"{BASE_URL}{WEBHOOK_PATH}"
    try:
        await bot.set_webhook(webhook_url, secret_token=WEBHOOK_SECRET, drop_pending_updates=True)
        logger.info(f"✅ Webhook установлен: {webhook_url}")
    except Exception as e:
        logger.error(f"❌ Ошибка установки вебхука: {e}")
        raise

    # --- ЗАПУСК ПЛАНИРОВЩИКА ---
    # Запускаем задачу на очистку очереди каждый день в 01:00 по Екатеринбургу (UTC+5)
    # Это соответствует 20:00 UTC
    cron_task = aiocron.crontab('0 20 * * *', func=scheduled_queue_clear)
    logger.info("Планировщик задач запущен. Очередь будет очищаться каждый день в 01:00 по Екатеринбургскому времени (20:00 UTC).")

    # Бесконечный цикл для удержания процесса
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        logger.info("Приложение останавливается...")
        cron_task.stop() # Останавливаем планировщик при завершении
    finally:
        await runner.cleanup()
        logger.info("Сервер остановлен.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        logger.error(f"Критическая ошибка в main: {e}")
        exit(1)
