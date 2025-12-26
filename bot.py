# bot.py
import asyncio
import sqlite3
from datetime import datetime
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

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

# --- КОМАНДЫ ---
@dp.message(Command("start"))
async def start(m: Message):
    await m.answer("Привет! Используй /регистрация Имя, чтобы начать.")

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

# --- ЗАПУСК ---
async def main():
    print("🤖 Telegram-бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":

    asyncio.run(main())

