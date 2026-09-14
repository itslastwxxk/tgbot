from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, FSInputFile, 
    ReplyKeyboardMarkup, KeyboardButton, 
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardRemove
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest
import asyncio
import logging
import os
from dotenv import load_dotenv
import redis.asyncio as redis
import random
import re

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

redis_client = None

# Кэши в памяти
_balance_cache: dict[int, float] = {}
_username_cache: dict[int, str] = {}
_name_cache: dict[int, str] = {}
_username_to_id_cache: dict[str, int] = {}
_name_to_id_cache: dict[str, int] = {}

TOKEN = os.getenv("TG_BOT_TOKEN")
REDIS_URL = os.getenv("REDIS_URL")
REDIS_TOKEN = os.getenv("REDIS_TOKEN")

# === ЧТЕНИЕ ADMIN_IDS ИЗ .ENV ===
raw_admin_ids = os.getenv("ADMIN_IDS", "")
if raw_admin_ids:
    try:
        ADMIN_IDS = {int(x.strip()) for x in raw_admin_ids.split(",") if x.strip()}
    except ValueError:
        logger.error("Неверный формат ADMIN_IDS в .env.")
        ADMIN_IDS = set()
else:
    ADMIN_IDS = set()

if not TOKEN:
    raise ValueError("Не задан токен бота.")
if not REDIS_URL or not REDIS_TOKEN:
    raise ValueError("Не заданы REDIS_URL и/или REDIS_TOKEN.")

bot = Bot(token=TOKEN)
dp = Dispatcher()
router = Router()

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# --- FSM ---
class TradingForm(StatesGroup):
    waiting_for_amount = State()
    waiting_for_direction = State()

class NameForm(StatesGroup):
    waiting_for_name = State()

# --- Инициализация Redis ---
async def init_redis():
    global redis_client
    redis_client = redis.from_url(REDIS_URL, password=REDIS_TOKEN, decode_responses=True)
    await redis_client.ping()
    logger.info("Redis: OK")

# --- Функции работы с балансом (через хеш + кэш) ---
async def get_balance(user_id: int) -> float:
    if user_id in _balance_cache:
        return _balance_cache[user_id]

    data = await redis_client.hgetall(f"user:{user_id}")
    balance = float(data.get("balance", "0"))
    _balance_cache[user_id] = balance
    return balance

async def add_to_balance(user_id: int, amount: float) -> float:
    new_balance = await redis_client.hincrbyfloat(f"user:{user_id}", "balance", amount)
    _balance_cache[user_id] = new_balance
    return new_balance

async def set_balance(user_id: int, amount: float):
    await redis_client.hset(f"user:{user_id}", mapping={"balance": str(amount)})
    _balance_cache[user_id] = amount

# --- Функции работы с username (через хеш + кэш) ---
async def save_user_info(user_id: int, username: str | None):
    clean = (username or "").lstrip("@").lower()
    if not clean:
        clean = "без_username"

    data = await redis_client.hgetall(f"user:{user_id}")
    current_username = data.get("username", "без_username")

    if current_username == clean:
        return

    if current_username != "без_username":
        await redis_client.delete(f"username:{current_username}:user_id")
        _username_to_id_cache.pop(current_username, None)

    await redis_client.hset(f"user:{user_id}", mapping={"username": clean})
    _username_cache[user_id] = clean

    await redis_client.set(f"username:{clean}:user_id", str(user_id))
    _username_to_id_cache[clean] = user_id

async def get_username(user_id: int) -> str:
    if user_id in _username_cache:
        return _username_cache[user_id]

    data = await redis_client.hgetall(f"user:{user_id}")
    username = data.get("username", "без_username")
    _username_cache[user_id] = username
    return username

async def get_user_id_by_username(username: str) -> int | None:
    clean = username.lstrip("@").lower()
    if clean in _username_to_id_cache:
        return _username_to_id_cache[clean]

    key = f"username:{clean}:user_id"
    value = await redis_client.get(key)
    if value:
        user_id = int(value)
        _username_to_id_cache[clean] = user_id
        return user_id
    return None

async def resolve_target(target_str: str) -> int | None:
    target_str = target_str.strip()
    try:
        return int(target_str)
    except ValueError:
        pass
    user_id = await get_user_id_by_username(target_str)
    return user_id

