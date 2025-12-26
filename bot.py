# bot.py — только Telegram-бот (aiogram), без Flask!
import asyncio
import sqlite3
from datetime import datetime
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
import os

# Токен
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не установлен!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- БАЗА ДАННЫХ ---
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

init_db()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def add_to_queue(tg_id):
    with get_db() as conn:
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT OR IGNORE INTO queue (tg_id, join_time) VALUES (?, ?)",
            (tg_id, now)
        )

def remove_from_queue(tg_id):
    with get_db() as conn:
        return conn.execute("DELETE FROM queue WHERE tg_id = ?", (tg_id,)).rowcount

def get_queue_position(tg_id):
    with get_db() as conn:
        res = conn.execute("""
            SELECT COUNT(*) FROM queue
            WHERE join_time <= (SELECT join_time FROM queue WHERE tg_id = ?)
        """, (tg_id,)).fetchone()
        return res[0] if res else 1

# --- КОМАНДЫ ---
@dp.message(Command("start"))
async def start(m: Message):
    with get_db() as conn:
        user = conn.execute("SELECT name FROM couriers WHERE tg_id = ?", (m.from_user.id,)).fetchone()
    
    if user:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Встать в очередь", callback_data="join")],
            [InlineKeyboardButton(text="🚪 Выйти из очереди", callback_data="leave")],
            [InlineKeyboardButton(text="ℹ️ Справка", callback_data="help")]
        ])
        await m.answer(f"Привет, {user['name']}! 👋\nВыбери действие:", reply_markup=kb)
    else:
        await m.answer(
            "🚴 Добро пожаловать!\n\n"
            "📌 Сначала зарегистрируйся:\n"
            "`/регистрация Имя`\n\n"
            "Пример: `/регистрация Иван`",
            parse_mode="Markdown"
        )

@dp.message(Command("регистрация"))
async def register(m: Message):
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        await m.answer("📌 Пример: `/регистрация Иван`", parse_mode="Markdown")
        return
    name = parts[1].strip()
    tg_id = m.from_user.id
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO couriers (tg_id, name) VALUES (?, ?)",
            (tg_id, name)
        )
    await m.answer(f"✅ Привет, *{name}*! Теперь ты в системе.", parse_mode="Markdown")

@dp.message(Command("help"))
async def help_cmd(m: Message):
    await m.answer(
        "ℹ️ *Справка по боту*\n\n"
        "🔹 `/регистрация Имя` — один раз в начале\n"
        "🔹 `✅ Встать` — встать в очередь\n"
        "🔹 `🚪 Выйти` — покинуть очередь\n\n"
        "💡 После регистрации нажми /start — появятся кнопки.",
        parse_mode="Markdown"
    )

# --- КНОПКИ ---
@dp.callback_query(lambda c: c.data == "join")
async def join_btn(c: CallbackQuery):
    tg_id = c.from_user.id
    with get_db() as conn:
        user = conn.execute("SELECT name FROM couriers WHERE tg_id = ?", (tg_id,)).fetchone()
        if not user:
            await c.answer("⛔ Зарегистрируйся: /регистрация Имя", show_alert=True)
            return
    add_to_queue(tg_id)
    pos = get_queue_position(tg_id)
    await c.answer(f"✅ Ты №{pos} в очереди!", show_alert=True)

@dp.callback_query(lambda c: c.data == "leave")
async def leave_btn(c: CallbackQuery):
    tg_id = c.from_user.id
    changed = remove_from_queue(tg_id)
    text = "🚪 Ты вышел из очереди." if changed else "📭 Тебя не было в очереди."
    await c.answer(text, show_alert=True)

@dp.callback_query(lambda c: c.data == "help")
async def help_btn(c: CallbackQuery):
    await help_cmd(c.message)

# --- ЗАПУСК ---
async def main():
    print("🤖 Telegram-бот запущен и готов к работе!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
