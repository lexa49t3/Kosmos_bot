# app.py - чистый aiohttp сервер с Telegram ботом (только API и касса)
import asyncio
import logging
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from aiohttp.web import Request, Response

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

BASE_URL = os.getenv("BASE_URL", "https://your-app-name.up.railway.app").rstrip("/")
WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = "courier_bot_secret_2025"

# Получаем Telegram ID кассира из переменной окружения
CASHIER_TG_ID = os.getenv("CASHIER_TG_ID")
if not CASHIER_TG_ID:
    raise RuntimeError("❌ CASHIER_TG_ID не установлен в Variables!")
# Преобразуем строку в число
try:
    CASHIER_TG_ID = int(CASHIER_TG_ID)
except ValueError:
    raise RuntimeError("❌ CASHIER_TG_ID должен быть числом!")

# === ГЛОБАЛЬНАЯ ПЕРЕМЕННАЯ ДЛЯ ХРАНЕНИЯ ID СООБЩЕНИЯ С ОЧЕРЕДЬЮ (только для кассира) ===
queue_message_id = None

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
                conn.commit()
        logger.info("База данных инициализирована/проверена успешно.")
    except Exception as e:
        logger.error(f"Ошибка инициализации БД: {e}")
        raise

# Инициализация БД при старте
init_db()

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
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
            result = cur.execute("DELETE FROM queue WHERE tg_id = %s", (tg_id,))
            affected = result.rowcount
            conn.commit()
            return affected

def get_queue():
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

# === HTML шаблон для кассы ===
CASHIER_HTML = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>👨‍💻 Касса — Очередь курьеров</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #f5f5f5;
            padding: 15px;
            min-height: 100vh;
        }
        .container {
            max-width: 600px;
            margin: 0 auto;
        }
        header {
            text-align: center;
            margin-bottom: 25px;
            padding: 15px;
            background: #d32f2f;
            color: white;
            border-radius: 12px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.1);
        }
        h1 {
            font-size: 1.6rem;
            font-weight: 600;
        }
        .time {
            font-size: 0.9rem;
            opacity: 0.9;
            margin-top: 4px;
        }
        .queue-list {
            list-style: none;
        }
        .queue-item {
            background: white;
            margin-bottom: 12px;
            padding: 16px;
            border-radius: 10px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.08);
            display: flex;
            align-items: center;
            font-size: 1.3rem;
            font-weight: 500;
        }
        .number {
            display: flex;
            align-items: center;
            justify-content: center;
            width: 42px;
            height: 42px;
            background: #e57373;
            color: white;
            border-radius: 50%;
            margin-right: 16px;
            font-size: 1.4rem;
            flex-shrink: 0;
        }
        .name {
            flex-grow: 1;
        }
        .remove-btn {
            background: #f44336;
            color: white;
            border: none;
            border-radius: 8px;
            padding: 8px 12px;
            cursor: pointer;
            font-size: 1rem;
            font-weight: 500;
            transition: background-color 0.2s;
        }
        .remove-btn:hover {
            background: #d32f2f;
        }
        .empty {
            text-align: center;
            color: #757575;
            font-size: 1.2rem;
            padding: 40px 20px;
        }
        .last-update {
            text-align: center;
            color: #666;
            font-size: 0.85rem;
            margin-top: 20px;
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>Касса — Очередь курьеров</h1>
            <div class="time" id="current-time">—</div>
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
            document.getElementById('current-time').textContent = 
                now.toLocaleTimeString('ru-RU', { hour12: false });
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
                        list.innerHTML = '<li class="empty">ostringstream Очередь пуста</li>';
                    } else {
                        // Создаем HTML для каждого элемента очереди с кнопкой удаления
                        list.innerHTML = data.map((item, index) => 
                            `<li class="queue-item">
                                <div class="number">${index + 1}</div>
                                <div class="name">${item.name}</div>
                                <button class="remove-btn" onclick="removeCourier(${item.tg_id})">❌ Удалить</button>
                            </li>`
                        ).join('');
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
                        '<li class="empty">⚠️ Ошибка загрузки</li>';
                });
        }

        function removeCourier(tgId) {
            if (confirm(`Вы уверены, что хотите удалить курьера с ID ${tgId} из очереди?`)) {
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
                        alert('Ошибка при удалении курьера.');
                        console.error('Ошибка при удалении:', response.status);
                    }
                })
                .catch(err => {
                    console.error('Ошибка сети при удалении:', err);
                    alert('Ошибка сети при удалении курьера.');
                });
            }
        }

        // Обновляем сразу при загрузке
        updateTime();
        updateQueue();

        // Автообновление
        setInterval(updateTime, 1000);
        setInterval(updateQueue, 5000);
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

# === ХЕНДЛЕРЫ БОТА ===
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
            if cur.fetchone():
                await c.answer("✅ Ты уже в очереди! Сначала выйди через 🚪 Выйти", show_alert=True)
                return

    add_to_queue(tg_id)
    pos = get_queue_position(tg_id)
    await c.answer(f"✅ Ты №{pos} в очереди!", show_alert=True)

@dp.callback_query(F.data == "leave")
async def leave_btn(c: CallbackQuery):
    changed = remove_from_queue(c.from_user.id)
    await c.answer("Ты вышел из очереди." if changed else "Тебя не было в очереди.", show_alert=True)

