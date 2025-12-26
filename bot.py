# bot.py — с геолокацией и умным кэшированием
import asyncio
import sqlite3
from datetime import datetime, timedelta
from math import radians, cos, sin, sqrt, atan2
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, Location
import os

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не установлен!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# 📍 Координаты ресторана (Челябинск)
RESTAURANT_LAT = 55.180278
RESTAURANT_LON = 61.293333
MAX_DISTANCE_METERS = 500  # радиус в метрах
GEO_CACHE_MINUTES = 10     # сколько минут действует геопозиция

def get_db():
    conn = sqlite3.connect("/tmp/couriers.db")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS couriers (
                tg_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                last_lat REAL,
                last_lon REAL,
                geo_verified_at TEXT
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

def haversine_distance(lat1, lon1, lat2, lon2):
    """Расстояние в метрах по формуле гаверсинуса"""
    R = 6371000
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lon2 - lon1)
    a = sin(dphi/2)**2 + cos(phi1)*cos(phi2)*sin(dlambda/2)**2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return R * c

def is_in_zone(lat, lon):
    return haversine_distance(RESTAURANT_LAT, RESTAURANT_LON, lat, lon) <= MAX_DISTANCE_METERS

def update_geo(tg_id, lat, lon):
    with get_db() as conn:
        conn.execute("""
            UPDATE couriers 
            SET last_lat = ?, last_lon = ?, geo_verified_at = ?
            WHERE tg_id = ?
        """, (lat, lon, datetime.now().isoformat(), tg_id))

def get_geo_status(tg_id):
    with get_db() as conn:
        row = conn.execute("""
            SELECT last_lat, last_lon, geo_verified_at 
            FROM couriers WHERE tg_id = ?
        """, (tg_id,)).fetchone()
        if not row or not row["last_lat"] or not row["last_lon"]:
            return None, None, None
        return row["last_lat"], row["last_lon"], row["geo_verified_at"]

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
    if not name:
        await m.answer("❌ Имя не может быть пустым.")
        return

    tg_id = m.from_user.id
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO couriers (tg_id, name, last_lat, last_lon, geo_verified_at) "
            "VALUES (?, ?, NULL, NULL, NULL)",
            (tg_id, name)
        )
    # Сразу запрашиваем гео
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📍 Отправить геопозицию", request_location=True)]
    ])
    await m.answer(
        f"✅ Привет, *{name}*!\n\n"
        "Теперь подтверди, что ты рядом с рестораном (улица Курчатова / Труда, Челябинск).\n"
        "Нажми кнопку ниже:",
        parse_mode="Markdown",
        reply_markup=kb
    )

@dp.message(Command("help"))
async def help_cmd(m: Message):
    await m.answer(
        "ℹ️ *Справка по боту*\n\n"
        "🔹 `/регистрация Имя` — один раз в начале\n"
        "🔹 `✅ Встать` — встать в очередь (только в зоне ресторана)\n"
        "🔹 `🚪 Выйти` — покинуть очередь\n\n"
        "💡 Геопозиция запрашивается раз в 10 минут — дальше работает автоматически.",
        parse_mode="Markdown"
    )

# --- ОБРАБОТКА ГЕОЛОКАЦИИ ---
@dp.message(lambda m: m.location is not None)
async def handle_location(m: Message):
    lat = m.location.latitude
    lon = m.location.longitude
    tg_id = m.from_user.id

    with get_db() as conn:
        user = conn.execute("SELECT name FROM couriers WHERE tg_id = ?", (tg_id,)).fetchone()
        if not user:
            await m.answer("❌ Сначала зарегистрируйся: /регистрация Имя")
            return

    dist = haversine_distance(RESTAURANT_LAT, RESTAURANT_LON, lat, lon)
    
    if is_in_zone(lat, lon):
        update_geo(tg_id, lat, lon)
        await m.answer(
            f"✅ Добро пожаловать в зону!\n"
            f"Ты в {dist:.0f} м от ресторана.\n\n"
            "Теперь можешь вставать в очередь без повторной проверки (до 10 минут)."
        )
    else:
        await m.answer(
            f"🚫 Ты слишком далеко!\n"
            f"Расстояние: {dist:.0f} м\n"
            f"Нужно ≤ {MAX_DISTANCE_METERS} м.\n\n"
            "Подойди ближе и отправь геопозицию снова.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📍 Повторить", request_location=True)]
            ])
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

        in_queue = conn.execute("SELECT 1 FROM queue WHERE tg_id = ?", (tg_id,)).fetchone()
        if in_queue:
            await c.answer("✅ Ты уже в очереди! Сначала выйди через 🚪 Выйти", show_alert=True)
            return

    # Проверяем гео-статус
    last_lat, last_lon, verified_at = get_geo_status(tg_id)
    
    if not last_lat or not last_lon:
        # Никогда не отправлял гео
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📍 Отправить геопозицию", request_location=True)]
        ])
        await c.message.answer(
            "🔒 Нужно подтвердить, что ты рядом с рестораном.",
            reply_markup=kb
        )
        await c.answer()
        return

    # Проверяем срок действия
    if verified_at:
        verified_time = datetime.fromisoformat(verified_at)
        if datetime.now() - verified_time > timedelta(minutes=GEO_CACHE_MINUTES):
            # Срок истёк
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📍 Обновить геопозицию", request_location=True)]
            ])
            await c.message.answer(
                f"⏳ Последняя геопозиция устарела (> {GEO_CACHE_MINUTES} мин).\n"
                "Обнови, пожалуйста:",
                reply_markup=kb
            )
            await c.answer()
            return

    # Проверяем, всё ещё в зоне?
    if not is_in_zone(last_lat, last_lon):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📍 Обновить геопозицию", request_location=True)]
        ])
        await c.message.answer(
            "🚫 Ты вышел из зоны ресторана.\n"
            "Чтобы встать в очередь — обнови геопозицию:",
            reply_markup=kb
        )
        await c.answer()
        return

    # ✅ Всё ок — встаём в очередь
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
    print("🤖 Telegram-бот с геопроверкой запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
