from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, FSInputFile,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardRemove,
    InputMediaPhoto
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile
from io import BytesIO
import asyncio
import logging
import os
import time
import json
import uuid
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv
import redis.asyncio as redis
import random
import re
from PIL import Image, ImageDraw, ImageFont

load_dotenv()

# --- Логирование: ротация в файл + дублирование в stdout ---
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_fh = RotatingFileHandler(
    "bot.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_fh.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
_fh.setFormatter(_fmt)
logger.addHandler(_fh)

_sh = logging.StreamHandler()
_sh.setLevel(logging.INFO)
_sh.setFormatter(_fmt)
logger.addHandler(_sh)

# --- Время старта для /ping ---
start_time = time.time()

redis_client = None

# Кэши в памяти
_balance_cache: dict[int, int] = {}
_username_cache: dict[int, str] = {}
_name_cache: dict[int, str] = {}
_username_to_id_cache: dict[str, int] = {}
_name_to_id_cache: dict[str, int] = {}
_farm_cooldown_cache: dict[int, float] = {}
_math_cooldown_cache: dict[int, float] = {}

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

class MathForm(StatesGroup):
    waiting_for_answer = State()

class BusinessForm(StatesGroup):
    waiting_for_raw = State()


class RouletteForm(StatesGroup):
    waiting_for_amount = State()
    waiting_for_bet = State()

class MineForm(StatesGroup):
    in_mine = State()

# ============================================================
# ЭКОНОМИКА: КОНСТАНТЫ
# ============================================================

MINE_REWARD = 200
MINE_COOLDOWN = 10
MATH_REWARD = 400
MATH_COOLDOWN = 15
RAW_PRICE = 1

# ============================================================
# ЭКОНОМИКА: БИЗНЕСЫ
# ============================================================

BUSINESS_LIST = [
    {
        "name": "Маленький ларёк",
        "price": 50_000,
        "income_per_min": 250,
        "raw_consumption_per_min": 100,
        "raw_capacity": 120_000,
    },
    {
        "name": "Шаурмечка",
        "price": 250_000,
        "income_per_min": 800,
        "raw_consumption_per_min": 320,
        "raw_capacity": 350_000,
    },
    {
        "name": "Ферма",
        "price": 1_000_000,
        "income_per_min": 2_500,
        "raw_consumption_per_min": 1_000,
        "raw_capacity": 960_000,
    },
    {
        "name": "Заправка",
        "price": 3_000_000,
        "income_per_min": 10_000,
        "raw_consumption_per_min": 4_000,
        "raw_capacity": 3_360_000,
    },
    {
        "name": "Гипермаркет",
        "price": 15_000_000,
        "income_per_min": 50_000,
        "raw_consumption_per_min": 120_000,
        "raw_capacity": 86_400_000,
    },
    {
        "name": "Аэропорт",
        "price": 500_000_000,
        "income_per_min": 1_250_000,
        "raw_consumption_per_min": 500_000,
        "raw_capacity": 300_000_000,
    },
]

UPGRADE_INCOME_MULT = {2: 1.5, 3: 2.0}
UPGRADE_CONSUMPTION_MULT = {2: 1.2, 3: 1.4}
UPGRADE_CAPACITY_MULT = {2: 2.0, 3: 1.5}

# --- Вспомогательная функция: форматирование времени ---
def format_time(minutes: float | None) -> str:
    if minutes is None:
        return "∞"
    if minutes < 1:
        return f"{int(minutes * 60)} сек"
    if minutes < 60:
        return f"{int(minutes)} мин"
    hours = int(minutes / 60)
    if hours < 24:
        return f"{hours} ч"
    days = int(hours / 24)
    return f"{days} дн"

# --- Бизнес: хелперы ---
def biz_upgrade_cost(biz):
    lvl = biz.get("level", 1)
    if lvl == 1:
        return biz["price"] // 2
    elif lvl == 2:
        return biz["price"]
    return None

def biz_sell_price(biz):
    return biz["price"] // 2 + biz.get("raw_stock", 0) * RAW_PRICE // 2 + biz.get("balance", 0)

def biz_net_profit_per_min(biz):
    income = biz.get("income_per_min", 0)
    consumption = biz.get("raw_consumption_per_min", 0)
    return income - consumption * RAW_PRICE

def biz_time_until_empty(biz):
    consumption = biz.get("raw_consumption_per_min", 0)
    stock = biz.get("raw_stock", 0)
    if consumption <= 0 or stock <= 0:
        return None
    return stock / consumption

def biz_settle(biz):
    """Начисляет доход и списывает сырьё за прошедшее время."""
    last = biz.get("last_collected", time.time())
    now = time.time()
    minutes = (now - last) / 60.0

    if minutes <= 0:
        return biz

    income_rate = biz.get("income_per_min", 0)
    consumption_rate = biz.get("raw_consumption_per_min", 0)
    stock = biz.get("raw_stock", 0)

    if stock <= 0 or income_rate <= 0:
        biz["last_collected"] = now
        return biz

    if consumption_rate <= 0:
        biz["balance"] = int(biz.get("balance", 0) + income_rate * minutes)
        biz["last_collected"] = now
        return biz

    max_run_minutes = stock / consumption_rate

    if minutes <= max_run_minutes:
        biz["balance"] = int(biz.get("balance", 0) + income_rate * minutes)
        biz["raw_stock"] = int(stock - consumption_rate * minutes)
    else:
        biz["balance"] = int(biz.get("balance", 0) + income_rate * max_run_minutes)
        biz["raw_stock"] = 0

    biz["last_collected"] = now
    return biz

async def settle_and_save_biz(user_id, biz):
    if biz is None:
        return None
    biz_settle(biz)
    await save_biz(user_id, biz)
    return biz

async def get_biz(user_id):
    data = await redis_client.hgetall(f"user:{user_id}")
    raw = data.get("business", "")
    if not raw:
        return None
    try:
        biz = json.loads(raw)
    except Exception:
        return None

    for key in ("income_per_min", "raw_consumption_per_min", "raw_capacity",
                "raw_stock", "balance", "price", "level"):
        if key in biz and biz[key] is not None:
            biz[key] = int(biz[key])

    last_str = data.get("business_last_collected")
    if last_str:
        try:
            biz["last_collected"] = float(last_str)
        except ValueError:
            pass

    return biz

async def save_biz(user_id, biz):
    data = {
        "business": json.dumps(biz) if biz else "",
        "business_income_per_min": str(biz.get("income_per_min", 0)) if biz else "0",
    }
    if biz and "last_collected" in biz:
        data["business_last_collected"] = str(biz["last_collected"])
    await redis_client.hset(f"user:{user_id}", mapping=data)

# --- Бизнес: генерация текстов и клавиатур ---
def biz_manage_view(biz):
    biz_balance = biz.get("balance", 0)
    consumption = biz.get("raw_consumption_per_min", 0)
    net_profit = biz_net_profit_per_min(biz)
    time_left = biz_time_until_empty(biz)
    text = (
        f"🏪 Ваш бизнес: «{biz['name']}»\n\n"
        f"📈 Уровень: {biz.get('level', 1)}/3\n"
        f"💰 Доход: {biz.get('income_per_min', 0):,} ₽/мин\n"
        f"📦 Расход сырья: {consumption:,}/мин\n"
        f"💵 Чистая прибыль: {net_profit:,} ₽/мин\n"
        f"📦 Сырьё: {biz.get('raw_stock', 0):,}/{biz.get('raw_capacity', 30000):,}\n"
        f"⏳ Хватит на: {format_time(time_left)}\n"
        f"🏦 Баланс бизнеса: {biz_balance:,} ₽\n\n"
    )
    if biz.get("raw_stock", 0) <= 0:
        text += "⚠️ Бизнес не работает — нет сырья!\nНажмите «📦 Склад» чтобы закупить."
    else:
        text += "✅ Бизнес работает!"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📦 Склад", callback_data="biz_wh"),
            InlineKeyboardButton(text="📈 Уровень", callback_data="biz_up"),
        ],
        [InlineKeyboardButton(text="💰 Собрать доход", callback_data="biz_collect")],
        [InlineKeyboardButton(text="💸 Продать", callback_data="biz_sell")],
        [
            InlineKeyboardButton(text="🔄 Обновить", callback_data="biz_refresh"),
            InlineKeyboardButton(text="🔙 Выйти", callback_data="biz_exit"),
        ],
    ])
    return text, kb