# --- Функции работы с именем (через хеш + кэш) ---
async def save_user_name(user_id: int, name: str):
    # Если у пользователя уже было имя — освобождаем старый маппинг
    old_name = await get_user_name(user_id)
    if old_name:
        await redis_client.delete(f"name:{old_name}:user_id")
        _name_to_id_cache.pop(old_name, None)

    await redis_client.hset(f"user:{user_id}", mapping={"name": name})
    _name_cache[user_id] = name

    # Создаём маппинг name → user_id для проверки уникальности
    await redis_client.set(f"name:{name}:user_id", str(user_id))
    _name_to_id_cache[name] = user_id

async def get_user_name(user_id: int) -> str | None:
    if user_id in _name_cache:
        return _name_cache[user_id]

    data = await redis_client.hgetall(f"user:{user_id}")
    name = data.get("name")
    if name:
        _name_cache[user_id] = name
        return name
    return None

async def is_name_taken(name: str) -> bool:
    """Проверяет, занят ли ник другим игроком."""
    if name in _name_to_id_cache:
        return True

    value = await redis_client.get(f"name:{name}:user_id")
    return value is not None

# --- Ферма (без EXISTS) ---
async def can_farm(user_id: int, cooldown_seconds: int = 2) -> bool:
    key = f"cooldown:{user_id}"
    ok = await redis_client.set(key, "1", nx=True, ex=cooldown_seconds)
    return ok is True

# --- Топ игроков (через хеши) ---
async def get_all_balances() -> list:
    keys = []
    async for key in redis_client.scan_iter(match="user:*", count=100):
        if key.endswith(":balance") or key.endswith(":username"):
            continue

        parts = key.split(":")
        if len(parts) != 2:
            continue

        try:
            user_id = int(parts[1])
        except ValueError:
            continue

        data = await redis_client.hgetall(key)
        if not data:
            continue

        balance = float(data.get("balance", "0"))
        name = data.get("name") or data.get("username", "без_username")
        keys.append((user_id, name, balance))

    keys.sort(key=lambda x: x[2], reverse=True)
    return keys

# --- Проверка имени ---
def is_valid_name(name: str) -> bool:
    """Русские и английские буквы + цифры, от 3 до 10 символов."""
    name = name.strip()
    if len(name) < 3 or len(name) > 10:
        return False
    return bool(re.match(r'^[a-zA-Zа-яА-ЯёЁ0-9]+$', name))

# --- Главное меню ---
async def send_main_menu(target: Message | CallbackQuery, user_id: int):
    balance = await get_balance(user_id)
    name = await get_user_name(user_id)
    display_name = name if name else "Игрок"
    text = f"🏙 Главное меню\n{display_name}, ваш баланс: {balance:.2f} ₽\nВыберите раздел:"
    try:
        photo = FSInputFile("images/glmenu.png")
        if isinstance(target, CallbackQuery):
            await target.message.answer_photo(photo=photo, caption=text, reply_markup=get_main_keyboard())
        else:
            await target.answer_photo(photo=photo, caption=text, reply_markup=get_main_keyboard())
    except FileNotFoundError:
        logger.warning("Файл images/glmenu.png не найден.")
        if isinstance(target, CallbackQuery):
            await target.message.answer(text, reply_markup=get_main_keyboard())
        else:
            await target.answer(text, reply_markup=get_main_keyboard())

# --- Клавиатуры ---