# --- ИЗМЕНЕННЫЙ ХЕНДЛЕР show_queue ---
@dp.callback_query(F.data == "show_queue")
async def show_queue(c: CallbackQuery):
    global queue_message_id
    rows = get_queue()
    if not rows:
        text = "sstream пуста."
    else:
        lines = [f"{i+1}. {row['name']}" for i, row in enumerate(rows)]
        text = "📋 *Текущая очередь:*\n" + "\n".join(lines)

    # Создаем клавиатуру без кнопок удаления для курьеров
    # Проверяем, является ли пользователь кассиром
    is_cashier = (c.from_user.id == CASHIER_TG_ID)
    
    if is_cashier:
        # Для кассира добавляем кнопку "Обновить очередь" или "Назад" (опционально)
        # В данном случае, кнопка "Список" сама по себе обновляет, так что можно оставить как есть
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="show_queue")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
        ])
    else:
        # Для обычного курьера просто кнопка "Назад"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
        ])

    # Проверяем, нужно ли отправить новое сообщение или отредактировать старое
    # Если это кассир и у нас есть ID старого сообщения с очередью
    if c.from_user.id == CASHIER_TG_ID and queue_message_id:
        try:
            # Пытаемся отредактировать старое сообщение
            await bot.edit_message_text(
                chat_id=c.from_user.id,
                message_id=queue_message_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=kb
            )
            # ID сообщения остается тем же
            await c.answer() # Ответим на callback
            return # Выходим, чтобы не отправлять новое сообщение
        except Exception as e:
            # Если не удалось отредактировать (например, сообщение удалено), логируем и отправим новое
            logger.warning(f"Не удалось отредактировать сообщение с очередью: {e}")
            # Сбросим ID, так как сообщение больше не существует
            queue_message_id = None

    # Отправляем новое сообщение
    sent_message = await c.message.answer(text, parse_mode="Markdown", reply_markup=kb)
    # Если это кассир, сохраняем ID нового сообщения
    if c.from_user.id == CASHIER_TG_ID:
        queue_message_id = sent_message.message_id
    await c.answer() # Ответим на callback

# --- /ИЗМЕНЕННЫЙ ХЕНДЛЕР ---

# Добавляем обработчик для кнопки "Назад"
@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu(c: CallbackQuery, state: FSMContext):
    global queue_message_id
    # Сбрасываем ID сообщения с очередью, если пользователь уходит с этой страницы
    if c.from_user.id == CASHIER_TG_ID:
        queue_message_id = None

    # Повторяем логику start, но для редактирования сообщения
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM couriers WHERE tg_id = %s", (c.from_user.id,))
            user = cur.fetchone()

    if user:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Встать в очередь", callback_data="join")],
            [InlineKeyboardButton(text="🚪 Выйти из очереди", callback_data="leave")],
            [InlineKeyboardButton(text="📋 Список", callback_data="show_queue")],
            [InlineKeyboardButton(text="ℹ️ Справка", callback_data="help")]
        ])
        await c.message.edit_text(f"Привет, {user['name']}! 👋\nВыбери действие:", reply_markup=kb)
    else:
        await c.message.edit_text("👋 Добро пожаловать!\nПожалуйста, укажи своё *имя и фамилию*:", parse_mode="Markdown")
        await state.set_state(Register.waiting_for_name)
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

# === AIOHTTP маршруты ===
async def api_queue(request: Request) -> Response:
    try:
        rows = get_queue()
        # Возвращаем список объектов с name и tg_id
        return web.json_response([{"name": row["name"], "tg_id": row["tg_id"]} for row in rows])
    except Exception as e:
        logger.error(f"Ошибка в /api/queue: {e}")
        return web.json_response({"error": "Internal Server Error"}, status=500)

# --- МАРШРУТ ДЛЯ УДАЛЕНИЯ ЧЕРЕЗ САЙТ ---
async def api_remove_courier(request: Request) -> Response:
    try:
        data = await request.json()
        tg_id = data.get("tg_id")
        
        if not tg_id:
            return web.json_response({"error": "Missing tg_id"}, status=400)

        # Проверяем, что tg_id - число
        try:
            tg_id = int(tg_id)
        except ValueError:
            return web.json_response({"error": "Invalid tg_id format"}, status=400)

        removed = remove_from_queue(tg_id)

        if removed > 0:
            logger.info(f"Курьер {tg_id} удален из очереди через веб-интерфейс.")
            # Удаляем ID сообщения с очередью, чтобы оно обновилось при следующем вызове show_queue
            global queue_message_id
            if queue_message_id:
                queue_message_id = None
            return web.json_response({"status": "success", "removed": removed})
        else:
            # Возвращаем success, даже если курьер не был в очереди
            logger.info(f"Попытка удалить курьера {tg_id}, которого нет в очереди.")
            return web.json_response({"status": "success", "removed": 0})

    except Exception as e:
        logger.error(f"Ошибка в /api/remove_courier: {e}")
        return web.json_response({"error": "Internal Server Error"}, status=500)

# --- /МАРШРУТ ---

async def root_handler(request: Request) -> Response:
    return web.Response(text=CASHIER_HTML, content_type="text/html")

async def cashier(request: Request) -> Response:
    return web.Response(text=CASHIER_HTML, content_type="text/html")

async def healthcheck(request: Request) -> Response:
    return web.json_response({"status": "ok", "bot": "running"})

# === Основная функция запуска ===
async def main():
    app = web.Application()
    
    # Healthcheck
    app.router.add_get("/health", healthcheck)
    
    # Главная страница - теперь возвращает кассу
    app.router.add_get("/", root_handler)
    
    # API маршруты
    app.router.add_get("/api/queue", api_queue)
    # Добавляем новый маршрут для удаления через сайт
    app.router.add_post("/api/remove_courier", api_remove_courier)
    
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

    # Бесконечный цикл для удержания процесса
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        logger.info("Приложение останавливается...")
    finally:
        await runner.cleanup()
        logger.info("Сервер остановлен.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        logger.error(f"Критическая ошибка в main: {e}")
        exit(1)
