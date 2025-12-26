# bot.py — улучшенная версия
import asyncio
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не установлен!")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("❌ DATABASE_URL не установлен! Добавь в Railway Variables.")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- FSM для регистрации ---
class Register(StatesGroup):
    waiting_for_name = State()

def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
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
                    tg_id BIGINT,
                    join_time TIMESTAMPTZ DEFAULT NOW(),
                    FOREIGN KEY(tg_id) REFERENCES couriers(tg_id)
                )
            """)
            conn.commit()

init_db()

# --- Вспомогательные функции ---
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
            return cur.rowcount

def get_queue():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.name
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

# --- /start → главное меню ---
@dp.message(Command("start"))
async def start(m: Message, state: FSMContext):
    await state.clear()
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM couriers WHERE tg_id = %s", (m.from_user.id,))
            user = cur.fetchone()

    if user:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Встать в очередь", callback_data="join")],
            [InlineKeyboardButton(text="🚪 Выйти из очереди", callback_data="leave")],
            [InlineKeyboardButton(text="📋 Список", callback_data="show_queue")],
            [InlineKeyboardButton(text="ℹ️ Справка", callback_data="help")]
        ])
        await m.answer(f"Привет, {user['name']}! 👋\nВыбери действие:", reply_markup=kb)
    else:
        await m.answer("👋 Добро пожаловать!\nПожалуйста, укажи своё *имя и фамилию*:", parse_mode="Markdown")
        await state.set_state(Register.waiting_for_name)

# --- FSM: ожидание имени ---
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
        await start(m, state)  # покажем меню
    except Exception as e:
        await m.answer("❌ Ошибка регистрации. Попробуй ещё раз.")
        print("ERROR:", e)

# --- Кнопки ---
@dp.callback_query(F.data == "join")
async def join_btn(c: CallbackQuery):
    tg_id = c.from_user.id
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM couriers WHERE tg_id = %s", (tg_id,))
            user = cur.fetchone()
            if not user:
                await c.answer("⛔ Сначала зарегистрируйся", show_alert=True)
                return

            cur.execute("SELECT 1 FROM queue WHERE tg_id = %s", (tg_id,))
            in_queue = cur.fetchone()
            if in_queue:
                await c.answer("✅ Ты уже в очереди! Сначала выйди через 🚪 Выйти", show_alert=True)
                return

    add_to_queue(tg_id)
    pos = get_queue_position(tg_id)
    await c.answer(f"✅ Ты №{pos} в очереди!", show_alert=True)

@dp.callback_query(F.data == "leave")
async def leave_btn(c: CallbackQuery):
    changed = remove_from_queue(c.from_user.id)
    await c.answer("Ты вышел из очереди." if changed else "Тебя не было в очереди.", show_alert=True)

@dp.callback_query(F.data == "show_queue")
async def show_queue(c: CallbackQuery):
    rows = get_queue()
    if not rows:
        text = "📭 Очередь пуста."
    else:
        lines = [f"{i+1}. {row['name']}" for i, row in enumerate(rows)]
        text = "📋 *Текущая очередь:*\n" + "\n".join(lines)
    await c.message.answer(text, parse_mode="Markdown")
    await c.answer()

@dp.callback_query(F.data == "help")
async def help_btn(c: CallbackQuery):
    await c.message.answer(
        "ℹ️ *Справка*\n\n"
        "🔹 При первом входе — укажи имя и фамилию\n"
        "🔹 ✅ Встать — встать в очередь\n"
        "🔹 🚪 Выйти — покинуть очередь\n"
        "🔹 📋 Список — посмотреть очередь\n\n"
        "Все действия — через кнопки, без команд.",
        parse_mode="Markdown"
    )
    await c.answer()

# --- Запуск ---
async def main():
    print("🚀 Бот запущен (PostgreSQL + FSM)")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