def get_main_keyboard():
    keyboard = [
        [KeyboardButton(text="💼 Работа"), KeyboardButton(text="🛒 Магаз")],
        [KeyboardButton(text="🏆 Топ")]
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

def get_work_keyboard():
    keyboard = [
        [KeyboardButton(text="🔗 Реф"), KeyboardButton(text="⛏ Шахта")],
        [KeyboardButton(text="📈 Трейдинг"), KeyboardButton(text="🧮 Математика")],
        [KeyboardButton(text="🔙 Назад")]
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

def get_mine_keyboard():
    keyboard = [[KeyboardButton(text="🎮 Фармить")], [KeyboardButton(text="🔙 Назад")]]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

def get_trading_direction_keyboard():
    keyboard = [
        [
            InlineKeyboardButton(text="📈 Вверх", callback_data="trade_up"),
            InlineKeyboardButton(text="📉 Вниз", callback_data="trade_down"),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_trading_result_keyboard():
    keyboard = [
        [
            InlineKeyboardButton(text="🎮 Продолжить играть", callback_data="trade_continue"),
            InlineKeyboardButton(text="🚪 Выйти", callback_data="trade_exit"),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_trading_result_keyboard2():
    keyboard = [[InlineKeyboardButton(text="🚪 Выйти", callback_data="trade_exit")]]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

# --- Игровые хендлеры ---

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await save_user_info(message.from_user.id, message.from_user.username)

    # Проверяем, есть ли уже имя
    name = await get_user_name(message.from_user.id)
    if not name:
        await message.answer(
            "👋 Привет! Как тебя зовут?\n"
            "Введи имя — русские или английские буквы и цифры (от 3 до 10 символов).\n"
            "Например: Alex123, Иван4, Макс777\n\n"
            "⚠️ Имя должно быть уникальным — если оно уже занято, придётся выбрать другое."
        )
        await state.set_state(NameForm.waiting_for_name)
        return

    await send_main_menu(message, message.from_user.id)

@router.message(NameForm.waiting_for_name)
async def process_name(message: Message, state: FSMContext):
    name = message.text.strip()
    user_id = message.from_user.id

    # Админ может ввести любой ник — без ограничений
    if not is_admin(user_id):
        if not is_valid_name(name):
            await message.answer(
                "❌ Имя должно содержать только русские или английские буквы и цифры (от 3 до 10 символов).\n"
                "Без пробелов и спецсимволов. Попробуй ещё раз:"
            )
            return

        if await is_name_taken(name):
            await message.answer(
                f"❌ Ник «{name}» уже занят. Выбери другой:"
            )
            return

    await save_user_name(user_id, name)
    await state.clear()
    await message.answer(f"👍 Приятно познакомиться, {name}!")
    await send_main_menu(message, user_id)

@router.message(F.text == "💼 Работа")
async def show_work_menu(message: Message):
    await message.answer("Выберите направление в работе:", reply_markup=get_work_keyboard())

@router.message(F.text == "🛒 Магаз")
async def show_shop_menu(message: Message):
    await message.answer("Раздел «Магаз» пока в разработке.")

@router.message(F.text == "🏆 Топ")
async def show_top(message: Message):
    user_id = message.from_user.id

    # Админ — без кулдауна
    if not is_admin(user_id):
        cooldown_key = f"cooldown:top:{user_id}"
        ok = await redis_client.set(cooldown_key, "1", nx=True, ex=60)
        if not ok:
            ttl = await redis_client.ttl(cooldown_key)
            if ttl > 0:
                await message.answer(f"⏳ Топ можно будет посмотреть через {ttl} сек.")
            else:
                await message.answer("⏳ Топ можно проверять раз в минуту. Подожди немного.")
            return

    balances = await get_all_balances()

    if not balances:
        await message.answer("🏆 Топ игроков\n\nПока нет данных.")
        return

    text = "🏆 Топ игроков по балансу:\n\n"
    for i, (uid, name, balance) in enumerate(balances[:10], 1):
        text += f"{i}. {name} ({uid}) — {balance:.2f} ₽\n"

    if len(balances) > 10:
        text += f"\n...и ещё {len(balances) - 10} игроков"

    await message.answer(text)

@router.message(F.text == "🔙 Назад")
async def handle_back(message: Message):
    await send_main_menu(message, message.from_user.id)

@router.message(F.text == "⛏ Шахта")
async def show_mine_menu(message: Message):
    await message.answer("Меню «Шахта». Нажмите «Фармить», чтобы заработать!", reply_markup=get_mine_keyboard())

@router.message(F.text == "⛏ Фармить")
async def handle_farm(message: Message):
    user_id = message.from_user.id

    if not await can_farm(user_id):
        await message.answer("⏳ Обожди секунду, шахта восстанавливается.")
        return

    new_balance = await add_to_balance(user_id, 1.0)

    await message.answer("⛏ Красава, ты заработал 1 рубль!\nТвой баланс: {new_balance:.2f}".format(new_balance=new_balance))

@router.message(F.text == "🔗 Реф")
async def handle_ref(message: Message):
    await message.answer("Вы выбрали «Реф».")

@router.message(F.text == "📈 Трейдинг")
async def handle_trading(message: Message, state: FSMContext):
    user_id = message.from_user.id
    balance = await get_balance(user_id)

    if balance <= 0:
        await message.answer(
            "У вас недостаточно средств для трейдинга. Сначала поработайте в шахте!",
            reply_markup=get_work_keyboard()
        )
        return

    await message.answer(
        f"💰 Ваш баланс: {balance:.2f} ₽\n"
        "Введите сумму ставки (число больше 0):",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(TradingForm.waiting_for_amount)

@router.message(TradingForm.waiting_for_amount)
async def process_trading_amount(message: Message, state: FSMContext):
    user_id = message.from_user.id

    try:
        amount = float(message.text)
        if amount <= 0:
            await message.answer("Сумма должна быть больше 0. Попробуйте ещё раз:")
            return
    except ValueError:
        await message.answer("Пожалуйста, введите корректное число (например, 100):")
        return

    balance = await get_balance(user_id)
    if amount > balance:
        await message.answer(
            f"Недостаточно средств! Ваш баланс: {balance:.2f} ₽\n"
            "Введите меньшую сумму:"
        )
        return

    await state.update_data(amount=amount)

    caption_text = f"📊 График актива\nСтавка: {amount:.2f} ₽\nКуда пойдёт график?"
    photo_path = "images/graph.png"

    if not os.path.exists(photo_path):
        logger.warning(f"Файл {photo_path} не найден. Отправляем только текст.")
        await message.answer(
            text=caption_text,
            reply_markup=get_trading_direction_keyboard()
        )
    else:
        try:
            photo = FSInputFile(photo_path)
            await message.answer_photo(
                photo=photo,
                caption=caption_text,
                reply_markup=get_trading_direction_keyboard()
            )
        except Exception as e:
            logger.error(f"Ошибка отправки фото: {e}")
            await message.answer(
                text=caption_text,
                reply_markup=get_trading_direction_keyboard()
            )

    await state.set_state(TradingForm.waiting_for_direction)

@router.callback_query(F.data.in_({"trade_up", "trade_down"}))
async def process_trading_direction(callback: CallbackQuery, state: FSMContext):
    await callback.answer()

    user_id = callback.from_user.id
    data = await state.get_data()
    amount = data.get("amount")

    if not amount:
        await callback.message.edit_text("Сессия истекла. Начните заново.")
        await state.clear()
        return

    actual_direction = "up" if random.random() < 0.5 else "down"
    user_direction = "up" if callback.data == "trade_up" else "down"

    if user_direction == actual_direction:
        await add_to_balance(user_id, amount)
        balance_after = await get_balance(user_id)
        result_text = (
            f"🎉 Победа! График пошёл {'вверх' if actual_direction == 'up' else 'вниз'}.\n"
            f"Вы выиграли {amount:.2f} ₽!\n"
            f"Ваш баланс: {balance_after:.2f} ₽"
        )
    else:
        await add_to_balance(user_id, -amount)
        balance_after = await get_balance(user_id)
        result_text = (
            f"😕 Проигрыш. График пошёл {'вверх' if actual_direction == 'up' else 'вниз'}.\n"
            f"Ваша ставка {amount:.2f} ₽ сгорела.\n"
            f"Ваш баланс: {balance_after:.2f} ₽"
        )

    try:
        await callback.message.edit_text(
            text=result_text,
            reply_markup=get_trading_result_keyboard()
        )
    except TelegramBadRequest as e:
        logger.warning(f"Не удалось отредактировать сообщение: {e}. Отправляем новое.")
        await callback.message.answer(
            text=result_text,
            reply_markup=get_trading_result_keyboard()
        )
        await callback.message.delete()

    await state.update_data(amount=None)

@router.callback_query(F.data == "trade_continue")
async def process_trade_continue(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    user_id = callback.from_user.id
    balance = await get_balance(user_id)

    if balance <= 0:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await callback.message.answer(
            "❌ У вас недостаточно средств для новой ставки.",
            reply_markup=get_work_keyboard()
        )
        await state.clear()
        return

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    await callback.message.answer(
        "Введите новую сумму ставки (число больше 0):",
        reply_markup=get_trading_result_keyboard2()
    )
    await state.set_state(TradingForm.waiting_for_amount)

@router.callback_query(F.data == "trade_exit")
async def process_trade_exit(callback: CallbackQuery, state: FSMContext):
    await callback.answer()

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    await state.clear()
    await callback.message.answer(
        "🚪 Вы вышли из трейдинга.",
        reply_markup=get_work_keyboard()
    )

@router.message(F.text == "🧮 Математика")
async def handle_math(message: Message):
    await message.answer("Вы выбрали «Математика».")

# --- Универсальный хендлер ---
@router.message(F.text)
async def handle_unknown_text(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Используй кнопки 😡")

dp.include_router(router)

async def main():
    logger.info("Запуск бота...")
    try:
        await init_redis()
    except Exception as e:
        logger.error(f"Redis: ошибка подключения — {e}")
        return
    await dp.start_polling(bot, drop_pending_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
