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

class AdminForm(StatesGroup):
    waiting_for_search = State()
    waiting_for_amount = State()

# ============================================================
# КОНСТАНТЫ
# ============================================================
HELP_TEXT_MAIN = (
    "бот коммерсант - тут можно зарабатывать деньги, торговать, делать бизнес(и многое другое)\n\n"
    "жми кнопки ниже, расскажу про каждый раздел."
)

HELP_TEXT_TRADING = (
    "Трейдинг — это торговля на рынке криптовалюты с разной степенью риска.\n\n"
    "Как это работает:\n"
    "1. Выбираешь риск: низкий (высокий шанс победы, но выйгрыш небольшой), средний (шанс 50на50, выйгрыш х2), высокий (маленький шанс, но выйгрыш х5 от ставки!!).\n"
    "2. Вводи сумму ставки\n"
    "3. Бот проверяет рынок и показывает результат.\n\n"
)

HELP_TEXT_MINE = (
    "Шахта — самый простой способ заработать первые деньги.\n"
    "Нажал = получил деньги."
)

HELP_TEXT_MATH = (
    "Математика — решил пример = получил деньги.\n\n"
)

HELP_TEXT_BUSINESS = (
    "Бизнесы — это пассивный доход: ты покупаешь бизнес, и он приносит деньги каждую минуту.\n\n"
    "Бизнесу нужно сырьё. Если оно заканчивается, бизнес перестаёт работать и доход останавливается.\n\n"
)

# ============================================================
# ЭКОНОМИКА: КОНСТАНТЫ
# ============================================================
MINE_REWARD = 200
MINE_COOLDOWN = 5
MATH_REWARD = 400
MATH_COOLDOWN = 10
RAW_PRICE = 1

TRADING_MIN_BALANCE = 25000

TRADING_MODES = {
    "low": {"multiplier": 1.3, "chance": 0.7},
    "mid": {"multiplier": 2.0, "chance": 0.5},
    "high": {"multiplier": 5.0, "chance": 0.2},
}

# ============================================================
# БИЗНЕСЫ: КОНСТАНТЫ
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
        f"🏪 Твой бизнес: «{biz['name']}»\n\n"
        f"🚀 Уровень: {biz.get('level', 1)}/3\n"
        f"💰 Капает: {biz.get('income_per_min', 0):,} ₽/мин\n"
        f"📦 Сырьё уходит: {consumption:,}/мин\n"
        f"💸 Чистыми: {net_profit:,} ₽/мин — в плюсах\n"
        f"📦 Склад: {biz.get('raw_stock', 0):,}/{biz.get('raw_capacity', 30000):,}\n"
        f"⏳ Хватит на: {format_time(time_left)}\n"
        f"💳 На счету бизнеса: {biz_balance:,} ₽\n\n"
    )
    if biz.get("raw_stock", 0) <= 0:
        text += "⚠️ Бизнес встал — сырья ноль!\nЖми «📦 Склад», затарься."
    else:
        text += "✅ Бизнес работает, копит кэш!"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📦 Склад", callback_data="biz_wh"),
            InlineKeyboardButton(text="🚀 Прокачать", callback_data="biz_up"),
        ],
        [InlineKeyboardButton(text="💰 Забрать кэш", callback_data="biz_collect")],
        [InlineKeyboardButton(text="💸 Продать", callback_data="biz_sell")],
        [
            InlineKeyboardButton(text="🔄 Обновить", callback_data="biz_refresh"),
            InlineKeyboardButton(text="🔙 Выйти", callback_data="biz_exit"),
        ],
    ])
    return text, kb


def biz_no_biz_view():
    text = "🏪 Мои бизнесы\n\nПока пусто — бизнеса нет.\nЖми кнопку ниже, выбери себе точку."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Взять бизнес", callback_data="biz_car:0")]
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
        f"🏪 Берём бизнес\n\n"
        f"🏗 {biz['name']}\n"
        f"💸 Цена: {biz['price']:,} ₽\n"
        f"💰 Капает: {biz['income_per_min']:,} ₽/мин\n"
        f"📦 Сырьё уходит: {consumption:,}/мин\n"
        f"💸 Чистыми: {net:,} ₽/мин\n"
        f"📦 Склад: {biz['raw_capacity']:,}\n"
        f"💸 Затарить склад: {full_stock_cost:,} ₽\n"
        f"⏳ Полного склада хватит на: {format_time(run_time)}\n"
        f"📊 Окупится за: {format_time(payback_min)}\n\n"
        f"Твой баланс: {balance:,} ₽"
    )
    nav = []
    if idx > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"biz_car:{idx-1}"))
    nav.append(InlineKeyboardButton(text=f"{idx+1}/{len(BUSINESS_LIST)}", callback_data="biz_noop"))
    if idx < len(BUSINESS_LIST) - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"biz_car:{idx+1}"))
    rows = [nav]
    if can_buy:
        rows.append([InlineKeyboardButton(text=f"✅ Взять за {biz['price']:,} ₽", callback_data=f"biz_buy:{idx}")])
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
        f"Цена: {RAW_PRICE} ₽ за штуку\n"
        f"📦 Уходит: {biz.get('raw_consumption_per_min', 0):,}/мин\n"
        f"⏳ Хватит на: {format_time(time_left)}\n"
        f"💳 На счету бизнеса: {biz_balance:,} ₽\n\n"
        f"Откуда скидываем?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 С основного баланса", callback_data="biz_wh:user")],
        [InlineKeyboardButton(text="🏪 Со счёта бизнеса", callback_data="biz_wh:biz")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="biz_manage")],
    ])
    return text, kb