def biz_no_biz_view():
    text = "🏪 Мои бизнесы\n\nУ вас нет бизнеса.\nНажмите кнопку ниже чтобы выбрать."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Купить бизнес", callback_data="biz_car:0")]
    ])
    return text, kb

def biz_carousel_view(idx, balance):
    biz = BUSINESS_LIST[idx]
    can_buy = balance >= biz["price"]
    consumption = biz["raw_consumption_per_min"]
    net = biz["income_per_min"] - consumption * RAW_PRICE
    full_stock_cost = biz["raw_capacity"] * RAW_PRICE
    run_time = biz["raw_capacity"] / consumption if consumption > 0 else 0
    payback_min = biz["price"] / net if net > 0 else 0
    text = (
        f"🏪 Покупка бизнеса\n\n"
        f"🏗 {biz['name']}\n"
        f"💰 Стоимость: {biz['price']:,} ₽\n"
        f"📈 Доход: {biz['income_per_min']:,} ₽/мин\n"
        f"📦 Расход сырья: {consumption:,}/мин\n"
        f"💵 Чистая прибыль: {net:,} ₽/мин\n"
        f"📦 Склад: {biz['raw_capacity']:,}\n"
        f"💸 Заполнить склад: {full_stock_cost:,} ₽\n"
        f"⏳ Полный склад хватит на: {format_time(run_time)}\n"
        f"📊 Окупаемость: {format_time(payback_min)}\n\n"
        f"Ваш баланс: {balance:,} ₽"
    )
    nav = []
    if idx > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"biz_car:{idx-1}"))
    nav.append(InlineKeyboardButton(text=f"{idx+1}/{len(BUSINESS_LIST)}", callback_data="biz_noop"))
    if idx < len(BUSINESS_LIST) - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"biz_car:{idx+1}"))
    rows = [nav]
    if can_buy:
        rows.append([InlineKeyboardButton(text=f"✅ Купить за {biz['price']:,} ₽", callback_data=f"biz_buy:{idx}")])
    else:
        rows.append([InlineKeyboardButton(text=f"❌ Не хватает {biz['price']:,} ₽", callback_data="biz_noop")])
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="biz_manage")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)

def biz_warehouse_view(biz):
    stock = biz.get("raw_stock", 0)
    capacity = biz.get("raw_capacity", 30000)
    biz_balance = biz.get("balance", 0)
    time_left = biz_time_until_empty(biz)
    text = (
        f"📦 Склад «{biz['name']}»\n\n"
        f"Сырьё: {stock:,}/{capacity:,}\n"
        f"Цена: {RAW_PRICE} ₽ за единицу\n"
        f"📦 Расход: {biz.get('raw_consumption_per_min', 0):,}/мин\n"
        f"⏳ Хватит на: {format_time(time_left)}\n"
        f"🏦 Баланс бизнеса: {biz_balance:,} ₽\n\n"
        f"Выберите источник оплаты:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Пополнить с основного баланса", callback_data="biz_wh:user")],
        [InlineKeyboardButton(text="🏪 Пополнить с баланса бизнеса", callback_data="biz_wh:biz")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="biz_manage")],
    ])
    return text, kb

def biz_upgrade_view(biz):
    level = biz.get("level", 1)
    cost = biz_upgrade_cost(biz)
    if cost is None:
        text = f"📈 «{biz['name']}»\n\nУровень: {level}/3 — максимальный!"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="biz_manage")]
        ])
        return text, kb
    new_level = level + 1
    income_mult = UPGRADE_INCOME_MULT[new_level]
    consumption_mult = UPGRADE_CONSUMPTION_MULT[new_level]
    capacity_mult = UPGRADE_CAPACITY_MULT[new_level]

    new_income = int(biz["income_per_min"] * income_mult)
    new_consumption = int(biz.get("raw_consumption_per_min", 0) * consumption_mult)
    new_capacity = int(biz.get("raw_capacity", 30000) * capacity_mult)
    new_net = new_income - new_consumption * RAW_PRICE
    current_net = biz_net_profit_per_min(biz)

    text = (
        f"📈 Улучшение «{biz['name']}»\n\n"
        f"Текущий уровень: {level}/3\n"
        f"💰 Доход: {biz['income_per_min']:,} ₽/мин\n"
        f"📦 Расход сырья: {biz.get('raw_consumption_per_min', 0):,}/мин\n"
        f"💵 Чистая прибыль: {current_net:,} ₽/мин\n"
        f"📦 Склад: {biz.get('raw_capacity', 30000):,}\n\n"
        f"⬆️ Уровень {new_level}:\n"
        f"💰 Доход: {new_income:,} ₽/мин\n"
        f"📦 Расход сырья: {new_consumption:,}/мин\n"
        f"💵 Чистая прибыль: {new_net:,} ₽/мин\n"
        f"📦 Склад: {new_capacity:,}\n"
        f"Стоимость: {cost:,} ₽"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"🛒 За {cost:,} ₽ (баланс)", callback_data="biz_up_do:user"),
            InlineKeyboardButton(text=f"🏪 За {cost:,} ₽ (бизнес)", callback_data="biz_up_do:biz"),
        ],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="biz_manage")]
    ])
    return text, kb

def biz_sell_view(biz):
    sell_price = biz_sell_price(biz)
    text = (
        f"💸 Продажа «{biz['name']}»\n\n"
        f"Вы получите: {sell_price:,} ₽\n"
        f"(50% стоимости + 50% сырья + баланс бизнеса)\n\n"
        f"Нажмите «Подтвердить продажу», чтобы продолжить."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить продажу", callback_data="biz_sell_confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="biz_manage")],
    ])
    return text, kb

async def biz_edit(callback, text, kb):
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=kb)

# --- Инициализация Redis ---
async def init_redis():
    global redis_client
    redis_client = redis.from_url(REDIS_URL, password=REDIS_TOKEN, decode_responses=True)
    await redis_client.ping()
    logger.info("Redis: OK")

# --- Функции работы с балансом ---
async def get_balance(user_id: int) -> int:
    if user_id in _balance_cache:
        return _balance_cache[user_id]
    data = await redis_client.hgetall(f"user:{user_id}")
    balance = int(float(data.get("balance", "0")))
    _balance_cache[user_id] = balance
    return balance

