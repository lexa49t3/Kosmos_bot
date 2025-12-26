# bot.py
import asyncio
import sqlite3
from datetime import datetime
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# Получаем токен из переменной окружения (Railway будет подставлять его)
import os
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("❌ Переменная BOT_TOKEN не установлена!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- БАЗА ---
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

init_db()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def get_queue():
    with get_db() as conn:
        return conn.execute("""
            SELECT c.name, q.tg_id, q.join_time
            FROM queue q
            JOIN couriers c ON q.tg_id = c.tg_id
            ORDER BY q.join_time
        """).fetchall()

def add_to_queue(tg_id):
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO queue (tg_id, join_time) VALUES (?, ?)",
                     (tg_id, datetime.now().isoformat()))

def remove_from_queue(tg_id):
    with get_db() as conn:
        return conn.execute("DELETE FROM queue WHERE tg_id = ?", (tg_id,)).rowcount

def assign_order(tg_id):
    with get_db() as conn:
        conn.execute("INSERT INTO orders (courier_tg_id, assigned_at) VALUES (?, ?)",
                     (tg_id, datetime.now().isoformat()))
        conn.execute("DELETE FROM queue WHERE tg_id = ?", (tg_id,))

@dp.callback_query(lambda c: c.data == "join")
async def join_btn(c: types.CallbackQuery):
    tg_id = c.from_user.id
    with get_db() as conn:
        user = conn.execute("SELECT name FROM couriers WHERE tg_id = ?", (tg_id,)).fetchone()
        if not user:
            await c.answer("⛔ Зарегистрируйся сначала: /регистрация Имя", show_alert=True)
            return

    add_to_queue(tg_id)
    pos = get_queue_position(tg_id)
    await c.answer(f"✅ Ты №{pos} в очереди!", show_alert=True)
    # Обновим клавиатуру
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Встать в очередь", callback_data="join")],
        [InlineKeyboardButton(text="🚪 Выйти из очереди", callback_data="leave")],
        [InlineKeyboardButton(text="ℹ️ Справка", callback_data="help")]
    ])
    await c.message.edit_reply_markup(reply_markup=kb)

@dp.callback_query(lambda c: c.data == "leave")
async def leave_btn(c: types.CallbackQuery):
    tg_id = c.from_user.id
    with get_db() as conn:
        changed = conn.execute("DELETE FROM queue WHERE tg_id = ?", (tg_id,)).rowcount
    text = "🚪 Ты вышел из очереди." if changed else "📭 Тебя не было в очереди."
    await c.answer(text, show_alert=True)

@dp.callback_query(lambda c: c.data == "help")
async def help_btn(c: types.CallbackQuery):

@app.route("/api/queue")
def api_queue():
    db = get_db()
    queue = db.execute('''
        SELECT c.name
        FROM queue q
        JOIN couriers c ON q.tg_id = c.tg_id
        ORDER BY q.join_time
    ''').fetchall()
    result = [{"name": row["name"]} for row in queue]
    print("🔍 API /api/queue →", result)  # ← будет в логах Railway
    return jsonify(result)

# --- КОМАНДЫ ---
@dp.message(Command("start"))
async def start(m: Message):
    # Проверим, зарегистрирован ли
    with get_db() as conn:
        user = conn.execute("SELECT name FROM couriers WHERE tg_id = ?", (m.from_user.id,)).fetchone()
    
    if user:
        # Зарегистрирован → показываем кнопки
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Встать в очередь", callback_data="join")],
            [InlineKeyboardButton(text="🚪 Выйти из очереди", callback_data="leave")],
            [InlineKeyboardButton(text="ℹ️ Справка", callback_data="help")]
        ])
        await m.answer(f"Привет, {user['name']}! 👋\nВыбери действие:", reply_markup=kb)
    else:
        # Не зарегистрирован → просим регистрацию
        await m.answer(
            "🚴 Добро пожаловать!\n\n"
            "📌 Сначала зарегистрируйся:\n"
            "`/регистрация Имя`\n\n"
            "Например: `/регистрация Иван`",
            parse_mode="Markdown"
        )

@dp.message(Command("регистрация"))
async def reg(m: Message):
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        await m.answer("📌 /регистрация Имя")
        return
    name = parts[1].strip()
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO couriers (tg_id, name) VALUES (?, ?)",
                     (m.from_user.id, name))
    await m.answer(f"✅ Привет, {name}!")

@dp.message(Command("встать"))
async def join(m: Message):
    add_to_queue(m.from_user.id)
    queue = get_queue()
    pos = next((i+1 for i, q in enumerate(queue) if q["tg_id"] == m.from_user.id), 1)
    await m.answer(f"✅ Ты №{pos} в очереди!")

@dp.message(Command("выйти"))
async def leave(m: Message):
    if remove_from_queue(m.from_user.id):
        await m.answer("🚪 Ты вышел из очереди.")
    else:
        await m.answer("📭 Тебя не было в очереди.")

@dp.message(Command("help"))
async def help_cmd(m: Message):
    help_text = (
        "ℹ️ *Справка по боту*\n\n"
        "🔹 `/регистрация Имя` — один раз в начале\n"
        "   Пример: `/регистрация Анна`\n\n"
        "🔹 `✅ Встать` — встать в конец очереди\n"
        "🔹 `🚪 Выйти` — покинуть очередь\n\n"
        "💡 Подсказка: после регистрации кнопки появятся автоматически — просто нажми /start"
    )
    await m.answer(help_text, parse_mode="Markdown")

# --- ЗАПУСК ---
async def main():
    print("🤖 Telegram-бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":

    asyncio.run(main())