def biz_upgrade_view(biz):
    level = biz.get("level", 1)
    cost = biz_upgrade_cost(biz)
    if cost is None:
        text = f"🚀 «{biz['name']}»\n\nУровень: {level}/3 — потолок, выше не прыгнешь!"
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
        f"🚀 Прокачка «{biz['name']}»\n\n"
        f"Сейчас уровень: {level}/3\n"
        f"💰 Капает: {biz['income_per_min']:,} ₽/мин\n"
        f"📦 Сырьё уходит: {biz.get('raw_consumption_per_min', 0):,}/мин\n"
        f"💸 Чистыми: {current_net:,} ₽/мин\n"
        f"📦 Склад: {biz.get('raw_capacity', 30000):,}\n\n"
        f"⬆️ После прокачки (уровень {new_level}):\n"
        f"💰 Капает: {new_income:,} ₽/мин\n"
        f"📦 Сырьё уходит: {new_consumption:,}/мин\n"
        f"💸 Чистыми: {new_net:,} ₽/мин\n"
        f"📦 Склад: {new_capacity:,}\n"
        f"💸 Стоит: {cost:,} ₽"
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
        f"💸 Продаём «{biz['name']}»\n\n"
        f"На руки получишь: {sell_price:,} ₽\n"
        f"(50% цены + 50% сырья + баланс бизнеса)\n\n"
        f"Жми «Подтвердить», если реально готов продать."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтверждаю", callback_data="biz_sell_confirm")],
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

# --- Хранение истории последних 10 сделок ---
async def log_trade(user_id: int, mode: str, amount: int, result: float, win: bool):
    trade = {
        "mode": mode,
        "amount": amount,
        "result": result,
        "win": win,
        "ts": time.time(),
    }
    key = f"user:{user_id}:trades"
    trades = await redis_client.lrange(key, 0, -1)
    trades = [json.loads(t) for t in trades] if trades else []
    trades.append(trade)
    trades = trades[-10:]
    await redis_client.delete(key)
    for t in trades:
        await redis_client.rpush(key, json.dumps(t))

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
    text = f"вечер в хату, {display_name}. твой баланс: {balance:,} ₽"
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


# ============================================================
# АДМИН-ПАНЕЛЬ
# ============================================================

def get_admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Найти игрока", callback_data="admin_find")],
        [InlineKeyboardButton(text="📊 Топ по балансу", callback_data="admin_top")],
    ])

def get_admin_player_keyboard(player_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Задать баланс", callback_data=f"admin_act:set:{player_id}")],
        [InlineKeyboardButton(text="➕ Добавить", callback_data=f"admin_act:add:{player_id}")],
        [InlineKeyboardButton(text="➖ Вычесть", callback_data=f"admin_act:sub:{player_id}")],
        [InlineKeyboardButton(text="🔙 К меню", callback_data="admin_main")],
    ])

def get_admin_confirm_keyboard(action: str, player_id: int, amount: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"admin_do:{action}:{player_id}:{amount}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin_back:{player_id}")],
    ])

def get_admin_back_keyboard(player_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 К игроку", callback_data=f"admin_back:{player_id}")],
        [InlineKeyboardButton(text="🏠 В меню админа", callback_data="admin_main")],
    ])


async def _edit_or_answer(bot_obj, chat_id: int, msg_id: int | None, text: str, kb=None):
    """Пытается отредактировать сообщение; если не получается — отправляет новое."""
    if msg_id:
        try:
            await bot_obj.edit_message_text(
                text=text,
                chat_id=chat_id,
                message_id=msg_id,
                reply_markup=kb,
            )
            return
        except TelegramBadRequest:
            pass
    await bot_obj.send_message(chat_id, text, reply_markup=kb)


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Доступ закрыт, ты не админ.")
        return
    await state.clear()
    sent = await message.answer(
        "🛡 Админ-панель\n\nВыбирай действие:",
        reply_markup=get_admin_keyboard()
    )
    await state.update_data(admin_msg_id=sent.message_id)