async def add_to_balance(user_id: int, amount: int) -> int:
    new_balance = await redis_client.hincrby(f"user:{user_id}", "balance", amount)
    _balance_cache[user_id] = new_balance
    return new_balance

async def set_balance(user_id: int, amount: int):
    await redis_client.hset(f"user:{user_id}", mapping={"balance": str(amount)})
    _balance_cache[user_id] = amount

# --- Функции работы с username ---
async def save_user_info(user_id: int, username: str | None):
    clean = (username or "").lstrip("@").lower()
    if not clean:
        clean = "без_username"
    data = await redis_client.hgetall(f"user:{user_id}")
    current_username = data.get("username", "без_username")
    if current_username == clean:
        _username_cache[user_id] = clean
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
    return await get_user_id_by_username(target_str)

# --- Функции работы с именем ---
async def save_user_name(user_id: int, name: str):
    data = await redis_client.hgetall(f"user:{user_id}")
    old_name = data.get("name")
    if old_name:
        await redis_client.delete(f"name:{old_name}:user_id")
        _name_to_id_cache.pop(old_name, None)
    await redis_client.hset(f"user:{user_id}", mapping={"name": name})
    _name_cache[user_id] = name
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
    if name in _name_to_id_cache:
        return True
    value = await redis_client.get(f"name:{name}:user_id")
    return value is not None

async def get_user_id_by_name_direct(name: str) -> int | None:
    key = f"name:{name}:user_id"
    value = await redis_client.get(key)
    if value:
        return int(value)
    return None

# --- Фарм ---
async def can_farm(user_id: int, cooldown_seconds: int = MINE_COOLDOWN) -> tuple[bool, int]:
    now = time.time()
    if user_id in _farm_cooldown_cache:
        remaining = _farm_cooldown_cache[user_id] - now
        if remaining > 0:
            return False, int(remaining)
        del _farm_cooldown_cache[user_id]
    key = f"cooldown:farm:{user_id}"
    ok = await redis_client.set(key, "1", nx=True, ex=cooldown_seconds)
    if ok:
        _farm_cooldown_cache[user_id] = now + cooldown_seconds
        return True, 0
    else:
        ttl = await redis_client.ttl(key)
        _farm_cooldown_cache[user_id] = now + max(ttl, 0)
        return False, max(ttl, 0)

# --- Математика: кулдаун ---
async def can_math(user_id: int, cooldown_seconds: int = MATH_COOLDOWN) -> tuple[bool, int]:
    now = time.time()
    if user_id in _math_cooldown_cache:
        remaining = _math_cooldown_cache[user_id] - now
        if remaining > 0:
            return False, int(remaining)
        del _math_cooldown_cache[user_id]
    key = f"cooldown:math:{user_id}"
    ok = await redis_client.set(key, "1", nx=True, ex=cooldown_seconds)
    if ok:
        _math_cooldown_cache[user_id] = now + cooldown_seconds
        return True, 0
    else:
        ttl = await redis_client.ttl(key)
        _math_cooldown_cache[user_id] = now + max(ttl, 0)
        return False, max(ttl, 0)

# --- Топ игроков ---
async def get_all_balances() -> list[tuple[int, str, int]]:
    results = []
    async for key in redis_client.scan_iter(match="user:*", count=100):
        if not key.startswith("user:") or key.count(":") != 1:
            continue
        _, user_id_str = key.split(":", 1)
        try:
            user_id = int(user_id_str)
        except ValueError:
            continue
        data = await redis_client.hgetall(key)
        if not data:
            continue
        balance_str = data.get("balance", "0")
        try:
            balance = int(float(balance_str))
        except ValueError:
            balance = 0
        name = data.get("name") or data.get("username", "Игрок")
        results.append((user_id, name, balance))
    results.sort(key=lambda x: x[2], reverse=True)
    return results

# --- Проверка имени ---
def is_valid_name(name: str) -> bool:
    name = name.strip()
    if len(name) < 3 or len(name) > 10:
        return False
    return bool(re.match(r'^[a-zA-Zа-яА-ЯёЁ0-9]+$', name))

# --- Главное меню ---
async def send_main_menu(target: Message | CallbackQuery, user_id: int):
    if user_id in _balance_cache and user_id in _name_cache:
        balance = _balance_cache[user_id]
        name = _name_cache[user_id]
    else:
        data = await redis_client.hgetall(f"user:{user_id}")
        balance = _balance_cache.get(user_id)
        if balance is None:
            balance = int(float(data.get("balance", "0")))
            _balance_cache[user_id] = balance
        name = _name_cache.get(user_id)
        if name is None:
            name = data.get("name")
            if name:
                _name_cache[user_id] = name
    display_name = name or "Игрок"
    text = f"🏙 Главное меню\n{display_name}, ваш баланс: {balance:,} ₽\nВыберите раздел:"
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
        [KeyboardButton(text="🎰 Казино"), KeyboardButton(text="🏆 Топ")]
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def get_casino_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎡 Рулетка")],
            [KeyboardButton(text="🔙 В меню")]
        ],
        resize_keyboard=True
    )


def get_roulette_amount_keyboard(amount: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💰 Ставка {amount:,} ₽", callback_data="roulette_amount_noop")],
        [InlineKeyboardButton(text="🔙 В казино", callback_data="casino_menu")],
    ])