@router.callback_query(F.data == "admin_main")
async def admin_main(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ закрыт, ты не админ.", show_alert=True)
        return
    await state.clear()
    await callback.answer()
    await _edit_or_answer(
        callback.bot, callback.message.chat.id, callback.message.message_id,
        "🛡 Админ-панель\n\nВыбирай действие:",
        get_admin_keyboard(),
    )
    await state.update_data(admin_msg_id=callback.message.message_id)


@router.callback_query(F.data == "admin_find")
async def admin_find(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ закрыт, ты не админ.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminForm.waiting_for_search)
    await _edit_or_answer(
        callback.bot, callback.message.chat.id, callback.message.message_id,
        "🔍 Введи ник игрока или @username:\nНапример: Alex123 или @someuser",
    )
    await state.update_data(admin_msg_id=callback.message.message_id)


@router.message(AdminForm.waiting_for_search)
async def admin_search(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    query = message.text.strip()
    chat_id = message.chat.id

    try:
        await message.delete()
    except TelegramBadRequest:
        pass

    data = await state.get_data()
    msg_id = data.get("admin_msg_id")

    player_id = await get_user_id_by_name_direct(query)
    if not player_id:
        player_id = await get_user_id_by_username(query)

    if not player_id:
        await _edit_or_answer(
            message.bot, chat_id, msg_id,
            f"❌ Игрок «{query}» не найден.\n\nПопробуй ещё раз — введи ник или @username:",
        )
        return

    await _show_admin_player(message.bot, chat_id, msg_id, state, player_id)


async def _show_admin_player(bot_obj, chat_id: int, msg_id: int | None,
                             state: FSMContext, player_id: int):
    balance = await get_balance(player_id)
    name = await get_user_name(player_id) or "без ника"
    username = await get_username(player_id)

    text = (
        f"👤 Игрок #{player_id}\n\n"
        f"📝 Ник: {name}\n"
        f"👤 Username: @{username}\n"
        f"💰 Баланс: {balance:,} ₽"
    )
    kb = get_admin_player_keyboard(player_id)

    await state.update_data(admin_player_id=player_id)
    await _edit_or_answer(bot_obj, chat_id, msg_id, text, kb)
    await state.update_data(admin_msg_id=msg_id)


@router.callback_query(F.data.startswith("admin_act:"))
async def admin_action_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ закрыт, ты не админ.", show_alert=True)
        return

    parts = callback.data.split(":")
    action = parts[1]
    player_id = int(parts[2])

    await callback.answer()

    action_names = {
        "set": "задать новый баланс",
        "add": "добавить к балансу",
        "sub": "вычесть из баланса",
    }

    current_balance = await get_balance(player_id)

    await state.update_data(
        admin_action=action,
        admin_player_id=player_id,
        admin_msg_id=callback.message.message_id,
    )
    await state.set_state(AdminForm.waiting_for_amount)

    await _edit_or_answer(
        callback.bot, callback.message.chat.id, callback.message.message_id,
        f"💰 Сейчас на балансе: {current_balance:,} ₽\n\n"
        f"Введи сумму для «{action_names[action]}»:",
    )


@router.message(AdminForm.waiting_for_amount)
async def admin_enter_amount(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    chat_id = message.chat.id

    try:
        amount = int(message.text.strip())
        if amount < 0:
            try:
                await message.delete()
            except TelegramBadRequest:
                pass
            await _edit_or_answer(
                message.bot, chat_id, (await state.get_data()).get("admin_msg_id"),
                "❌ Минус нельзя. Введи нормальное число:",
            )
            return
    except ValueError:
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        await _edit_or_answer(
            message.bot, chat_id, (await state.get_data()).get("admin_msg_id"),
            "❌ Введи целое число:",
        )
        return

    try:
        await message.delete()
    except TelegramBadRequest:
        pass

    data = await state.get_data()
    action = data.get("admin_action")
    player_id = data.get("admin_player_id")
    msg_id = data.get("admin_msg_id")

    if not action or not player_id:
        await _edit_or_answer(
            message.bot, chat_id, msg_id,
            "❌ Сессия истекла. Начни заново через /admin",
        )
        await state.clear()
        return

    current_balance = await get_balance(player_id)
    name = await get_user_name(player_id) or "без ника"

    action_texts = {
        "set": f"Задать баланс = {amount:,} ₽",
        "add": f"Добавить {amount:,} ₽ (станет {current_balance + amount:,} ₽)",
        "sub": f"Вычесть {amount:,} ₽ (станет {current_balance - amount:,} ₽)",
    }

    text = (
        f"⚠️ Подтверди действие:\n\n"
        f"👤 Игрок: {name} (#{player_id})\n"
        f"💰 Сейчас на балансе: {current_balance:,} ₽\n"
        f"📋 {action_texts[action]}"
    )

    kb = get_admin_confirm_keyboard(action, player_id, amount)
    await _edit_or_answer(message.bot, chat_id, msg_id, text, kb)
    await state.set_state(None)


@router.callback_query(F.data.startswith("admin_do:"))
async def admin_do_action(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ закрыт, ты не админ.", show_alert=True)
        return

    parts = callback.data.split(":")
    action = parts[1]
    player_id = int(parts[2])
    amount = int(parts[3])

    await callback.answer()

    if action == "set":
        await set_balance(player_id, amount)
    elif action == "add":
        await add_to_balance(player_id, amount)
    elif action == "sub":
        await add_to_balance(player_id, -amount)

    new_balance = await get_balance(player_id)
    name = await get_user_name(player_id) or "без ника"

    await _edit_or_answer(
        callback.bot, callback.message.chat.id, callback.message.message_id,
        f"✅ Готово!\n\n"
        f"👤 Игрок: {name} (#{player_id})\n"
        f"💰 Новый баланс: {new_balance:,} ₽",
        get_admin_back_keyboard(player_id),
    )


@router.callback_query(F.data.startswith("admin_back:"))
async def admin_back_to_player(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ закрыт, ты не админ.", show_alert=True)
        return

    player_id = int(callback.data.split(":")[1])
    await callback.answer()
    await _show_admin_player(
        callback.bot, callback.message.chat.id,
        callback.message.message_id, state, player_id,
    )


@router.callback_query(F.data == "admin_top")
async def admin_top(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ закрыт, ты не админ.", show_alert=True)
        return

    await callback.answer()
    balances = await get_all_balances()
    if not balances:
        await _edit_or_answer(
            callback.bot, callback.message.chat.id, callback.message.message_id,
            "Пока пусто — нет данных.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 В меню админа", callback_data="admin_main")]
            ]),
        )
        return

    text = "📊 Топ игроков (админ-режим):\n\n"
    for i, (uid, name, balance) in enumerate(balances[:20], 1):
        text += f"{i}. {name} (#{uid}) — {balance:,} ₽\n"

    text += f"\nВсего игроков: {len(balances)}"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 В меню админа", callback_data="admin_main")]
    ])

    await _edit_or_answer(
        callback.bot, callback.message.chat.id, callback.message.message_id,
        text, kb,
    )
    await state.update_data(admin_msg_id=callback.message.message_id)