def get_roulette_bet_keyboard(amount: int = 0):
    # Европейская рулетка: 0 + 1..36, с логикой цвета через эмодзи.
    rows = [
        [
            InlineKeyboardButton(text="➗ 0.5", callback_data="roulette_mul:0.5"),
            InlineKeyboardButton(text=f"💰 {amount:,} ₽", callback_data="roulette_amount_noop"),
            InlineKeyboardButton(text="✖️ 2", callback_data="roulette_mul:2"),
        ],
        [
            InlineKeyboardButton(text="🔴 красное", callback_data="roulette_bet:red"),
            InlineKeyboardButton(text="🟢 зеро", callback_data="roulette_bet:0"),
            InlineKeyboardButton(text="⚫ чёрное", callback_data="roulette_bet:black"),
        ],
        [
            InlineKeyboardButton(text="нечёт", callback_data="roulette_bet:odd"),
            InlineKeyboardButton(text="чёт", callback_data="roulette_bet:even"),
        ],
        [
            InlineKeyboardButton(text="1–18", callback_data="roulette_bet:low"),
            InlineKeyboardButton(text="19–36", callback_data="roulette_bet:high"),
        ],
        [
            InlineKeyboardButton(text="1-12", callback_data="roulette_bet:dozen1"),
            InlineKeyboardButton(text="13-24", callback_data="roulette_bet:dozen2"),
            InlineKeyboardButton(text="25-36", callback_data="roulette_bet:dozen3"),
        ],
        [InlineKeyboardButton(text="🔙 назад", callback_data="casino_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def get_work_keyboard():
    keyboard = [
        [KeyboardButton(text="🔗 Реф"), KeyboardButton(text="⛏ Шахта")],
        [KeyboardButton(text="📈 Трейдинг"), KeyboardButton(text="🧮 Математика")],
        [KeyboardButton(text="🏪 Мои бизнесы")],
        [KeyboardButton(text="🔙 В главное меню")]
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

def get_mine_keyboard():
    buttons = [
        [InlineKeyboardButton(text="⛏ Фармить", callback_data="mine_farm")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="mine_exit")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

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

def get_math_keyboard():
    keyboard = [
        [
            InlineKeyboardButton(text="➡ Следующий пример", callback_data="math_next"),
            InlineKeyboardButton(text="🚪 Выйти", callback_data="math_exit"),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

# ============================================================
# ГЕНЕРАЦИЯ КАРТИНКИ ДЛЯ МАТЕМАТИКИ (ОПТИМИЗИРОВАНО)
# ============================================================

_font_cache = None
_images_dir_ready = False

def _get_font(size=48):
    global _font_cache
    if _font_cache is not None:
        return _font_cache
    paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "arial.ttf",
    ]
    for path in paths:
        try:
            _font_cache = ImageFont.truetype(path, size)
            return _font_cache
        except Exception:
            continue
    _font_cache = ImageFont.load_default()
    return _font_cache

def _ensure_images_dir():
    global _images_dir_ready
    if not _images_dir_ready:
        os.makedirs("images", exist_ok=True)
        _images_dir_ready = True

def _generate_image_in_memory(problem_text: str) -> bytes:
    img = Image.new("RGB", (400, 150), color=(30, 30, 30))
    draw = ImageDraw.Draw(img)
    font = _get_font(48)
    bbox = draw.textbbox((0, 0), problem_text, font=font)
    left, top, right, bottom = bbox
    text_w = right - left
    text_h = bottom - top

    # Центрируем текст
    x = (img.width - text_w) // 2
    y = (img.height - text_h) // 2

    draw.text((x, y), problem_text, fill=(255, 255, 255), font=font)

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

async def generate_math_problem(user_id: int) -> tuple[str, int]:
    a = random.randint(1, 50)
    b = random.randint(1, 50)
    operation = random.choice(["+", "-", "×"])
    if operation == "+":
        answer = a + b
    elif operation == "-":
        if a < b:
            a, b = b, a
        answer = a - b
    else:
        a = random.randint(2, 15)
        b = random.randint(2, 15)
        answer = a * b
    problem_text = f"{a} {operation} {b} = ?"
    return problem_text, answer

# ============================================================
# ХЕНДЛЕРЫ
# ============================================================

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    username = message.from_user.username
    await save_user_info(user_id, username)
    if username:
        _username_cache[user_id] = username.lstrip("@").lower()
    name = await get_user_name(user_id)
    if not name:
        await message.answer(
            "👋 Привет! Как тебя зовут?\n"
            "Введи имя — русские или английские буквы и цифры (от 3 до 10 символов).\n"
            "Например: Alex123, Иван4, Макс777\n\n"
            "⚠️ Имя должно быть уникальным — если оно уже занято, придётся выбрать другое."
        )
        await state.set_state(NameForm.waiting_for_name)
        return
    await send_main_menu(message, user_id)

@router.message(Command("ping"))
async def cmd_ping(message: Message):
    try:
        await redis_client.ping()
        redis_ok = "✅ Redis OK"
    except Exception as e:
        redis_ok = f"❌ Redis ERROR: {e}"
        logger.error(f"Redis healthcheck failed: {e}")
    uptime = int(time.time() - start_time)
    days, rem = divmod(uptime, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    uptime_str = f"{days} дн {hours} ч {mins} мин"
    await message.answer(
        f"🤖 Бот работает\n{redis_ok}\n⏳ Uptime: {uptime_str}"
    )

@router.message(NameForm.waiting_for_name)
async def process_name(message: Message, state: FSMContext):
    name = message.text.strip()
    user_id = message.from_user.id
    if not is_admin(user_id):
        if not is_valid_name(name):
            await message.answer(
                "❌ Имя должно содержать только русские или английские буквы и цифры (от 3 до 10 символов).\n"
                "Без пробелов и спецсимволов. Попробуй ещё раз:"
            )
            return
        if await is_name_taken(name):
            await message.answer(f"❌ Ник «{name}» уже занят. Выбери другой:")
            return
    else:
        if not is_valid_name(name):
            await message.answer("⚠️ Админ: имя должно быть 3–10 символов, буквы и цифры. Исправь:")
            return
        existing_id = await get_user_id_by_name_direct(name)
        if existing_id and existing_id != user_id:
            await message.answer(f"⚠️ Ник «{name}» уже используется игроком {existing_id}. Он будет перезаписан.")
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
    if not is_admin(user_id):
        cooldown_key = f"cooldown:top:{user_id}"
        ok = await redis_client.set(cooldown_key, "1", nx=True, ex=60)
        if not ok:
            ttl = await redis_client.ttl(cooldown_key)
            if ttl > 0:
                await message.answer(f"⏳ Топ можно будет посмотреть через {ttl} сек.")
            else:
                await redis_client.set(cooldown_key, "1", nx=True, ex=60)
                await message.answer("⏳ Топ можно проверять раз в минуту. Подожди немного.")
            return

    balances = await get_all_balances()
    if not balances:
        await message.answer("🏆 Топ игроков\n\nПока нет данных.")
        return

    text = "🏆 Топ игроков по балансу:\n\n"
    for i, (uid, name, balance) in enumerate(balances[:10], 1):
        # Убрали uid — показываем только позицию, имя и баланс
        text += f"{i}. {name} — {balance:,} ₽\n"

    if len(balances) > 10:
        text += f"\n...и ещё {len(balances) - 10} игроков"

    await message.answer(text)

# --- ВОЗВРАТЫ ---
@router.message(F.text == "🔙 Назад")
async def handle_back(message: Message):
    await send_main_menu(message, message.from_user.id)

@router.message(F.text.in_({"🔙 В главное меню", "🔙 В меню"}))
async def handle_back_to_main(message: Message, state: FSMContext):
    await state.clear()
    await send_main_menu(message, message.from_user.id)

@router.message(F.text == "⛏ Шахта")
async def show_mine_menu(message: Message):
    await message.answer(
        f"⛏ Шахта\n\n"
        f"Заработок: {MINE_REWARD:,} ₽ за клик\n"
        f"Кулдаун: {MINE_COOLDOWN} сек\n"
        f"Нажмите «Фармить», чтобы заработать!",
        reply_markup=get_mine_keyboard()
    )

@router.callback_query(F.data == "mine_farm")
async def handle_mine_farm(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id

    allowed, remaining = await can_farm(user_id, cooldown_seconds=MINE_COOLDOWN)
    if not allowed:
        await callback.answer(f"⏳ Осталось: {remaining} сек.", show_alert=True)
        return

    await callback.answer()
    new_balance = await add_to_balance(user_id, MINE_REWARD)

    text = (
        f"⛏ Красава, ты заработал {MINE_REWARD:,} ₽!\n"
        f"Твой баланс: {new_balance:,} ₽"
    )
    kb = get_mine_keyboard()

    data = await state.get_data()
    farm_msg_id = data.get("farm_msg_id")

    if farm_msg_id:
        # Последующие клики — редактируем сообщение с результатом
        try:
            await callback.bot.edit_message_text(
                text=text,
                chat_id=callback.message.chat.id,
                message_id=farm_msg_id,
                reply_markup=kb,
            )
            return
        except TelegramBadRequest:
            pass  # сообщение удалили — отправим новое ниже

    # Первый клик — отправляем новое сообщение, меню не трогаем
    sent = await callback.message.answer(text, reply_markup=kb)
    await state.update_data(farm_msg_id=sent.message_id)

@router.callback_query(F.data == "mine_exit")
async def handle_mine_exit(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.answer("Выберите направление в работе:", reply_markup=get_work_keyboard())

@router.message(F.text == "🔗 Реф")
async def handle_ref(message: Message):
    await message.answer("Вы выбрали «Реф».")

# --- ТРЕЙДИНГ ---
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
    await message.answer("📈 Трейдинг открыт!", reply_markup=ReplyKeyboardRemove())
    sent = await message.answer(
        f"💰 Ваш баланс: {balance:,} ₽\n"
        "Введите сумму ставки (целое число больше 0):",
        reply_markup=get_trading_result_keyboard2()
    )
    await state.update_data(amount_msg_id=sent.message_id)
    await state.set_state(TradingForm.waiting_for_amount)

@router.message(TradingForm.waiting_for_amount)
async def process_trading_amount(message: Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    amount_msg_id = data.get("amount_msg_id")
    try:
        amount = int(message.text)
        if amount <= 0:
            await message.answer("Сумма должна быть больше 0. Попробуйте ещё раз:")
            return
    except ValueError:
        await message.answer("Пожалуйста, введите целое число (например, 100):")
        return
    balance = await get_balance(user_id)
    if amount > balance:
        await message.answer(f"Недостаточно средств! Ваш баланс: {balance:,} ₽\nВведите меньшую сумму:")
        return
    if amount_msg_id:
        try:
            await message.bot.edit_message_reply_markup(
                chat_id=message.chat.id, message_id=amount_msg_id, reply_markup=None
            )
        except TelegramBadRequest:
            pass
    await state.update_data(amount=amount)
    caption_text = f"📊 График актива\nСтавка: {amount:,} ₽\nКуда пойдёт график?"
    photo_path = "images/graph.png"
    if not os.path.exists(photo_path):
        logger.warning(f"Файл {photo_path} не найден. Отправляем только текст.")
        await message.answer(text=caption_text, reply_markup=get_trading_direction_keyboard())
    else:
        try:
            photo = FSInputFile(photo_path)
            await message.answer_photo(photo=photo, caption=caption_text, reply_markup=get_trading_direction_keyboard())
        except Exception as e:
            logger.error(f"Ошибка отправки фото: {e}")
            await message.answer(text=caption_text, reply_markup=get_trading_direction_keyboard())
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
            f"Вы выиграли {amount:,} ₽!\n"
            f"Ваш баланс: {balance_after:,} ₽"
        )
    else:
        await add_to_balance(user_id, -amount)
        balance_after = await get_balance(user_id)
        result_text = (
            f"😕 Проигрыш. График пошёл {'вверх' if actual_direction == 'up' else 'вниз'}.\n"
            f"Ваша ставка {amount:,} ₽ сгорела.\n"
            f"Ваш баланс: {balance_after:,} ₽"
        )
    try:
        await callback.message.edit_text(text=result_text, reply_markup=get_trading_result_keyboard())
    except TelegramBadRequest as e:
        logger.warning(f"Не удалось отредактировать сообщение: {e}. Отправляем новое.")
        await callback.message.answer(text=result_text, reply_markup=get_trading_result_keyboard())
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
        await callback.message.answer("❌ У вас недостаточно средств для новой ставки.", reply_markup=get_work_keyboard())
        await state.clear()
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    sent = await callback.message.answer(
        f"💰 Ваш баланс: {balance:,} ₽\nВведите сумму ставки (целое число больше 0):",
        reply_markup=get_trading_result_keyboard2()
    )
    await state.update_data(amount_msg_id=sent.message_id)
    await state.set_state(TradingForm.waiting_for_amount)

@router.callback_query(F.data == "trade_exit")
async def process_trade_exit(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await state.clear()
    await callback.message.answer("🚪 Вы вышли из трейдинга.", reply_markup=get_work_keyboard())

# ============================================================
# МАТЕМАТИКА
# ============================================================

@router.message(F.text == "🧮 Математика")
async def handle_math(message: Message, state: FSMContext):
    """Вход в математику — без кулдауна, первый пример сразу."""
    problem_text, answer = await generate_math_problem(message.from_user.id)
    await state.update_data(math_answer=answer)
    await state.set_state(MathForm.waiting_for_answer)

    await message.answer(
        f"🧮 Математика началась! За правильный ответ: {MATH_REWARD:,} ₽",
        reply_markup=ReplyKeyboardRemove()
    )
    sent = await message.answer(
        f"🧮 Реши пример!\n\n<b>{problem_text}</b>\n\nНапиши ответ числом:",
        parse_mode="HTML",
        reply_markup=get_math_keyboard()
    )
    await state.update_data(problem_msg_id=sent.message_id)

@router.message(MathForm.waiting_for_answer)
async def process_math_answer(message: Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    correct_answer = data.get("math_answer")
    problem_msg_id = data.get("problem_msg_id")
    if correct_answer is None:
        await message.answer("Этот пример уже решён. Нажми «Следующий пример»!")
        return
    try:
        user_answer = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введи число — ответ примера:")
        return
    if problem_msg_id:
        try:
            await message.bot.edit_message_reply_markup(
                chat_id=message.chat.id, message_id=problem_msg_id, reply_markup=None
            )
        except TelegramBadRequest:
            pass
    if user_answer == correct_answer:
        new_balance = await add_to_balance(user_id, MATH_REWARD)
        result_text = f"✅ Верно! +{MATH_REWARD:,} ₽!\nТвой баланс: {new_balance:,} ₽"
    else:
        result_text = f"❌ Неверно! Правильный ответ: {correct_answer}"
    await message.answer(result_text, reply_markup=get_math_keyboard())
    await state.update_data(math_answer=None)

@router.callback_query(F.data == "math_next")
async def process_math_next(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id

    allowed, remaining = await can_math(user_id, cooldown_seconds=MATH_COOLDOWN)
    if not allowed:
        await callback.answer(f"⏳ КД: {remaining} сек.", show_alert=True)
        return

    await callback.answer()

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    problem_text, answer = await generate_math_problem(user_id)
    await state.update_data(math_answer=answer)
    await state.set_state(MathForm.waiting_for_answer)

    sent = await callback.message.answer(
        f"🧮 Реши пример!\n\n<b>{problem_text}</b>\n\nНапиши ответ числом:",
        parse_mode="HTML",
        reply_markup=get_math_keyboard()
    )
    await state.update_data(problem_msg_id=sent.message_id)

@router.callback_query(F.data == "math_exit")
async def process_math_exit(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await state.clear()
    await callback.message.answer("🚪 Вы вышли из математики.", reply_markup=get_work_keyboard())

# ============================================================
# БИЗНЕС
# ============================================================

@router.message(F.text == "🏪 Мои бизнесы")
async def handle_my_businesses(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    biz = await get_biz(user_id)
    if biz:
        await settle_and_save_biz(user_id, biz)
        text, kb = biz_manage_view(biz)
    else:
        text, kb = biz_no_biz_view()

    await message.answer(text, reply_markup=kb)

@router.callback_query(F.data.startswith("biz_"))
async def handle_biz_callbacks(callback: CallbackQuery, state: FSMContext):
    data = callback.data
    user_id = callback.from_user.id

    if data == "biz_noop":
        await callback.answer()
        return

    if data == "biz_refresh":
        await callback.answer()
        biz = await get_biz(user_id)
        if not biz:
            text, kb = biz_no_biz_view()
            await biz_edit(callback, text, kb)
            return
        await settle_and_save_biz(user_id, biz)
        text, kb = biz_manage_view(biz)
        await biz_edit(callback, text, kb)
        return

    if data == "biz_exit":
        await callback.answer()
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await callback.message.answer(
            "Выберите направление в работе:",
            reply_markup=get_work_keyboard()
        )
        return

    if data == "biz_manage":
        await callback.answer()
        await state.clear()
        biz = await get_biz(user_id)
        if biz:
            await settle_and_save_biz(user_id, biz)
            text, kb = biz_manage_view(biz)
        else:
            text, kb = biz_no_biz_view()
        await biz_edit(callback, text, kb)
        return

    if data.startswith("biz_car:"):
        await callback.answer()
        existing = await get_biz(user_id)
        if existing:
            await settle_and_save_biz(user_id, existing)
            text, kb = biz_manage_view(existing)
            await biz_edit(callback, text, kb)
            return
        idx = int(data.split(":")[1])
        balance = await get_balance(user_id)
        text, kb = biz_carousel_view(idx, balance)
        await biz_edit(callback, text, kb)
        return

    if data.startswith("biz_buy:"):
        idx = int(data.split(":")[1])
        biz_def = BUSINESS_LIST[idx]
        existing = await get_biz(user_id)
        if existing:
            await callback.answer("У вас уже есть бизнес! Сначала продайте его.", show_alert=True)
            return
        balance = await get_balance(user_id)
        if balance < biz_def["price"]:
            await callback.answer("Недостаточно средств!", show_alert=True)
            return
        await callback.answer()
        await add_to_balance(user_id, -biz_def["price"])
        new_biz = {
            "name": biz_def["name"],
            "price": biz_def["price"],
            "income_per_min": biz_def["income_per_min"],
            "raw_consumption_per_min": biz_def["raw_consumption_per_min"],
            "raw_capacity": biz_def["raw_capacity"],
            "level": 1,
            "raw_stock": 0,
            "balance": 0,
            "last_collected": time.time(),
        }
        await save_biz(user_id, new_biz)
        text, kb = biz_manage_view(new_biz)
        await callback.message.edit_text(
            f"✅ Вы купили «{biz_def['name']}» за {biz_def['price']:,} ₽!\n\n" + text,
            reply_markup=kb
        )
        return

    biz = await get_biz(user_id)
    if not biz:
        await callback.answer()
        text, kb = biz_no_biz_view()
        await biz_edit(callback, text, kb)
        return

    await settle_and_save_biz(user_id, biz)

    if data == "biz_wh":
        await callback.answer()
        text, kb = biz_warehouse_view(biz)
        await biz_edit(callback, text, kb)
        return

    if data == "biz_wh:user" or data == "biz_wh:biz":
        await callback.answer()
        source = data.split(":")[1]
        await state.update_data(raw_source=source)
        await state.set_state(BusinessForm.waiting_for_raw)
        stock = biz.get("raw_stock", 0)
        capacity = biz.get("raw_capacity", 30000)
        space = capacity - stock
        source_text = "основного баланса" if source == "user" else "баланса бизнеса"
        await callback.message.answer(
            f"Введите количество сырья для покупки.\n"
            f"Цена: {RAW_PRICE} ₽ за единицу\n"
            f"Свободно на складе: {space:,}\n"
            f"Оплата с: {source_text}"
        )
        return

    if data == "biz_up":
        await callback.answer()
        text, kb = biz_upgrade_view(biz)
        await biz_edit(callback, text, kb)
        return

    if data.startswith("biz_up_do"):
        source = data.split(":")[1] if ":" in data else "user"
        cost = biz_upgrade_cost(biz)
        if cost is None:
            await callback.answer("Максимальный уровень!", show_alert=True)
            return
        if source == "user":
            balance = await get_balance(user_id)
            if balance < cost:
                await callback.answer(f"Не хватает {cost - balance:,} ₽", show_alert=True)
                return
            await callback.answer()
            await add_to_balance(user_id, -cost)
        else:
            biz_balance = biz.get("balance", 0)
            if biz_balance < cost:
                await callback.answer(f"На балансе бизнеса не хватает {cost - biz_balance:,} ₽", show_alert=True)
                return
            await callback.answer()
            biz["balance"] = biz_balance - cost
        biz["level"] = biz.get("level", 1) + 1
        new_level = biz["level"]
        if new_level in UPGRADE_INCOME_MULT:
            biz["income_per_min"] = int(biz["income_per_min"] * UPGRADE_INCOME_MULT[new_level])
            biz["raw_consumption_per_min"] = int(biz.get("raw_consumption_per_min", 0) * UPGRADE_CONSUMPTION_MULT[new_level])
            biz["raw_capacity"] = int(biz.get("raw_capacity", 30000) * UPGRADE_CAPACITY_MULT[new_level])
        await save_biz(user_id, biz)
        text, kb = biz_manage_view(biz)
        await biz_edit(callback, text, kb)
        return

    if data == "biz_sell":
        await callback.answer()
        text, kb = biz_sell_view(biz)
        await biz_edit(callback, text, kb)
        return

    if data == "biz_sell_confirm":
        await callback.answer()
        sell_price = biz_sell_price(biz)
        text = (
            f"⚠️ Вы точно хотите продать «{biz['name']}»?\n\n"
            f"Сумма выплаты: {sell_price:,} ₽\n\n"
            f"После продажи бизнес исчезнет, а деньги поступят на ваш баланс."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, продать", callback_data="biz_sell_do"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="biz_manage"),
            ],
        ])
        await biz_edit(callback, text, kb)
        return

    if data == "biz_sell_do":
        await callback.answer("Продажа завершена.")
        sell_price = biz_sell_price(biz)
        await add_to_balance(user_id, sell_price)
        await save_biz(user_id, None)
        text, kb = biz_no_biz_view()
        await callback.message.edit_text(
            f"✅ Бизнес продан! Вы получили {sell_price:,} ₽.\n\n" + text,
            reply_markup=kb
        )
        return

    if data == "biz_collect":
        biz_balance = biz.get("balance", 0)
        if biz.get("raw_stock", 0) <= 0 and biz_balance <= 0:
            await callback.answer("Бизнес не работает — нет сырья и дохода!", show_alert=True)
            return
        if biz_balance < 1:
            await callback.answer("Доход ещё не накопился.", show_alert=True)
            return
        await callback.answer()
        await add_to_balance(user_id, biz_balance)
        biz["balance"] = 0
        biz["last_collected"] = time.time()
        await save_biz(user_id, biz)
        text, kb = biz_manage_view(biz)
        await callback.message.edit_text(
            f"💰 Вы получили {biz_balance:,} ₽!\n\n" + text,
            reply_markup=kb
        )
        return

# --- БИЗНЕС: ввод количества сырья ---
@router.message(BusinessForm.waiting_for_raw)
async def process_raw_amount(message: Message, state: FSMContext):
    user_id = message.from_user.id

    try:
        amount = int(message.text.strip())
        if amount <= 0:
            await message.answer("Количество должно быть больше 0. Попробуйте ещё раз:")
            return
    except ValueError:
        await message.answer("Введите число:")
        return

    data = await state.get_data()
    source = data.get("raw_source")

    biz = await get_biz(user_id)
    if not biz:
        await state.clear()
        await message.answer("Бизнес не найден.", reply_markup=get_work_keyboard())
        return

    await settle_and_save_biz(user_id, biz)
    capacity = biz.get("raw_capacity", 30000)
    stock = biz.get("raw_stock", 0)
    space = capacity - stock

    if amount > space:
        await message.answer(f"Не хватает места на складе! Свободно: {space:,}\nВведите меньшее количество:")
        return

    cost = amount * RAW_PRICE

    if source == "user":
        balance = await get_balance(user_id)
        if balance < cost:
            await message.answer(f"Не хватает {cost - balance:,} ₽. Ваш баланс: {balance:,} ₽\nВведите меньшее количество:")
            return
        await add_to_balance(user_id, -cost)
    else:
        biz_balance = biz.get("balance", 0)
        if biz_balance < cost:
            await message.answer(f"На балансе бизнеса не хватает {cost - biz_balance:,} ₽. Баланс бизнеса: {biz_balance:,} ₽\nВведите меньшее количество:")
            return
        biz["balance"] = biz_balance - cost

    biz["raw_stock"] = stock + amount
    await save_biz(user_id, biz)
    await state.clear()

    await message.answer(
        f"✅ Закуплено {amount:,} единиц сырья за {cost:,} ₽\n"
        f"Сырьё: {biz['raw_stock']:,}/{capacity:,}"
    )

    text, kb = biz_warehouse_view(biz)
    await message.answer(text, reply_markup=kb)


# ============================================================
# КАЗИНО — ЕВРОПЕЙСКАЯ РУЛЕТКА
# ============================================================

EUROPEAN_RED_NUMBERS = {
    1, 3, 5, 7, 9, 12, 14, 16, 18,
    19, 21, 23, 25, 27, 30, 32, 34, 36
}

def roulette_color(number: int) -> str:
    if number == 0:
        return "🟢"
    return "🔴" if number in EUROPEAN_RED_NUMBERS else "⚫"


def roulette_bet_name(bet: str) -> str:
    names = {
        "0": "🟢 зеро",
        "red": "🔴 красное",
        "black": "⚫ чёрное",
        "odd": "нечёт",
        "even": "чёт",
        "low": "1–18",
        "high": "19–36",
        "dozen1": "1-12",
        "dozen2": "13-24",
        "dozen3": "25-36",
    }
    if bet in names:
        return names[bet]
    return f"{roulette_color(int(bet))} {bet}"


def roulette_bet_result(bet: str, number: int) -> tuple[bool, int]:
    """Возвращает (победа, коэффициент выплаты).
    Коэффициент — чистый выигрыш к размеру ставки.
    """
    if bet == "0":
        return number == 0, 35

    if bet == "red":
        return number in EUROPEAN_RED_NUMBERS, 1
    if bet == "black":
        return number != 0 and number not in EUROPEAN_RED_NUMBERS, 1
    if bet == "odd":
        return number != 0 and number % 2 == 1, 1
    if bet == "even":
        return number != 0 and number % 2 == 0, 1
    if bet == "low":
        return 1 <= number <= 18, 1
    if bet == "high":
        return 19 <= number <= 36, 1
    if bet == "dozen1":
        return 1 <= number <= 12, 2
    if bet == "dozen2":
        return 13 <= number <= 24, 2
    if bet == "dozen3":
        return 25 <= number <= 36, 2

    return False, 0


async def roulette_show_amount(message: Message, state: FSMContext):
    user_id = message.from_user.id
    balance = await get_balance(user_id)
    await state.clear()
    await state.set_state(RouletteForm.waiting_for_amount)
    sent = await message.answer(
        f"🎡 Европейская рулетка\n\n"
        f"💰 Ваш баланс: {balance:,} ₽\n\n"
        f"Введите сумму ставки:",
        reply_markup=get_roulette_amount_keyboard(0)
    )
    await state.update_data(amount_msg_id=sent.message_id, amount=0)


@router.message(F.text == "🎰 Казино")
async def show_casino(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🎰 Казино\n\nВыберите игру:",
        reply_markup=get_casino_keyboard()
    )


@router.callback_query(F.data == "casino_menu")
async def casino_menu(callback: CallbackQuery, state: FSMContext):
    await callback.answer()  # <-- обязательно: снимает «часы» у пользователя
    await state.clear()

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        # Сообщение могло быть удалено/изменено — просто игнорируем
        pass

    await callback.message.answer(
        "🎰 Казино\n\nВыберите игру:",
        reply_markup=get_casino_keyboard()
    )

@router.message(F.text == "🎡 Рулетка")
async def casino_roulette(message: Message, state: FSMContext):
    await state.clear()

    # Убираем Reply-клавиатуру, но само сообщение выбора ставки
    # создаётся отдельно и остаётся на месте вместе с inline-кнопками.
    await message.answer(
        "🎡 Рулетка открыта.",
        reply_markup=ReplyKeyboardRemove()
    )
    await roulette_show_amount(message, state)

@router.callback_query(F.data == "roulette_amount_noop")
async def roulette_amount_noop(callback: CallbackQuery):
    await callback.answer("Введите сумму сообщением.")


@router.message(RouletteForm.waiting_for_amount)
async def process_roulette_amount(message: Message, state: FSMContext):
    user_id = message.from_user.id

    try:
        amount = int(message.text.strip())
    except (TypeError, ValueError):
        await message.answer("❌ Введите целое число, например: 100")
        return

    if amount <= 0:
        await message.answer("❌ Ставка должна быть больше 0.")
        return

    balance = await get_balance(user_id)
    if amount > balance:
        await message.answer(
            f"❌ Недостаточно средств.\n"
            f"Ваш баланс: {balance:,} ₽\n"
            f"Введите меньшую сумму:"
        )
        return

    data = await state.get_data()
    amount_msg_id = data.get("amount_msg_id")
    if amount_msg_id:
        try:
            await message.bot.edit_message_reply_markup(
                chat_id=message.chat.id,
                message_id=amount_msg_id,
                reply_markup=None
            )
        except TelegramBadRequest:
            pass

    await state.update_data(amount=amount)
    await state.set_state(RouletteForm.waiting_for_bet)

    await message.answer(
        f"🎡 Европейская рулетка\n\n"
        f"💰 Ставка: {amount:,} ₽\n"
        f"🎯 Выберите, на что поставить:",
        reply_markup=get_roulette_bet_keyboard(amount)
    )


@router.callback_query(RouletteForm.waiting_for_amount, F.data.startswith("roulette_mul:"))
async def roulette_change_amount(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    current = int(data.get("amount", 0))
    multiplier = callback.data.split(":")[1]

    # Если сумма ещё не введена, 0.5/2 ничего не меняет.
    if current <= 0:
        await callback.answer("Сначала введите сумму ставки.", show_alert=True)
        return

    new_amount = int(current * float(multiplier))
    balance = await get_balance(callback.from_user.id)

    if new_amount <= 0:
        await callback.answer("Минимальная ставка — 1 ₽.", show_alert=True)
        return
    if new_amount > balance:
        await callback.answer(
            f"Недостаточно средств. Баланс: {balance:,} ₽",
            show_alert=True
        )
        return

    await state.update_data(amount=new_amount)
    await callback.answer(f"Ставка: {new_amount:,} ₽")

    try:
        await callback.message.edit_reply_markup(
            reply_markup=get_roulette_amount_keyboard(new_amount)
        )
    except TelegramBadRequest:
        pass


@router.callback_query(RouletteForm.waiting_for_bet, F.data.startswith("roulette_mul:"))
async def roulette_change_bet_amount(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    current = int(data.get("amount", 0))
    multiplier = float(callback.data.split(":")[1])
    new_amount = int(current * multiplier)
    balance = await get_balance(callback.from_user.id)

    if new_amount < 1:
        await callback.answer("Минимальная ставка — 1 ₽.", show_alert=True)
        return
    if new_amount > balance:
        await callback.answer(f"Недостаточно средств. Баланс: {balance:,} ₽", show_alert=True)
        return

    await state.update_data(amount=new_amount)
    await callback.answer(f"Ставка изменена: {new_amount:,} ₽")

    try:
        await callback.message.edit_reply_markup(
            reply_markup=get_roulette_bet_keyboard(new_amount)
        )
    except TelegramBadRequest:
        pass


@router.callback_query(RouletteForm.waiting_for_bet, F.data == "roulette_amount_noop")
async def roulette_amount_noop_bet(callback: CallbackQuery):
    await callback.answer("Используйте кнопки 0.5/2 или выберите ставку.")


@router.callback_query(RouletteForm.waiting_for_bet, F.data.startswith("roulette_bet:"))
async def process_roulette_bet(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    state_data = await state.get_data()
    amount = int(state_data.get("amount", 0))
    bet = callback.data.split(":", 1)[1]

    if amount <= 0:
        await callback.answer("Сначала укажите сумму ставки.", show_alert=True)
        return

    balance = await get_balance(user_id)
    if amount > balance:
        await callback.answer("Недостаточно средств для этой ставки.", show_alert=True)
        return

    await callback.answer()
    await add_to_balance(user_id, -amount)

    # Для первого вращения создаём сообщение.
    # Для всех следующих игр используем ПОСЛЕДНЕЕ сообщение результата
    # и превращаем его обратно в прокрутку.
    last_result_message_id = state_data.get("last_result_message_id")
    chat_id = callback.message.chat.id

    spin_text = (
        f"🎡 <b>РУЛЕТКА КРУТИТСЯ...</b>\n\n"
        f"🎯 Ставка: <b>{roulette_bet_name(bet)}</b>\n"
        f"💰 Сумма: <b>{amount:,} ₽</b>"
    )

    spin_message = None

    if last_result_message_id:
        try:
            spin_message = await callback.bot.edit_message_text(
                chat_id=chat_id,
                message_id=last_result_message_id,
                text=spin_text,
                parse_mode="HTML",
                reply_markup=None
            )
        except TelegramBadRequest:
            spin_message = None

    if spin_message is None:
        spin_message = await callback.message.answer(
            spin_text,
            parse_mode="HTML"
        )

    # Запоминаем сообщение, которое сейчас используется для прокрутки.
    await state.update_data(
        last_result_message_id=spin_message.message_id,
        bet=bet
    )

    spin_frames = [
        "🎡 🔄 ⚫ 17",
        "🎡 🔄 🔴 32",
        "🎡 🔄 🔴 9",
        "🎡 🔄 🟢 0",
        "🎡 🔄 ⚫ 26",
        "🎡 🔄 🔴 14",
        "🎡 🔄 ⚫ 4",
        "🎡 🔄 🔴 21",
        "🎡 🔄 ⚫ 35",
        "🎡 🔄 🔴 18",
        "🎡 🔄 ⚫ 6",
        "🎡 🔄 🔴 1",
        "🎡 🔄 ⚫ 11",
    ]

    # Перемешиваем кадры каждый раз — рандомный порядок
    random.shuffle(spin_frames)

    # Эффект замедления: первые кадры быстро, потом медленнее
    delays = [0.8, 0.9, 0.10, 0.11, 0.11, 0.11, 0.11, 0.15, 0.16, 0.17, 0.20, 0.22, 0.23]

    for i, frame in enumerate(spin_frames):
        try:
            await callback.bot.edit_message_text(
                chat_id=chat_id,
                message_id=spin_message.message_id,
                text=(
                    f"<b>РУЛЕТКА КРУТИТСЯ...</b>\n\n"
                    f"{frame}\n\n"
                    f"🎯 Ставка: <b>{roulette_bet_name(bet)}</b>\n"
                    f"💰 Сумма: <b>{amount:,} ₽</b>"
                ),
                parse_mode="HTML",
                reply_markup=None
            )
            delay = delays[i] if i < len(delays) else 0.25
            await asyncio.sleep(delay)
        except TelegramBadRequest:
            break

    number = random.randint(0, 36)
    color = roulette_color(number)
    won, payout_mult = roulette_bet_result(bet, number)

    if won:
        winnings = amount * (payout_mult + 1)
        await add_to_balance(user_id, winnings)
        new_balance = await get_balance(user_id)

        result_text = (
            f"🎡 <b>РУЛЕТКА ОСТАНОВИЛАСЬ!</b>\n\n"
            f"Выпало: {color} <b>{number}</b>\n"
            f"Ваша ставка: <b>{roulette_bet_name(bet)}</b>\n"
            f"Сумма: <b>{amount:,} ₽</b>\n\n"
            f"✅ <b>ПРАВИЛЬНО!</b>\n"
            f"🎉 Выигрыш: +{amount * payout_mult:,} ₽\n"
            f"💰 Баланс: <b>{new_balance:,} ₽</b>"
        )
    else:
        new_balance = await get_balance(user_id)

        result_text = (
            f"🎡 <b>РУЛЕТКА ОСТАНОВИЛАСЬ!</b>\n\n"
            f"Выпало: {color} <b>{number}</b>\n"
            f"Ваша ставка: <b>{roulette_bet_name(bet)}</b>\n"
            f"Сумма: <b>{amount:,} ₽</b>\n\n"
            f"❌ <b>НЕПРАВИЛЬНО!</b>\n"
            f"💸 Ставка сгорела.\n"
            f"💰 Баланс: <b>{new_balance:,} ₽</b>"
        )

    # После остановки оставляем только результат без inline-кнопок.
    await callback.bot.edit_message_text(
        chat_id=chat_id,
        message_id=spin_message.message_id,
        text=result_text,
        parse_mode="HTML",
        reply_markup=None
    )

    # last_result_message_id остаётся в state.
    # Поэтому следующий клик по ставке изменит ЭТО ЖЕ сообщение
    # обратно на прокрутку, а не создаст новое.

@router.callback_query(F.data.startswith("roulette_mul:"))
async def roulette_change_amount_outside_state(callback: CallbackQuery):
    await callback.answer("Сначала откройте рулетку и введите ставку.", show_alert=True)


# --- Универсальный хендлер ---
@router.message(F.text)
async def handle_unknown_text(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Используй кнопки 😡")

dp.include_router(router)

# ============================================================
# ТОЧКА ВХОДА
# ============================================================

LOCK_FILE = "bot.lock"

async def main():
    if os.path.exists(LOCK_FILE):
        logger.warning("Lock-файл существует. Возможно, бот уже запущен.")
        with open(LOCK_FILE, "r") as f:
            old_pid = f.read().strip()
        logger.warning(f"PID предыдущего процесса: {old_pid}")

    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))

    logger.info("Запуск бота...")
    try:
        await init_redis()
    except Exception as e:
        logger.error(f"Redis: ошибка подключения — {e}")
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
        return

    try:
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
        logger.info("Бот остановлен.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Остановка бота по сигналу пользователя.")
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)