# --- Клавиатуры ---
def get_main_keyboard():
    keyboard = [
        [KeyboardButton(text="💼 Работа"), KeyboardButton(text="🛒 Магаз")],
        [KeyboardButton(text="🎰 Казино"), KeyboardButton(text="🏆 Топ")]
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

def get_help_menu_keyboard():
    keyboard = [
        [
            InlineKeyboardButton(text="📈 Трейдинг", callback_data="help_trading"),
            InlineKeyboardButton(text="⛏ Шахта", callback_data="help_mine"),
        ],
        [
            InlineKeyboardButton(text="🧮 Математика", callback_data="help_math"),
            InlineKeyboardButton(text="🏪 Бизнесы", callback_data="help_business"),
        ],
        [InlineKeyboardButton(text="🔙 В главное меню", callback_data="main_menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

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
        [KeyboardButton(text="🏪 Бизнесы")],
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

def get_trading_mode_keyboard():
    rows = [
        [
            InlineKeyboardButton(text="🟢 Низкий риск (x1.2, 80%)", callback_data="trade_mode:low"),
            InlineKeyboardButton(text="🟡 Средний риск (x2.0, 50%)", callback_data="trade_mode:mid"),
        ],
        [InlineKeyboardButton(text="🔴 Высокий риск (x5.0, 20%)", callback_data="trade_mode:high")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def get_trading_result_keyboard():
    keyboard = [
        [
            InlineKeyboardButton(text="🎮 Ещё раз", callback_data="trade_continue"),
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
            InlineKeyboardButton(text="➡ Следующий", callback_data="math_next"),
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
            "Введи ник — буквы (русские или английские) и цифры, от 3 до 10 символов.\n"
            "Например: Alex123, Иван4, Макс777\n\n"
            "⚠️ Ник должен быть уникальным — если занят, придётся придумать другой."
        )
        await state.set_state(NameForm.waiting_for_name)
        return
    await send_main_menu(message, user_id)

@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        HELP_TEXT_MAIN,
        reply_markup=get_help_menu_keyboard()
    )

@router.message(Command("ping"))
async def cmd_ping(message: Message):
    try:
        await redis_client.ping()
        redis_ok = "✅ Redis ок"
    except Exception as e:
        redis_ok = f"❌ Redis лёг: {e}"
        logger.error(f"Redis healthcheck failed: {e}")
    uptime = int(time.time() - start_time)
    days, rem = divmod(uptime, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    uptime_str = f"{days} дн {hours} ч {mins} мин"
    await message.answer(
        f"🤖 Бот жив\n{redis_ok}\n⏳ В строю уже: {uptime_str}"
    )

@router.message(Command("trades"))
async def cmd_trades(message: Message):
    user_id = message.from_user.id
    key = f"user:{user_id}:trades"
    trades = await redis_client.lrange(key, 0, -1)
    if not trades:
        await message.answer("Сделок пока ноль — ты ещё не заходил в трейдинг.")
        return
    trades = [json.loads(t) for t in trades]
    text = "📜 Твои сделки:\n\n"
    wins = 0
    total_profit = 0
    for i, t in enumerate(trades[-5:], 1):
        sign = "✅" if t["win"] else "❌"
        text += f"{i}. {sign} {t['mode']} | Ставка: {t['amount']:,} ₽ | Результат: {t['result']:+,.0f} ₽\n"
        if t["win"]:
            wins += 1
        total_profit += t["result"]
    text += f"\nВсего: {len(trades)} | Побед: {wins} | Общий результат: {total_profit:+,.0f} ₽"
    await message.answer(text)

@router.message(NameForm.waiting_for_name)
async def process_name(message: Message, state: FSMContext):
    name = message.text.strip()
    user_id = message.from_user.id
    if not is_admin(user_id):
        if not is_valid_name(name):
            await message.answer(
                "❌ Ник — только буквы и цифры, 3–10 символов. Без пробелов и всякой дичи.\n"
                "Попробуй ещё раз:"
            )
            return
        if await is_name_taken(name):
            await message.answer(f"❌ Ник «{name}» уже занят. Придумай другой:")
            return
    else:
        if not is_valid_name(name):
            await message.answer("⚠️ Админ: ник 3–10 символов, буквы и цифры. Исправь:")
            return
        existing_id = await get_user_id_by_name_direct(name)
        if existing_id and existing_id != user_id:
            await message.answer(f"⚠️ Ник «{name}» уже у игрока {existing_id}. Перезапишу.")
    await save_user_name(user_id, name)
    await state.clear()
    await message.answer(f"👍 База, {name}! Ты в игре.")
    await send_main_menu(message, user_id)

@router.message(F.text == "💼 Работа")
async def show_work_menu(message: Message):
    await message.answer("Выбирай, чем займешься:", reply_markup=get_work_keyboard())

@router.message(F.text == "🛒 Магаз")
async def show_shop_menu(message: Message):
    await message.answer("Раздел «Магаз» пока в разработке — скоро зальём.")

@router.message(F.text == "🏆 Топ")
async def show_top(message: Message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        cooldown_key = f"cooldown:top:{user_id}"
        ok = await redis_client.set(cooldown_key, "1", nx=True, ex=60)
        if not ok:
            ttl = await redis_client.ttl(cooldown_key)
            if ttl > 0:
                await message.answer(f"⏳ Топ можно глянуть через {ttl} сек.")
            else:
                await redis_client.set(cooldown_key, "1", nx=True, ex=60)
                await message.answer("⏳ Топ раз в минуту. Подожди чуток.")
            return

    balances = await get_all_balances()
    if not balances:
        await message.answer("🏆 Топ игроков\n\nПока пусто — никто не играл.")
        return

    text = "🏆 Топ по балансу:\n\n"
    for i, (uid, name, balance) in enumerate(balances[:10], 1):
        text += f"{i}. {name} — {balance:,} ₽\n"

    if len(balances) > 10:
        text += f"\n...и ещё {len(balances) - 10} челиков"

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
        f"За клик: {MINE_REWARD:,} ₽\n"
        f"КД: {MINE_COOLDOWN} сек\n"
        f"Жми «Фармить» — и кэш твой!",
        reply_markup=get_mine_keyboard()
    )

@router.callback_query(F.data == "help_trading")
async def handle_help_trading(callback: CallbackQuery):
    try:
        await callback.message.edit_text(
            HELP_TEXT_TRADING,
            reply_markup=get_help_menu_keyboard()
        )
    except TelegramBadRequest:
        await callback.message.answer(
            HELP_TEXT_TRADING,
            reply_markup=get_help_menu_keyboard()
        )

@router.callback_query(F.data == "help_mine")
async def handle_help_mine(callback: CallbackQuery):
    try:
        await callback.message.edit_text(
            HELP_TEXT_MINE,
            reply_markup=get_help_menu_keyboard()
        )
    except TelegramBadRequest:
        await callback.message.answer(
            HELP_TEXT_MINE,
            reply_markup=get_help_menu_keyboard()
        )

@router.callback_query(F.data == "help_math")
async def handle_help_math(callback: CallbackQuery):
    try:
        await callback.message.edit_text(
            HELP_TEXT_MATH,
            reply_markup=get_help_menu_keyboard()
        )
    except TelegramBadRequest:
        await callback.message.answer(
            HELP_TEXT_MATH,
            reply_markup=get_help_menu_keyboard()
        )

@router.callback_query(F.data == "help_business")
async def handle_help_business(callback: CallbackQuery):
    try:
        await callback.message.edit_text(
            HELP_TEXT_BUSINESS,
            reply_markup=get_help_menu_keyboard()
        )
    except TelegramBadRequest:
        await callback.message.answer(
            HELP_TEXT_BUSINESS,
            reply_markup=get_help_menu_keyboard()
        )

@router.callback_query(F.data == "main_menu")
async def handle_main_menu_from_help(callback: CallbackQuery):
    user_id = callback.from_user.id
    await send_main_menu(callback, user_id)

@router.callback_query(F.data == "mine_farm")
async def handle_mine_farm(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id

    allowed, remaining = await can_farm(user_id, cooldown_seconds=MINE_COOLDOWN)
    if not allowed:
        await callback.answer(f"⏳ Осталось {remaining} сек.", show_alert=True)
        return

    await callback.answer()
    new_balance = await add_to_balance(user_id, MINE_REWARD)

    text = (
        f"⛏ Красава, +{MINE_REWARD:,} ₽!\n"
        f"Баланс: {new_balance:,} ₽"
    )

    data = await state.get_data()
    farm_msg_id = data.get("farm_msg_id")

    if farm_msg_id:
        try:
            await callback.bot.edit_message_text(
                text=(
                    f"⛏ Красава, +{MINE_REWARD:,} ₽!\n"
                    f"Баланс: {new_balance:,} ₽"
                ),
                chat_id=callback.message.chat.id,
                message_id=farm_msg_id,
                reply_markup=None,
            )
            return
        except TelegramBadRequest:
            pass

    sent = await callback.message.answer(text, reply_markup=None)
    await state.update_data(farm_msg_id=sent.message_id)

@router.callback_query(F.data == "mine_exit")
async def handle_mine_exit(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()

    try:
        await callback.message.edit_text(
            text=callback.message.text,
            reply_markup=None
        )
    except Exception:
        pass

    await callback.message.answer(
        "Выбирай, чем займешься:",
        reply_markup=get_work_keyboard()
    )

@router.message(F.text == "🔗 Реф")
async def handle_ref(message: Message):
    await message.answer("Раздел «Реф» скоро зальём.")

# --- ТРЕЙДИНГ ---
@router.message(F.text == "📈 Трейдинг")
async def handle_trading(message: Message, state: FSMContext):
    user_id = message.from_user.id
    balance = await get_balance(user_id)

    if balance < TRADING_MIN_BALANCE:
        await message.answer(
            f"❌ Не хватает денег для трейдинга.\n"
            f"Минимум на вход: {TRADING_MIN_BALANCE:,} ₽\n"
            "Сначала пофарми в шахте — накопишь нужную сумму.",
            reply_markup=get_work_keyboard()
        )
        return

    await message.answer("📈 Трейдинг открыт!", reply_markup=ReplyKeyboardRemove())
    sent = await message.answer(
        f"💰 Твой баланс: {balance:,} ₽\n"
        "Введи сумму ставки (целое число больше 0):",
        reply_markup=get_trading_result_keyboard2()
    )
    await state.update_data(amount_msg_id=sent.message_id)
    await state.set_state(TradingForm.waiting_for_amount)

@router.callback_query(F.data.startswith("trade_mode:"))
async def handle_trade_mode(callback: CallbackQuery, state: FSMContext):
    mode = callback.data.split(":")[1]
    info = TRADING_MODES[mode]
    await state.update_data(trade_mode=mode)
    await callback.message.edit_text(
        f"📈 Режим: «{mode}»\n"
        f"Множитель: x{info['multiplier']}\n"
        f"Шанс на победу: {info['chance']*100:.0f}%\n\n"
        "Введи сумму ставки:",
        reply_markup=None
    )
    await state.set_state(TradingForm.waiting_for_amount)

@router.message(TradingForm.waiting_for_amount)
async def process_trading_amount(message: Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    amount_msg_id = data.get("amount_msg_id")
    try:
        amount = int(message.text)
        if amount <= 0:
            await message.answer("Сумма должна быть больше 0. Попробуй ещё раз:")
            return
    except ValueError:
        await message.answer("Введи целое число (например, 100):")
        return
    balance = await get_balance(user_id)
    if amount > balance:
        await message.answer(f"Не хватает денег! Баланс: {balance:,} ₽\nВведи меньше:")
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
async def handle_trade_confirm(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    amount = data["amount"]
    mode = data["trade_mode"]
    user_id = callback.from_user.id

    msg = await callback.message.answer("🎲 Расчёт сделки...")

    await asyncio.sleep(random.uniform(1.0, 1.5))

    info = TRADING_MODES[mode]
    won = random.random() < info["chance"]
    multiplier = info["multiplier"]

    if won:
        profit = int(amount * multiplier) - amount
        await add_to_balance(user_id, profit)
        result_text = (
            f"🎉 Забрал!\n"
            f"Режим: {mode}\n"
            f"Ставка: {amount:,} ₽\n"
            f"Чистыми: +{profit:,} ₽ (x{multiplier})"
        )
    else:
        result_text = (
            f"💥 Мимо...\n"
            f"Режим: {mode}\n"
            f"Ставка: {amount:,} ₽\n"
            f"Ставка сгорела."
        )

    await msg.edit_text(result_text)
    await log_trade(user_id, mode, amount, profit if won else -amount, won)
    await state.clear()

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
        await callback.message.answer("❌ Денег не хватает на новую ставку.", reply_markup=get_work_keyboard())
        await state.clear()
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    sent = await callback.message.answer(
        f"💰 Твой баланс: {balance:,} ₽\nВведи сумму ставки (целое число больше 0):",
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
    await callback.message.answer("🚪 Вышел из трейдинга.", reply_markup=get_work_keyboard())

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
        f"🧮 Математика! За правильный ответ: {MATH_REWARD:,} ₽",
        reply_markup=ReplyKeyboardRemove()
    )
    sent = await message.answer(
        f"🧮 Реши пример!\n\n<b>{problem_text}</b>\n\nПиши ответ числом:",
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
        await message.answer("Этот пример уже решён. Жми «Следующий»!")
        return
    try:
        user_answer = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Пиши число — ответ примера:")
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
        result_text = f"✅ Точно! +{MATH_REWARD:,} ₽!\nБаланс: {new_balance:,} ₽"
    else:
        result_text = f"❌ Мимо. Правильный ответ: {correct_answer}"
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
        f"🧮 Реши пример!\n\n<b>{problem_text}</b>\n\nПиши ответ числом:",
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
    await callback.message.answer("🚪 Вышел из математики.", reply_markup=get_work_keyboard())

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
            "Выбирай, чем займешься:",
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
            await callback.answer("У тебя уже есть бизнес! Сначала продай его.", show_alert=True)
            return
        balance = await get_balance(user_id)
        if balance < biz_def["price"]:
            await callback.answer("Не хватает денег!", show_alert=True)
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
            f"✅ Взял «{biz_def['name']}» за {biz_def['price']:,} ₽!\n\n" + text,
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
        source_text = "основного баланса" if source == "user" else "со счёта бизнеса"
        await callback.message.answer(
            f"Введи количество сырья для закупки.\n"
            f"Цена: {RAW_PRICE} ₽ за штуку\n"
            f"Свободно на складе: {space:,}\n"
            f"Оплата: {source_text}"
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
                await callback.answer(f"На счёте бизнеса не хватает {cost - biz_balance:,} ₽", show_alert=True)
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
            f"⚠️ Точно продаёшь «{biz['name']}»?\n\n"
            f"На руки получишь: {sell_price:,} ₽\n\n"
            f"После продажи бизнес исчезнет, бабки упадут на баланс."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, продаю", callback_data="biz_sell_do"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="biz_manage"),
            ],
        ])
        await biz_edit(callback, text, kb)
        return

    if data == "biz_sell_do":
        await callback.answer("Продали.")
        sell_price = biz_sell_price(biz)
        await add_to_balance(user_id, sell_price)
        await save_biz(user_id, None)
        text, kb = biz_no_biz_view()
        await callback.message.edit_text(
            f"✅ Бизнес продан! На руках: {sell_price:,} ₽.\n\n" + text,
            reply_markup=kb
        )
        return

    if data == "biz_collect":
        biz_balance = biz.get("balance", 0)
        if biz.get("raw_stock", 0) <= 0 and biz_balance <= 0:
            await callback.answer("Бизнес стоит — сырья и денег нет!", show_alert=True)
            return
        if biz_balance < 1:
            await callback.answer("Ещё не накапало.", show_alert=True)
            return
        await callback.answer()
        await add_to_balance(user_id, biz_balance)
        biz["balance"] = 0
        biz["last_collected"] = time.time()
        await save_biz(user_id, biz)
        text, kb = biz_manage_view(biz)
        await callback.message.edit_text(
            f"💰 Забрал {biz_balance:,} ₽!\n\n" + text,
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
            await message.answer("Количество должно быть больше 0. Попробуй ещё раз:")
            return
    except ValueError:
        await message.answer("Введи число:")
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
        await message.answer(f"На складе не хватает места! Свободно: {space:,}\nВведи меньше:")
        return

    cost = amount * RAW_PRICE

    if source == "user":
        balance = await get_balance(user_id)
        if balance < cost:
            await message.answer(f"Не хватает {cost - balance:,} ₽. Баланс: {balance:,} ₽\nВведи меньше:")
            return
        await add_to_balance(user_id, -cost)
    else:
        biz_balance = biz.get("balance", 0)
        if biz_balance < cost:
            await message.answer(f"На счёте бизнеса не хватает {cost - biz_balance:,} ₽. Там: {biz_balance:,} ₽\nВведи меньше:")
            return
        biz["balance"] = biz_balance - cost

    biz["raw_stock"] = stock + amount
    await save_biz(user_id, biz)
    await state.clear()

    await message.answer(
        f"✅ Затарил {amount:,} шт. сырья за {cost:,} ₽\n"
        f"Склад: {biz['raw_stock']:,}/{capacity:,}"
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
        f"🎡 Рулетка\n\n"
        f"💰 Твой баланс: {balance:,} ₽\n\n"
        f"Введи сумму ставки:",
        reply_markup=get_roulette_amount_keyboard(0)
    )
    await state.update_data(amount_msg_id=sent.message_id, amount=0)


@router.message(F.text == "🎰 Казино")
async def show_casino(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🎰 Казино\n\nза какой стол хочешь сесть?:",
        reply_markup=get_casino_keyboard()
    )


@router.callback_query(F.data == "casino_menu")
async def casino_menu(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    await callback.message.answer(
        "🎰 Казино\n\nза какой стол хочешь сесть?:",
        reply_markup=get_casino_keyboard()
    )

@router.message(F.text == "🎰 Рулетка")
async def casino_roulette(message: Message, state: FSMContext):
    await state.clear()
    
    # Путь к твоему файлу с картинкой рулетки
    photo_path = "images/roulette_table.png" 
    
    try:
        await message.answer_photo(
            photo=FSInputFile(photo_path),
            reply_markup=ReplyKeyboardRemove()
        )
    except FileNotFoundError:
        # Если картинки нет, отправляем текст как запасной вариант, чтобы бот не молчал
        await message.answer(
            "🎰 Рулетка открыта (картинка временно недоступна).",
            reply_markup=ReplyKeyboardRemove()
        )
        logger.warning(f"Файл {photo_path} не найден!")

    await roulette_show_amount(message, state)

@router.callback_query(F.data == "roulette_amount_noop")
async def roulette_amount_noop(callback: CallbackQuery):
    await callback.answer("Введи сумму сообщением.")


@router.message(RouletteForm.waiting_for_amount)
async def process_roulette_amount(message: Message, state: FSMContext):
    user_id = message.from_user.id

    try:
        amount = int(message.text.strip())
    except (TypeError, ValueError):
        await message.answer("❌ Введи целое число, например: 100")
        return

    if amount <= 0:
        await message.answer("❌ Ставка должна быть больше 0.")
        return

    balance = await get_balance(user_id)
    if amount > balance:
        await message.answer(
            f"❌ Не хватает денег.\n"
            f"Баланс: {balance:,} ₽\n"
            f"Введи меньше:"
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
        f"🎡 Рулетка\n\n"
        f"🎯 На что ставишь?",
        reply_markup=get_roulette_bet_keyboard(amount)
    )


@router.callback_query(RouletteForm.waiting_for_amount, F.data.startswith("roulette_mul:"))
async def roulette_change_amount(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    current = int(data.get("amount", 0))
    multiplier = callback.data.split(":")[1]

    if current <= 0:
        await callback.answer("Сначала введи сумму ставки.", show_alert=True)
        return

    new_amount = int(current * float(multiplier))
    balance = await get_balance(callback.from_user.id)

    if new_amount <= 0:
        await callback.answer("Минимальная ставка — 1 ₽.", show_alert=True)
        return
    if new_amount > balance:
        await callback.answer(
            f"Не хватает денег. Баланс: {balance:,} ₽",
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
        await callback.answer(f"Не хватает денег. Баланс: {balance:,} ₽", show_alert=True)
        return

    await state.update_data(amount=new_amount)
    await callback.answer(f"Ставка: {new_amount:,} ₽")

    try:
        await callback.message.edit_reply_markup(
            reply_markup=get_roulette_bet_keyboard(new_amount)
        )
    except TelegramBadRequest:
        pass


@router.callback_query(RouletteForm.waiting_for_bet, F.data == "roulette_amount_noop")
async def roulette_amount_noop_bet(callback: CallbackQuery):
    await callback.answer("Жми 0.5/2 или выбери ставку.")


@router.callback_query(RouletteForm.waiting_for_bet, F.data.startswith("roulette_bet:"))
async def process_roulette_bet(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    state_data = await state.get_data()
    amount = int(state_data.get("amount", 0))
    bet = callback.data.split(":", 1)[1]

    if amount <= 0:
        await callback.answer("Сначала введи сумму ставки.", show_alert=True)
        return

    balance = await get_balance(user_id)
    if amount > balance:
        await callback.answer("Не хватает денег на эту ставку.", show_alert=True)
        return

    await callback.answer()
    await add_to_balance(user_id, -amount)

    last_result_message_id = state_data.get("last_result_message_id")
    chat_id = callback.message.chat.id

    spin_text = (
        f"🎡 <b>КРУТИМ...</b>\n\n"
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

    random.shuffle(spin_frames)

    delays = [0.8, 0.9, 0.10, 0.11, 0.11, 0.11, 0.11, 0.15, 0.16, 0.17, 0.20, 0.22, 0.23]

    for i, frame in enumerate(spin_frames):
        try:
            await callback.bot.edit_message_text(
                chat_id=chat_id,
                message_id=spin_message.message_id,
                text=(
                    f"<b>КРУТИМ...</b>\n\n"
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
            f"🎡 <b>СТОП!</b>\n\n"
            f"Выпало: {color} <b>{number}</b>\n"
            f"Твоя ставка: <b>{roulette_bet_name(bet)}</b>\n\n"
            f"✅ <b>ВЫЙГРЫШ!</b>\n"
            f"🎉 Выигрыш: +{amount * payout_mult:,} ₽\n"
            f"💰 Баланс: <b>{new_balance:,} ₽</b>"
        )
    else:
        new_balance = await get_balance(user_id)

        result_text = (
            f"🎡 <b>СТОП!</b>\n\n"
            f"Выпало: {color} <b>{number}</b>\n"
            f"Твоя ставка: <b>{roulette_bet_name(bet)}</b>\n\n"
            f"❌ <b>ПРОИГРЫШ!</b>\n"
            f"💸 Ставка {amount:,} ₽ сгорела.\n"
            f"💰 Баланс: <b>{new_balance:,} ₽</b>"
        )

    await callback.bot.edit_message_text(
        chat_id=chat_id,
        message_id=spin_message.message_id,
        text=result_text,
        parse_mode="HTML",
        reply_markup=None
    )

@router.callback_query(F.data.startswith("roulette_mul:"))
async def roulette_change_amount_outside_state(callback: CallbackQuery):
    await callback.answer("Сначала открой рулетку и введи ставку.", show_alert=True)


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