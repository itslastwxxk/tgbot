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
from aiogram.types import ReplyKeyboardRemove, FSInputFile
from aiogram.exceptions import TelegramBadRequest
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
_pickaxe_cache: dict[int, int] = {}
_stats_cache: dict[int, dict] = {}
_daily_streak_cache: dict[int, int] = {}
_daily_last_claim_cache: dict[int, float] = {}

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

class DuelForm(StatesGroup):
    waiting_for_target = State()

# ============================================================
# КОНСТАНТЫ HELP
# ============================================================
HELP_TEXT_MAIN = (
    "бот коммерсант - тут можно зарабатывать деньги, торговать, делать бизнес(и многое другое)\n\n"
    "жми кнопки ниже, расскажу про каждый раздел."
)

HELP_TEXT_TRADING = (
    "Трейдинг — это торговля на рынке криптовалюты с разной степенью риска.\n\n"
    "Как это работает:\n"
    "1. Выбираешь риск: низкий (высокий шанс победы, но выйгрыш небольшой), средний (шанс 50на50, выйгрыш х2), высокий (маленький шанс, но выйгрыш х5 от ставки!!).\n"
    "2. Вводи сумму ставки\n"
    "3. Бот проверяет рынок и показывает результат.\n\n"
)

HELP_TEXT_MINE = (
    "Шахта — самый простой способ заработать первые деньги.\n"
    "Нажал = получил деньги."
)

HELP_TEXT_MATH = (
    "Математика — решил пример = получил деньги.\n\n"
)

HELP_TEXT_BUSINESS = (
    "Бизнесы — это пассивный доход: ты покупаешь бизнес, и он приносит деньги каждую минуту.\n\n"
    "Бизнесу нужно сырьё. Если оно заканчивается, бизнес перестаёт работать и доход останавливается.\n\n"
)

GREETINGS = [
    "вечер в хату, {name}.",
    "добро пожаловать обратно, {name}!",
    "рад видеть тебя снова, {name}.",
    "салют, {name}! Как дела?",
    "привет-привет, {name}.",
    "о, {name}, давно не виделись!",
    "на связи, {name}.",
    "как жизнь, {name}?",
    "здорово, {name}!",
    "рад тебя видеть, {name}.",
    "мир твоему миру, {name}.",
    "какие люди и без охраны, {name}.",
    "честь имею, {name}.",
    "здрав буде, {name}.",
    "добро пожаловать отсюда, {name}.",
    "рад снова с тобой увидится, {name}.",
    "связь, {name}.",
    "я тебя могну, {name}.",
    "ку, {name}.",
    "салам, {name}.",
    "сап, {name}."
]

# ============================================================
# КОНСТАНТЫ ЕЖЕДНЕВНОГО БОНУСА
# ============================================================
DAILY_BONUS_BASE = 3000          # базовая награда
DAILY_BONUS_STREAK_MULT = 0.20    # +20% за каждый день стрика
DAILY_BONUS_MAX_STREAK = 100     # потолок множителя
DAILY_BONUS_RANDOM_MIN = 500    # случайная прибавка — минимум
DAILY_BONUS_RANDOM_MAX = 2000    # случайная прибавка — максимум
DAILY_BONUS_COOLDOWN = 86400     # 24 часа

# ============================================================
# КОНСТАНТЫ РЕФЕРАЛЬНОЙ СИСТЕМЫ
# ============================================================
REFERRAL_REWARD = 10000        # награда пригласившему
REFERRAL_NEWBIE_BONUS = 5000   # бонус новичку за регистрацию по ссылке

# ============================================================
# ЭКОНОМИКА: КОНСТАНТЫ
# ============================================================
MINE_REWARD = 100
MINE_COOLDOWN = 3
MATH_REWARD = 500
MATH_COOLDOWN = 10
RAW_PRICE = 2

PICKAXE_LEVELS = [
    {"name": "Деревянная",  "reward": 10,   "cost": 0},
    {"name": "Каменная",    "reward": 50,   "cost": 100},
    {"name": "Железная",    "reward": 150,   "cost": 1000},
    {"name": "Золотая",     "reward": 300,  "cost": 4000},
    {"name": "Алмазная",    "reward": 600,  "cost": 12000},
    {"name": "Незеритовая", "reward": 1200,  "cost": 36000},
    {"name": "Аметистовая", "reward": 2400,  "cost": 84000},
]

TRADING_MIN_BALANCE = 20000

TRADING_MODES = {
    "low":  {"multiplier": 1.2, "chance": 0.75},  # EV = -10% (казино в плюсе)
    "mid":  {"multiplier": 2.0, "chance": 0.48},  # EV = -4%
    "high": {"multiplier": 5.0, "chance": 0.18},  # EV = -10%
}

ROULETTE_HOUSE_RIG = 0.05
DUEL_TIMEOUT = 3600
DUEL_COOLDOWN = 60

XP_PER_MINE = 50
XP_PER_TRADE = 100
XP_PER_DUEL = 200

# --- ЛВЛ РАЗБЛОКИРОВКИ ---
MATH_UNLOCK_LEVEL = 2  # Математика открывается на 2 уровне

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
        "income_per_min": 9_000,
        "raw_consumption_per_min": 4_500,
        "raw_capacity": 3_360_000,
    },
    {
        "name": "Гипермаркет",
        "price": 15_000_000,
        "income_per_min": 50_000,
        "raw_consumption_per_min": 30_000,
        "raw_capacity": 19_800_000,
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

# --- Функции расчёта уровня ---
def total_xp_for_level(level: int) -> int:
    """Сколько суммарно XP нужно для достижения уровня."""
    return 100 * level * (level + 1) // 2

def xp_to_level(total_xp: int) -> int:
    """Возвращает уровень по суммарному XP."""
    level = 0
    while total_xp >= total_xp_for_level(level + 1):
        level += 1
    return level

def xp_for_next_level(level: int) -> int:
    """Сколько XP нужно для перехода на следующий уровень."""
    return 100 * (level + 1)

def xp_in_current_level(total_xp: int, level: int) -> int:
    """XP, заработанный в текущем уровне (не суммарный)."""
    return total_xp - total_xp_for_level(level)

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
        if stock <= 0:
            biz.setdefault("empty_since", last)
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
        biz.setdefault("empty_since", last + max_run_minutes * 60)

    if biz.get("raw_stock", 0) > 0:
        biz.pop("empty_since", None)
        biz.pop("empty_notified", None)

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
        f"💰 Доход: {biz.get('income_per_min', 0):,} ₽/мин\n"
        f"📦 Расход сырья: {consumption:,}/мин\n"
        f"💸 Чистыми: {net_profit:,} ₽/мин — в плюсах\n"
        f"📦 Склад: {biz.get('raw_stock', 0):,}/{biz.get('raw_capacity', 30000):,}\n"
        f"⏳ Хватит на: {format_time(time_left)}\n"
        f"💳 На счету бизнеса: {biz_balance:,} ₽\n\n"
    )
    if biz.get("raw_stock", 0) <= 0:
        text += "⚠️ Бизнес встал — сырья ноль!\nЖми «📦 Склад», затарься."
    else:
        text += "✅ Бизнес работает!"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📦 Склад", callback_data="biz_wh"),
            InlineKeyboardButton(text="🚀 Прокачать", callback_data="biz_up"),
        ],
        [InlineKeyboardButton(text="💰 Снять деньги", callback_data="biz_collect")],
        [InlineKeyboardButton(text="💸 Продать", callback_data="biz_sell")],
        [
            InlineKeyboardButton(text="🔄 Обновить", callback_data="biz_refresh"),
            InlineKeyboardButton(text="🔙 Выйти", callback_data="biz_exit"),
        ],
    ])
    return text, kb


def biz_no_biz_view():
    text = "🏪 Бизнесы\n\nУ тебя нет бизнеса.\nЖми кнопку ниже, выбери себе точку"
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
        f"🏪 Купить бизнес\n\n"
        f"🏗 {biz['name']}\n"
        f"💸 Цена: {biz['price']:,} ₽\n"
        f"💰 Доход: {biz['income_per_min']:,} ₽/мин\n"
        f"📦 Расход сырья: {consumption:,}/мин\n"
        f"💸 Чистыми: {net:,} ₽/мин\n"
        f"📦 Склад: {biz['raw_capacity']:,}\n"
        f"💸 Заполнить склад: {full_stock_cost:,} ₽\n"
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
        f"💰 Доход: {biz['income_per_min']:,} ₽/мин\n"
        f"📦 Расход сырья: {biz.get('raw_consumption_per_min', 0):,}/мин\n"
        f"💸 Чистыми: {current_net:,} ₽/мин\n"
        f"📦 Склад: {biz.get('raw_capacity', 30000):,}\n\n"
        f"⬆️ После прокачки (уровень {new_level}):\n"
        f"💰 Доход: {new_income:,} ₽/мин\n"
        f"📦 Расход сырья: {new_consumption:,}/мин\n"
        f"💸 Чистыми: {new_net:,} ₽/мин\n"
        f"📦 Склад: {new_capacity:,}\n"
        f"💸 Цена: {cost:,} ₽"
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
        f"(50% цены + 50% сырья + баланс бизнеса)"
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

def format_cooldown(seconds: int | float) -> str:
    """Остаток кулдауна в формате ЧЧ:ММ:СС."""
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


# --- Атомарные операции с балансом ---
_deduct_sha: str | None = None

DEDUCT_LUA = """
local balance = tonumber(redis.call('HGET', KEYS[1], 'balance') or '0')
local amount = tonumber(ARGV[1])
if balance >= amount then
    redis.call('HINCRBY', KEYS[1], 'balance', -amount)
    return 1
else
    return 0
end
"""

async def _ensure_deduct_script():
    """Загружает Lua-скрипт в Redis (кешируется SHA)."""
    global _deduct_sha
    if _deduct_sha is None:
        _deduct_sha = await redis_client.script_load(DEDUCT_LUA)

async def get_balance(user_id: int) -> int:
    if user_id in _balance_cache:
        return _balance_cache[user_id]
    data = await redis_client.hgetall(f"user:{user_id}")
    balance = int(float(data.get("balance", "0")))
    _balance_cache[user_id] = balance
    return balance

async def add_to_balance(user_id: int, amount: int) -> int:
    """Атомарно начисляет деньги (через HINCRBY). Возвращает новый баланс."""
    new_balance = await redis_client.hincrby(f"user:{user_id}", "balance", amount)
    _balance_cache[user_id] = new_balance
    await redis_client.zadd("leaderboard:balance", {str(user_id): new_balance})
    return new_balance

async def deduct_balance(user_id: int, amount: int) -> bool:
    """Атомарно списывает деньги через Lua-скрипт.
    True — успешно, False — не хватает баланса."""
    await _ensure_deduct_script()
    result = await redis_client.evalsha(_deduct_sha, 1, f"user:{user_id}", amount)
    if int(result) == 1:
        new_balance = await get_balance(user_id)
        _balance_cache[user_id] = new_balance
        await redis_client.zadd("leaderboard:balance", {str(user_id): new_balance})
        return True
    return False

async def set_balance(user_id: int, amount: int):
    await redis_client.hset(f"user:{user_id}", mapping={"balance": str(amount)})
    _balance_cache[user_id] = amount
    await redis_client.zadd("leaderboard:balance", {str(user_id): amount})

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

# --- ЕЖЕДНЕВНЫЙ БОНУС: хелперы ---
async def get_daily_streak(user_id: int) -> int:
    if user_id in _daily_streak_cache:
        return _daily_streak_cache[user_id]
    data = await redis_client.hgetall(f"user:{user_id}")
    streak_str = data.get("daily_streak", "0")
    try:
        streak = int(streak_str)
    except ValueError:
        streak = 0
    _daily_streak_cache[user_id] = streak
    return streak

async def get_daily_last_claim(user_id: int) -> float:
    if user_id in _daily_last_claim_cache:
        return _daily_last_claim_cache[user_id]
    data = await redis_client.hgetall(f"user:{user_id}")
    last_str = data.get("daily_last_claim", "0")
    try:
        last = float(last_str)
    except ValueError:
        last = 0.0
    _daily_last_claim_cache[user_id] = last
    return last

async def can_claim_daily(user_id: int) -> tuple[bool, int]:
    """Проверяет, можно ли забрать ежедневный бонус.
    Возвращает (можно, осталось_секунд)."""
    last_claim = await get_daily_last_claim(user_id)
    if last_claim == 0.0:
        return True, 0
    now = time.time()
    elapsed = now - last_claim
    if elapsed >= DAILY_BONUS_COOLDOWN:
        return True, 0
    remaining = DAILY_BONUS_COOLDOWN - int(elapsed)
    return False, max(remaining, 0)

async def daily_bonus_amount(streak: int) -> int:
    """Считает сумму бонуса: база * (1 + 0.2 * стрик) + случайная прибавка."""
    effective_streak = min(streak, DAILY_BONUS_MAX_STREAK)
    base = DAILY_BONUS_BASE * (1 + DAILY_BONUS_STREAK_MULT * effective_streak)
    random_bonus = random.randint(DAILY_BONUS_RANDOM_MIN, DAILY_BONUS_RANDOM_MAX)
    return int(base + random_bonus)

async def claim_daily_bonus(user_id: int) -> tuple[int, int]:
    """Начисляет бонус, обновляет стрик.
    Возвращает (сумма_бонуса, новый_стрик)."""
    now = time.time()
    last_claim = await get_daily_last_claim(user_id)
    current_streak = await get_daily_streak(user_id)

    if last_claim == 0.0:
        new_streak = 1
    elif (now - last_claim) >= 86400 * 2:
        # Пропустил больше 48 часов — стрик обнуляется
        new_streak = 1
    else:
        new_streak = current_streak + 1

    amount = await daily_bonus_amount(new_streak - 1)
    await add_to_balance(user_id, amount)
    await redis_client.hset(f"user:{user_id}", mapping={
        "daily_streak": str(new_streak),
        "daily_last_claim": str(now),
    })
    _daily_streak_cache[user_id] = new_streak
    _daily_last_claim_cache[user_id] = now
    return amount, new_streak

def get_daily_bonus_keyboard(can_claim: bool):
    """Клавиатура окна ежедневного бонуса."""
    rows = []
    if can_claim:
        rows.append([InlineKeyboardButton(text="🎁 Забрать бонус", callback_data="daily_claim")])
    rows.append([InlineKeyboardButton(text="🔙 В меню", callback_data="daily_back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ============================================================
# РЕФЕРАЛЬНАЯ СИСТЕМА: хелперы
# ============================================================
async def get_referral_count(user_id: int) -> int:
    data = await redis_client.hgetall(f"user:{user_id}")
    try:
        return int(data.get("referral_count", "0"))
    except ValueError:
        return 0

async def get_referrer(user_id: int) -> int | None:
    data = await redis_client.hgetall(f"user:{user_id}")
    ref = data.get("referrer", "")
    return int(ref) if ref else None

async def set_referrer(user_id: int, referrer_id: int):
    await redis_client.hset(f"user:{user_id}", mapping={"referrer": str(referrer_id)})

async def increment_referral_count(referrer_id: int) -> int:
    new_count = await redis_client.hincrby(f"user:{referrer_id}", "referral_count", 1)
    await redis_client.zadd("referrals_top", {str(referrer_id): new_count})
    return new_count

async def get_referral_earnings(user_id: int) -> int:
    data = await redis_client.hgetall(f"user:{user_id}")
    try:
        return int(data.get("referral_earnings", "0"))
    except ValueError:
        return 0

async def add_to_referral_earnings(user_id: int, amount: int):
    await redis_client.hincrby(f"user:{user_id}", "referral_earnings", amount)

async def process_referral(new_user_id: int, referrer_id: int) -> tuple[int, str] | None:
    """Обрабатывает реферала. Возвращает (new_count, referrer_name) или None."""
    existing = await get_referrer(new_user_id)
    if existing:
        return None
    referrer_name = await get_user_name(referrer_id)
    if not referrer_name:
        return None
    await set_referrer(new_user_id, referrer_id)
    new_count = await increment_referral_count(referrer_id)
    await add_to_balance(referrer_id, REFERRAL_REWARD)
    await add_to_referral_earnings(referrer_id, REFERRAL_REWARD)
    await add_to_balance(new_user_id, REFERRAL_NEWBIE_BONUS)
    try:
        await bot.send_message(
            referrer_id,
            f"🎉 По твоей ссылке зарегистрировался {referrer_name}!\n"
            f"💰 Награда: +{REFERRAL_REWARD:,} ₽\n"
            f"👥 Всего рефералов: {new_count}"
        )
    except Exception:
        pass
    return new_count, referrer_name

async def get_top_referrals(limit: int = 10) -> list[tuple[int, int]]:
    raw = await redis_client.zrevrange("referrals_top", 0, limit - 1, withscores=True)
    return [(int(uid), int(score)) for uid, score in raw]

# --- Дуэли: хелперы ---
async def create_duel(challenger_id: int, target_id: int, amount: int) -> str:
    duel_id = uuid.uuid4().hex[:8]
    await redis_client.hset(f"duel:{duel_id}", mapping={
        "challenger_id": str(challenger_id),
        "target_id": str(target_id),
        "amount": str(amount),
        "status": "pending",
        "created_at": str(time.time()),
    })
    await redis_client.expire(f"duel:{duel_id}", DUEL_TIMEOUT)
    return duel_id

async def get_duel(duel_id: str) -> dict | None:
    data = await redis_client.hgetall(f"duel:{duel_id}")
    if not data:
        return None
    return {
        "challenger_id": int(data["challenger_id"]),
        "target_id": int(data["target_id"]),
        "amount": int(data["amount"]),
        "status": data.get("status", "pending"),
    }

async def update_duel_status(duel_id: str, status: str):
    await redis_client.hset(f"duel:{duel_id}", "status", status)

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

# --- Кирка ---
async def get_pickaxe_level(user_id: int) -> int:
    """Возвращает уровень кирки (0 = деревянная)."""
    if user_id in _pickaxe_cache:
        return _pickaxe_cache[user_id]
    data = await redis_client.hgetall(f"user:{user_id}")
    try:
        level = int(data.get("pickaxe_level", "0"))
    except (TypeError, ValueError):
        level = 0
    _pickaxe_cache[user_id] = level
    return level

async def set_pickaxe_level(user_id: int, level: int):
    await redis_client.hset(f"user:{user_id}", "pickaxe_level", str(level))
    _pickaxe_cache[user_id] = level

def get_mine_reward_for_pickaxe(level: int) -> int:
    """Доход за клик в зависимости от уровня кирки."""
    if 0 <= level < len(PICKAXE_LEVELS):
        return PICKAXE_LEVELS[level]["reward"]
    return PICKAXE_LEVELS[-1]["reward"]

def get_pickaxe_upgrade_view(current_level: int, balance: int):
    """Текст и клавиатура окна прокачки кирки."""
    current = PICKAXE_LEVELS[current_level]

    if current_level >= len(PICKAXE_LEVELS) - 1:
        text = (
            f"🔧 Прокачка кирки\n\n"
            f"Твоя кирка: {current['name']} (макс. уровень!)\n"
            f"💰 Доход: {current['reward']:,} ₽ за клик\n\n"
            f"Выше некуда — ты на вершине ⛏"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="pickaxe_back")]
        ])
        return text, kb

    nxt = PICKAXE_LEVELS[current_level + 1]
    can_afford = balance >= nxt["cost"]

    text = (
        f"🔧 Прокачка кирки\n\n"
        f"Текущая: {current['name']} — {current['reward']:,} ₽/клик\n"
        f"Следующая: {nxt['name']} — {nxt['reward']:,} ₽/клик\n"
        f"💸 Цена: {nxt['cost']:,} ₽\n"
        f"💰 Баланс: {balance:,} ₽"
    )

    if can_afford:
        btn = InlineKeyboardButton(
            text=f"✅ Прокачать за {nxt['cost']:,} ₽",
            callback_data="pickaxe_upgrade"
        )
    else:
        btn = InlineKeyboardButton(
            text=f"❌ Не хватает {nxt['cost'] - balance:,} ₽",
            callback_data="pickaxe_noop"
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="pickaxe_back")],
    ])
    return text, kb

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

# --- XP и уровни ---
async def get_user_stats(user_id: int) -> dict:
    if user_id in _stats_cache:
        return _stats_cache[user_id]
    data = await redis_client.hgetall(f"user:{user_id}:stats")
    if not data:
        stats = {"xp": 0, "level": 0}
    else:
        stats = {
            "xp": int(data.get("xp", 0)),
            "level": int(data.get("level", 0)),
        }
    _stats_cache[user_id] = stats
    return stats

async def add_xp(user_id: int, amount: int) -> tuple[int, int, bool]:
    """Добавляет XP, обновляет уровень. Возвращает (новый_xp, новый_уровень, был_левелап)."""
    stats = await get_user_stats(user_id)
    old_level = stats["level"]
    new_xp = stats["xp"] + amount
    new_level = xp_to_level(new_xp)
    leveled_up = new_level > old_level

    await redis_client.hset(f"user:{user_id}:stats", mapping={
        "xp": str(new_xp),
        "level": str(new_level),
    })
    await redis_client.zadd("leaderboard:level", {str(user_id): new_level})

    # Обновляем кэш
    new_stats = {"xp": new_xp, "level": new_level}
    _stats_cache[user_id] = new_stats

    return new_xp, new_level, leveled_up

async def notify_level_up(user_id: int, new_level: int):
    """Отправляет сообщение о новом уровне."""
    try:
        await bot.send_message(
            user_id,
            f"🎉 LEVEL UP! Ты достиг {new_level} уровня!"
        )
    except Exception:
        pass

async def get_top_levels(limit: int = 10) -> list[tuple[int, int]]:
    raw = await redis_client.zrevrange("leaderboard:level", 0, limit - 1, withscores=True)
    return [(int(uid), int(score)) for uid, score in raw]

# --- Кулдаун дуэлей ---
async def check_duel_cooldown(user_id: int) -> tuple[bool, int]:
    key = f"cooldown:duel:{user_id}"
    ttl = await redis_client.ttl(key)
    if ttl > 0:
        return False, ttl
    return True, 0

async def set_duel_cooldown(user_id: int):
    await redis_client.set(f"cooldown:duel:{user_id}", "1", ex=DUEL_COOLDOWN)

# --- Хранение последних 15 действий ---
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
    trades = trades[-15:]
    await redis_client.delete(key)
    for t in trades:
        await redis_client.rpush(key, json.dumps(t))

# --- Топ игроков ---
async def get_all_balances() -> list[tuple[int, str, int]]:
    rows = await redis_client.zrevrange("leaderboard:balance", 0, 9, withscores=True)
    results = []
    for uid_raw, score in rows:
        try:
            uid = int(uid_raw)
        except (TypeError, ValueError):
            continue
        data = await redis_client.hgetall(f"user:{uid}")
        name = data.get("name") or data.get("username") or "Игрок"
        results.append((uid, name, int(score)))
    return results


async def rebuild_balance_leaderboard():
    async for key in redis_client.scan_iter(match="user:*", count=200):
        if not key.startswith("user:") or key.count(":") != 1:
            continue
        try:
            uid = int(key.split(":", 1)[1])
        except ValueError:
            continue
        data = await redis_client.hgetall(key)
        try:
            balance = int(float(data.get("balance", "0")))
        except (TypeError, ValueError):
            balance = 0
        await redis_client.zadd("leaderboard:balance", {str(uid): balance})


# --- Проверка имени ---
def is_valid_name(name: str) -> bool:
    name = name.strip()
    if len(name) < 3 or len(name) > 10:
        return False
    return bool(re.match(r'^[a-zA-Zа-яА-ЯёЁ0-9]+$', name))

# --- Главное меню ---
async def send_main_menu(target: Message | CallbackQuery, user_id: int):
    # --- Логика получения данных (без изменений) ---
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
    
    # --- ВЫБОР СЛУЧАЙНОГО ПРИВЕТСТВИЯ ---
    # Выбираем случайную фразу из списка и подставляем имя через форматирование строки
    greeting_template = random.choice(GREETINGS)
    greeting_text = greeting_template.format(name=display_name)
    
    # Формируем полный текст сообщения
    text = f"{greeting_text}\nтвой баланс: {balance:,} ₽\nвыбирай куда направишься"
    # -------------------------------------

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
        [
        InlineKeyboardButton(text="📊 Топ по балансу", callback_data="admin_top"),
        InlineKeyboardButton(text="👥 Топ по рефералам", callback_data="admin_ref_top")
        ],
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
    referral_count = await get_referral_count(player_id)
    biz = await get_biz(player_id)

    if biz:
        biz_text = f"«{biz['name']}» (ур. {biz.get('level', 1)})"
    else:
        biz_text = "нет"

    text = (
        f"👤 Игрок #{player_id}\n\n"
        f"📝 Ник: {name}\n"
        f"👤 Username: @{username}\n"
        f"💰 Баланс: {balance:,} ₽\n"
        f"🏪 Бизнес: {biz_text}\n"
        f"👥 Рефералов: {referral_count}"
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

@router.callback_query(F.data == "admin_ref_top")
async def admin_ref_top(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ закрыт, ты не админ.", show_alert=True)
        return

    await callback.answer()
    top = await get_top_referrals(20)

    if not top:
        await _edit_or_answer(
            callback.bot, callback.message.chat.id, callback.message.message_id,
            "👥 Топ по рефералам\n\nПока пусто — никто никого не пригласил.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 В меню админа", callback_data="admin_main")]
            ]),
        )
        return

    text = "👥 Топ по рефералам (админ-режим):\n\n"
    for i, (uid, count) in enumerate(top, 1):
        name = await get_user_name(uid) or "без ника"
        text += f"{i}. {name} (#{uid}) — {count} реф.\n"

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
        [KeyboardButton(text="🎰 Казино"), KeyboardButton(text="🥊 Дуэли")],
        [KeyboardButton(text="🎁 Ежедневный бонус"), KeyboardButton(text="🏆 Топ")],
        [KeyboardButton(text="📋 Профиль")]
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
    keyboard = [
        [KeyboardButton(text="⛏ Фармить")],
        [KeyboardButton(text="🔧 Прокачать кирку")],
        [KeyboardButton(text="🔙 В меню")],
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

def get_trading_direction_keyboard():
    keyboard = [
        [
            InlineKeyboardButton(text="📈 Вверх", callback_data="trade_up"),
            InlineKeyboardButton(text="📉 Вниз", callback_data="trade_down"),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

_trade_history_cache: dict[int, list[dict]] = {}
def add_trade_history(user_id, trade_data):
    if user_id not in _trade_history_cache:
        _trade_history_cache[user_id] = []
    _trade_history_cache[user_id].append(trade_data)
    # Ограничиваем историю до 10 сделок
    if len(_trade_history_cache[user_id]) > 10:
        _trade_history_cache[user_id] = _trade_history_cache[user_id][-10:]

def get_trading_mode_keyboard():
    """Клавиатура выбора риска"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🟢 Низкий риск (x1.3)", callback_data="trade_mode:low"),
            InlineKeyboardButton(text="🟡 Средний риск (x2.0)", callback_data="trade_mode:mid"),
        ],
        [
            InlineKeyboardButton(text="🔴 Высокий риск (x5.0)", callback_data="trade_mode:high"),
        ],
        [
            InlineKeyboardButton(text="📜 История сделок", callback_data="trade_history:0"),
            InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu"),
        ],
    ])

def get_trading_confirm_keyboard(amount: int, mode: str):
    """Клавиатура подтверждения ставки"""
    modes_text = {
        "low": "Низкий риск",
        "mid": "Средний риск",
        "high": "Высокий риск"
    }
    text = f"Ставка: {amount:,} ₽\nРиск: {modes_text.get(mode, 'Неизвестен')}"

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить ставку", callback_data=f"trade_confirm:{amount}")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="trade_cancel")],
    ])

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
# ИСТОРИЯ СДЕЛОК ТРЕЙДИНГА (пагинация 5 шт. на страницу)
# ============================================================

TRADE_HISTORY_PAGE_SIZE = 5
TRADE_HISTORY_MAX_ITEMS = 10

TRADE_MODE_NAMES = {
    "low": "🟢 Низкий риск",
    "mid": "🟡 Средний риск",
    "high": "🔴 Высокий риск",
}


async def get_trade_history_entries(user_id: int) -> list[dict]:
    """Последние 10 сделок пользователя, новые сверху."""
    raw = await redis_client.lrange(f"user:{user_id}:trades", 0, -1)
    entries = []
    for raw_item in raw or []:
        try:
            entries.append(json.loads(raw_item))
        except (TypeError, json.JSONDecodeError):
            continue
    entries = entries[-TRADE_HISTORY_MAX_ITEMS:][::-1]
    return entries


def render_trade_history_page(entries: list[dict], page: int) -> tuple[str, int]:
    """Возвращает (текст страницы, общее число страниц)."""
    total_pages = max(1, (len(entries) + TRADE_HISTORY_PAGE_SIZE - 1) // TRADE_HISTORY_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    if not entries:
        return "📜 История сделок\n\nПока сделок нет — самое время попробовать!", total_pages

    start = page * TRADE_HISTORY_PAGE_SIZE
    chunk = entries[start:start + TRADE_HISTORY_PAGE_SIZE]

    lines = [f"📜 История сделок (стр. {page + 1}/{total_pages})\n"]
    for i, item in enumerate(chunk, start=start + 1):
        mode = TRADE_MODE_NAMES.get(item.get("mode"), item.get("mode", "Сделка"))
        amount = item.get("amount", 0)
        result = item.get("result", 0)
        win = item.get("win")
        stamp = time.strftime("%d.%m %H:%M", time.localtime(item.get("ts", time.time())))
        status = "✅" if win else "❌"
        lines.append(
            f"{i}. {status} {mode} | 🕒 {stamp}\n"
            f"   Ставка: {amount:,} ₽ | Итог: {result:+,.0f} ₽"
        )
    return "\n".join(lines), total_pages


def get_trade_history_keyboard(page: int, total_pages: int) -> InlineKeyboardMarkup:
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"trade_history:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"trade_history:{page+1}"))
    rows = []
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔙 К трейдингу", callback_data="trade_history_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("trade_history:"))
async def handle_trade_history(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    user_id = callback.from_user.id
    try:
        page = int(callback.data.split(":", 1)[1])
    except ValueError:
        page = 0

    entries = await get_trade_history_entries(user_id)
    text, total_pages = render_trade_history_page(entries, page)
    kb = get_trade_history_keyboard(page if entries else 0, total_pages)

    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "trade_history_back")
async def handle_trade_history_back(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    balance = await get_balance(callback.from_user.id)
    text = f"💰 Твой баланс: {balance:,} ₽\nВыбери уровень риска:"
    try:
        await callback.message.edit_text(text, reply_markup=get_trading_mode_keyboard())
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=get_trading_mode_keyboard())

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

@router.message(Command("menu"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    username = message.from_user.username
    await save_user_info(user_id, username)
    if username:
        _username_cache[user_id] = username.lstrip("@").lower()
        
@router.message(F.text == "start")
async def handle_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    
    # Очищаем состояние, если необходимо
    await state.clear()
    
    # Отправляем начальное меню
    await send_main_menu(message, user_id)

    # --- Парсим реферальный payload ---
    referrer_id = None
    if message.text and len(message.text.split()) > 1:
        payload = message.text.split(maxsplit=1)[1]
        if payload.startswith("ref_"):
            try:
                referrer_id = int(payload[4:])
            except ValueError:
                pass

    name = await get_user_name(user_id)

    if not name:
        # Новый пользователь — сохраняем pending referrer в state
        if referrer_id and referrer_id != user_id:
            referrer_name = await get_user_name(referrer_id)
            if referrer_name:
                await state.update_data(pending_referrer=referrer_id)

        await message.answer(
            "👋 Привет! Как тебя зовут?\n"
            "Введи ник — буквы (русские или английские) и цифры, от 3 до 10 символов.\n"
            "Например: Alex123, Иван4, Макс777\n\n"
            "⚠️ Ник должен быть уникальным — если занят, придётся придумать другой."
        )
        await state.set_state(NameForm.waiting_for_name)
        return

    # Возвращающийся пользователь — обработать реферал сразу
    if referrer_id and referrer_id != user_id:
        result = await process_referral(user_id, referrer_id)
        if result:
            await message.answer(
                f"🎁 Тебя пригласил {result[1]}! "
                f"Бонус за регистрацию: +{REFERRAL_NEWBIE_BONUS:,} ₽"
            )

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

    # Получаем pending_referrer ДО очистки state
    data = await state.get_data()
    pending_referrer = data.get("pending_referrer")

    await save_user_name(user_id, name)
    await state.clear()

    # Обрабатываем реферал
    ref_bonus_text = ""
    if pending_referrer:
        result = await process_referral(user_id, pending_referrer)
        if result:
            ref_bonus_text = (
                f"\n🎁 Тебя пригласил {result[1]}! "
                f"Бонус: +{REFERRAL_NEWBIE_BONUS:,} ₽"
            )

    # Новый игрок проходит короткое обучение; флаг сохраняется в Redis.
    tutorial_done = await redis_client.hget(f"user:{user_id}", "tutorial_done")
    if not tutorial_done:
        await redis_client.hset(f"user:{user_id}", "tutorial_step", "mine")
    await message.answer(f"{ref_bonus_text}")
    if not tutorial_done:
        await message.answer(
            "🎓 Обучение новичка — шаг 1/2\n\n"
            "⛏ Начнём с шахты! Здесь можно добывать деньги и улучшать кирки. "
            "Чем выше уровень кирки, тем больше награда за добычу.\n\n"
            "Открой шахту и прокачай кирку до следующего уровня."
        )
    await send_main_menu(message, user_id)

@router.message(F.text == "📋 Профиль")
async def show_profile(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id

    # --- БЛОК РАСЧЕТА ДАННЫХ (оставь свой код) ---
    balance = await get_balance(user_id)
    stats = await get_user_stats(user_id)
    name = await get_user_name(user_id) or "Игрок"

    level = stats["level"]
    total_xp = stats["xp"]
    xp_needed = xp_for_next_level(level)
    xp_earned = xp_in_current_level(total_xp, level)
    percent = min(100, int((xp_earned / xp_needed) * 100)) if xp_needed > 0 else 100

    bar_len = 15
    filled = percent * bar_len // 100
    bar = "█" * filled + "░" * (bar_len - filled)

    profile_text = (
        f"📋 Профиль: {name}\n\n"
        f"💰 Баланс: {balance:,} ₽\n"
        f"📈 Уровень: {level}\n"
        f"⚡ XP: {xp_earned:,} / {xp_needed:,}\n"
        f"📊 [{bar}] {percent}%"
    )
    # -------------------------------------------

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 В меню", callback_data="main_menu")]
    ])

    # Проверяем наличие картинки
    photo_path = "images/profile.png"
    has_photo = False
    
    try:
        photo = FSInputFile(photo_path)
        has_photo = True
    except FileNotFoundError:
        has_photo = False

    # ==========================================
    # ШАГ 1: ОТПРАВКА ФОТО И УДАЛЕНИЕ КЛАВИАТУРЫ
    # ==========================================
    if has_photo:
        # Отправляем фото. ReplyKeyboardRemove() здесь критически важен!
        # Он убирает reply-клавиатуру в момент отправки этого сообщения.
        await message.answer_photo(
            photo=photo,
            reply_markup=ReplyKeyboardRemove()
        )
    else:
        # Если фото нет, все равно нужно убрать клавиатуру перед текстом.
        # Отправляем невидимое сообщение (с эмодзи) только для сброса кнопок.
        # Это единственный способ сбросить клавиатуру, если нет фото.
        await message.answer(".", reply_markup=ReplyKeyboardRemove())

    # ==========================================
    # ШАГ 2: ОТПРАВКА ПРОФИЛЯ (ТЕКСТ + КНОПКИ)
    # ==========================================
    # Отправляем текст профиля с inline-кнопками.
    # К этому моменту reply-клавиатура уже должна быть убрана.
    await message.answer(
        text=profile_text,
        reply_markup=kb
    )

@router.message(F.text == "💼 Работа")
async def show_work_menu(message: Message, state: FSMContext):
    await state.clear()
    text = f"выбирай, где хочешь работать:"
    try:
        photo = FSInputFile("images/work.png")
        await message.answer_photo(photo=photo, caption=text, reply_markup=get_work_keyboard())
    except FileNotFoundError:
        logger.warning("Файл images/work.png не найден.")
        await message.answer(text, reply_markup=get_work_keyboard())

@router.message(F.text == "🛒 Магаз")
async def show_shop_menu(message: Message):
    await message.answer("Раздел «Магаз» пока в разработке — скоро зальём.")

@router.message(F.text == "🏆 Топ")
async def show_top(message: Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Топ по балансу", callback_data="public_top:balance")],
        [InlineKeyboardButton(text="👥 Топ по рефералам", callback_data="public_top:referrals")],
        [InlineKeyboardButton(text="📈 Топ по уровню", callback_data="public_top:level")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="public_top:back_to_main")],
    ])
    try:
        photo = FSInputFile("images/123.png")
        await message.answer_photo(photo=photo, reply_markup=ReplyKeyboardRemove())
    except FileNotFoundError:
        await message.answer("🏆", reply_markup=ReplyKeyboardRemove())
    await message.answer("🏆 Выбери рейтинг:", reply_markup=kb)


@router.callback_query(F.data.startswith("public_top:"))
async def show_public_top(callback: CallbackQuery):
    user_id = callback.from_user.id
    await callback.answer()

    kind = callback.data.split(":", 1)[1]

    if kind == "back_to_main":
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await send_main_menu(callback, user_id)
        return

    if kind == "menu":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💰 Топ по балансу", callback_data="public_top:balance")],
            [InlineKeyboardButton(text="👥 Топ по рефералам", callback_data="public_top:referrals")],
            [InlineKeyboardButton(text="📈 Топ по уровню", callback_data="public_top:level")],
            [InlineKeyboardButton(text="🔙 В меню", callback_data="public_top:back_to_main")],
        ])
        await callback.message.edit_text("🏆 Выбери рейтинг:", reply_markup=kb)
        return

    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 К выбору топа", callback_data="public_top:menu")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="public_top:back_to_main")],
    ])

    if kind == "balance":
        balances = await get_all_balances()
        if not balances:
            result_text = "🏆 Топ по балансу\n\nПока пусто — никто не играл."
        else:
            result_text = "💰 Топ по балансу:\n\n"
            for i, (uid, name, balance) in enumerate(balances[:10], 1):
                result_text += f"{i}. {name} — {balance:,} ₽\n"
            if len(balances) > 10:
                result_text += f"\n...и ещё {len(balances) - 10} челиков"
    elif kind == "referrals":
        top = await get_top_referrals(10)
        if not top:
            result_text = "👥 Топ по рефералам\n\nПока пусто — никто никого не пригласил."
        else:
            result_text = "👥 Топ по рефералам:\n\n"
            for i, (uid, count) in enumerate(top, 1):
                name = await get_user_name(uid) or "без ника"
                result_text += f"{i}. {name} — {count} реф.\n"
    elif kind == "level":
        top = await get_top_levels(10)
        if not top:
            result_text = "📈 Топ по уровням\n\nПока пусто — никто не получил XP."
        else:
            result_text = "📈 Топ по уровням:\n\n"
            for i, (uid, lvl) in enumerate(top, 1):
                name = await get_user_name(uid) or "без ника"
                result_text += f"{i}. {name} — {lvl} ур.\n"

    else:
        await callback.answer("Неизвестный рейтинг.", show_alert=True)
        return

    try:
        await callback.message.edit_text(result_text, reply_markup=back_kb)
    except TelegramBadRequest:
        await callback.message.answer(result_text, reply_markup=back_kb)


# ============================================================
# ЕЖЕДНЕВНЫЙ БОНУС — ХЕНДЛЕРЫ
# ============================================================
@router.message(F.text == "🎁 Ежедневный бонус")
async def handle_daily_bonus(message: Message, state: FSMContext):
    user_id = message.from_user.id
    await state.clear()

    can, remaining = await can_claim_daily(user_id)
    streak = await get_daily_streak(user_id)

    if can:
        preview_amount = await daily_bonus_amount(streak)
        text = (
            f"🎁 Ежедневный бонус\n\n"
            f"🔥 Серия: {streak} дн. подряд\n"
            f"💰 Сегодня получишь: ~{preview_amount:,} ₽\n\n"
            f"Жми «Забрать», чтобы забрать награду!"
        )
        kb = get_daily_bonus_keyboard(True)
    else:
        text = (
            f"🎁 Ежедневный бонус\n\n"
            f"🔥 Серия: {streak} дн. подряд\n"
            f"⏳ Бонус уже забран. Приходи через:\n"
            f"⏰ {format_cooldown(remaining)}"
        )
        kb = get_daily_bonus_keyboard(False)

    try:
        photo = FSInputFile("images/daily_bonus.png")
        await message.answer_photo(photo=photo, caption=text, reply_markup=kb)
    except FileNotFoundError:
        logger.warning("Файл images/daily_bonus.png не найден.")
        await message.answer(text, reply_markup=kb)

@router.callback_query(F.data == "daily_claim")
async def handle_daily_claim(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    await callback.answer()

    can, remaining = await can_claim_daily(user_id)
    if not can:
        hours = remaining // 3600
        mins = (remaining % 3600) // 60
        secs = remaining % 60
        await callback.message.edit_text(
            f"⏳ Бонус уже забран. Приходи через {hours} ч {mins} мин {secs} сек.",
            reply_markup=get_daily_bonus_keyboard(False)
        )
        return

    amount, new_streak = await claim_daily_bonus(user_id)
    new_balance = await get_balance(user_id)

    text = (
        f"🎁 Ежедневный бонус забран!\n\n"
        f"💰 Получено: +{amount:,} ₽\n"
        f"🔥 Серия: {new_streak} дн. подряд\n"
        f"💳 Баланс: {new_balance:,} ₽\n\n"
        f"Возвращайся завтра — серия продолжится!"
    )
    try:
        await callback.message.edit_text(text, reply_markup=get_daily_bonus_keyboard(False))
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=get_daily_bonus_keyboard(False))


@router.callback_query(F.data == "daily_back_to_menu")
async def handle_daily_back_to_menu(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await send_main_menu(callback, user_id)


# --- ВОЗВРАТЫ ---
@router.message(F.text == "🔙 Назад")
async def handle_back(message: Message, state: FSMContext):
    await state.clear()
    await send_main_menu(message, message.from_user.id)

@router.message(F.text.in_({"🔙 В главное меню", "🔙 В меню"}))
async def handle_back_to_main(message: Message, state: FSMContext):
    await state.clear()
    await send_main_menu(message, message.from_user.id)

@router.message(F.text == "⛏ Шахта")
async def show_mine_menu(message: Message, state: FSMContext):
    await state.set_state(MineForm.in_mine)
    user_id = message.from_user.id
    tutorial_step = await redis_client.hget(f"user:{user_id}", "tutorial_step")
    if tutorial_step == "mine":
        await message.answer("⛏ Ты в шахте! Нажми «🔧 Прокачать кирку», затем подтверди улучшение.")
    pickaxe_lvl = await get_pickaxe_level(user_id)
    pickaxe_name = PICKAXE_LEVELS[pickaxe_lvl]["name"]
    reward = get_mine_reward_for_pickaxe(pickaxe_lvl)
    text = (
        f"⛏ Шахта\n\n"
        f"🔧 Кирка: {pickaxe_name}\n"
        f"💰 За клик: {reward:,} ₽\n"
        f"⏳ КД: {MINE_COOLDOWN} сек\n"
    )
    try:
        photo = FSInputFile("images/mine.png")
        await message.answer_photo(photo=photo, caption=text, reply_markup=get_mine_keyboard())
    except FileNotFoundError:
        logger.warning("Файл images/mine.png не найден.")
        await message.answer(text, reply_markup=get_mine_keyboard())

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

@router.message(MineForm.in_mine, F.text == "⛏ Фармить")
async def handle_mine_farm(message: Message, state: FSMContext):
    user_id = message.from_user.id

    allowed, remaining = await can_farm(user_id, cooldown_seconds=MINE_COOLDOWN)
    if not allowed:
        await message.answer(f"⏳ Осталось {remaining} сек.")
        return

    pickaxe_lvl = await get_pickaxe_level(user_id)
    reward = get_mine_reward_for_pickaxe(pickaxe_lvl)
    pickaxe_name = PICKAXE_LEVELS[pickaxe_lvl]["name"]

    new_balance = await add_to_balance(user_id, reward)
    _, new_level, leveled_up = await add_xp(user_id, XP_PER_MINE)

    text = (
        f"⛏ {pickaxe_name} кирка в деле! +{reward:,} ₽ +{XP_PER_MINE} XP!\n"
        f"Баланс: {new_balance:,} ₽"
    )
    await message.answer(text)

    if leveled_up:
        await notify_level_up(user_id, new_level)

@router.message(MineForm.in_mine, F.text == "🔙 Назад")
async def handle_mine_exit(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Выбирай, чем займешься:",
        reply_markup=get_work_keyboard()
    )

# --- Прокачка кирки: кнопка reply ---
@router.message(MineForm.in_mine, F.text == "🔧 Прокачать кирку")
async def handle_pickaxe_upgrade_menu(message: Message, state: FSMContext):
    user_id = message.from_user.id
    pickaxe_lvl = await get_pickaxe_level(user_id)
    balance = await get_balance(user_id)
    text, kb = get_pickaxe_upgrade_view(pickaxe_lvl, balance)
    await message.answer(text, reply_markup=kb)


# --- Прокачка кирки: кнопка inline "Прокачать" ---
@router.callback_query(MineForm.in_mine, F.data == "pickaxe_upgrade")
async def handle_pickaxe_upgrade_do(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    await callback.answer()

    pickaxe_lvl = await get_pickaxe_level(user_id)

    if pickaxe_lvl >= len(PICKAXE_LEVELS) - 1:
        await callback.answer("Уже максимальный уровень!", show_alert=True)
        return

    nxt = PICKAXE_LEVELS[pickaxe_lvl + 1]
    balance = await get_balance(user_id)

    if balance < nxt["cost"]:
        await callback.answer("Не хватает денег!", show_alert=True)
        return

    ok = await deduct_balance(user_id, nxt["cost"])
    if not ok:
        await callback.answer("Не хватает денег!", show_alert=True)
        return
    new_lvl = pickaxe_lvl + 1
    await set_pickaxe_level(user_id, new_lvl)

    new_balance = await get_balance(user_id)
    text, kb = get_pickaxe_upgrade_view(new_lvl, new_balance)

    tutorial_step = await redis_client.hget(f"user:{user_id}", "tutorial_step")
    if tutorial_step == "mine":
        await redis_client.hset(f"user:{user_id}", "tutorial_step", "math")

    upgrade_msg = (
        f"🎉 Кирка прокачана: {PICKAXE_LEVELS[pickaxe_lvl]['name']} → {nxt['name']}!\n"
        f"Новый доход: {nxt['reward']:,} ₽/клик\n\n"
    )

    if tutorial_step == "mine":
        upgrade_msg += (
            "\n\n🎓 Шаг 1 пройден! Теперь переходи в «🧮 Математика» "
            "и реши пример — за правильный ответ получишь награду."
        )
    try:
        await callback.message.edit_text(upgrade_msg + text, reply_markup=kb)
    except TelegramBadRequest:
        await callback.message.answer(upgrade_msg + text, reply_markup=kb)


# --- Прокачка кирки: пустая кнопка (не хватает денег) ---
@router.callback_query(MineForm.in_mine, F.data == "pickaxe_noop")
async def handle_pickaxe_noop(callback: CallbackQuery):
    await callback.answer("Не хватает денег на прокачку.", show_alert=True)


# --- Прокачка кирки: кнопка "Назад" ---
@router.callback_query(MineForm.in_mine, F.data == "pickaxe_back")
async def handle_pickaxe_back(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    await callback.answer()
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
    # Возвращаем в меню шахты
    pickaxe_lvl = await get_pickaxe_level(user_id)
    pickaxe_name = PICKAXE_LEVELS[pickaxe_lvl]["name"]
    reward = get_mine_reward_for_pickaxe(pickaxe_lvl)
    text = (
        f"⛏ Шахта\n\n"
        f"🔧 Кирка: {pickaxe_name}\n"
        f"💰 За клик: {reward:,} ₽\n"
        f"⏳ КД: {MINE_COOLDOWN} сек\n"
    )
    await callback.message.answer(text, reply_markup=get_mine_keyboard())

@router.message(F.text == "🔗 Реф")
async def handle_ref(message: Message):
    user_id = message.from_user.id
    referral_count = await get_referral_count(user_id)
    referral_earnings = await get_referral_earnings(user_id)
    bot_info = await message.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"

    text = (
        f"🔗 Реферальная система\n\n"
        f"Твоя ссылка:\n`{ref_link}`\n\n"
        f"👥 Приглашено: {referral_count} чел.\n"
        f"💰 Заработано с рефералов: {referral_earnings:,} ₽\n\n"
        f"За каждого приглашённого — {REFERRAL_REWARD:,} ₽\n"
        f"Новичку за регистрацию по ссылке — {REFERRAL_NEWBIE_BONUS:,} ₽"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Топ по рефералам", callback_data="ref_top")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="ref_back")],
    ])
    await message.answer(text, parse_mode="Markdown", reply_markup=kb)


@router.callback_query(F.data == "ref_top")
async def handle_ref_top(callback: CallbackQuery):
    user_id = callback.from_user.id
    await callback.answer()

    if not is_admin(user_id):
        cooldown_key = f"cooldown:reftop:{user_id}"
        ok = await redis_client.set(cooldown_key, "1", nx=True, ex=60)
        if not ok:
            ttl = await redis_client.ttl(cooldown_key)
            await callback.message.answer(f"⏳ Топ можно глянуть через {ttl} сек.")
            return

    top = await get_top_referrals(10)
    if not top:
        text = "👥 Топ по рефералам\n\nПока пусто — никто никого не пригласил."
    else:
        text = "👥 Топ по рефералам:\n\n"
        for i, (uid, count) in enumerate(top, 1):
            name = await get_user_name(uid) or "без ника"
            text += f"{i}. {name} — {count} реф.\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="ref_back_to_info")],
    ])

    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "ref_back")
async def handle_ref_back(callback: CallbackQuery):
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await send_main_menu(callback, callback.from_user.id)

@router.callback_query(F.data == "ref_back_to_info")
async def handle_ref_back_to_info(callback: CallbackQuery):
    user_id = callback.from_user.id
    await callback.answer()
    referral_count = await get_referral_count(user_id)
    referral_earnings = await get_referral_earnings(user_id)
    bot_info = await callback.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"
    text = (
        f"🔗 Реферальная система\n\n"
        f"Твоя ссылка:\n`{ref_link}`\n\n"
        f"👥 Приглашено: {referral_count} чел.\n"
        f"💰 Заработано с рефералов: {referral_earnings:,} ₽\n\n"
        f"За каждого приглашённого — {REFERRAL_REWARD:,} ₽\n"
        f"Новичку за регистрацию по ссылке — {REFERRAL_NEWBIE_BONUS:,} ₽"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Топ по рефералам", callback_data="ref_top")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="ref_back")],
    ])
    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    except TelegramBadRequest:
        await callback.message.answer(text, parse_mode="Markdown", reply_markup=kb)

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
    await message.answer(
        f"💰 Твой баланс: {balance:,} ₽\n"
        "Выбери уровень риска:",
        reply_markup=get_trading_mode_keyboard()
    )

@router.callback_query(F.data.startswith("trade_mode:"))
async def choose_risk(callback: CallbackQuery, state: FSMContext):
    mode = callback.data.split(":")[1]
    await state.update_data(trade_mode=mode)
    await callback.answer()

    balance = await get_balance(callback.from_user.id)
    await callback.message.edit_text(
        f"Режим: {mode}\n"
        f"💰 Баланс: {balance:,} ₽\n"
        "Введи сумму ставки (целое число больше 0):",
        reply_markup=get_trading_result_keyboard2()
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

@router.callback_query(TradingForm.waiting_for_direction, F.data.in_({"trade_up", "trade_down"}))
async def handle_trade_direction(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    data = await state.get_data()

    mode = data.get("trade_mode")
    amount = data.get("amount")
    direction = "up" if callback.data == "trade_up" else "down"

    if not mode or not amount:
        await callback.answer("❌ Сессия истекла. Начни трейдинг заново.", show_alert=True)
        await state.clear()
        return

    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)

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
            f"Направление: {'📈 Вверх' if direction == 'up' else '📉 Вниз'}\n"
            f"Ставка: {amount:,} ₽\n"
            f"Чистыми: +{profit:,} ₽ (x{multiplier})"
        )
        await log_trade(user_id, mode, amount, profit, True)
    else:
        await deduct_balance(user_id, amount)
        result_text = (
            f"💥 Мимо...\n"
            f"Режим: {mode}\n"
            f"Направление: {'📈 Вверх' if direction == 'up' else '📉 Вниз'}\n"
            f"Ставка: {amount:,} ₽\n"
            f"Ставка сгорела."
        )
        await log_trade(user_id, mode, amount, -amount, False)

    _, new_level, leveled_up = await add_xp(user_id, XP_PER_TRADE)
    await msg.edit_text(result_text, reply_markup=get_trading_result_keyboard())
    if leveled_up:
        await notify_level_up(user_id, new_level)

@router.callback_query(F.data == "trade_continue")
async def process_trade_continue(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    user_id = callback.from_user.id
    balance = await get_balance(user_id)

    if balance < TRADING_MIN_BALANCE:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await callback.message.answer(
            f"❌ Не хватает денег для трейдинга (нужно {TRADING_MIN_BALANCE:,} ₽).",
            reply_markup=get_work_keyboard()
        )
        await state.clear()
        return

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    await callback.message.answer(
        f"💰 Твой баланс: {balance:,} ₽\n"
        "Выбери уровень риска:",
        reply_markup=get_trading_mode_keyboard()
    )
    await state.clear()

@router.callback_query(F.data == "trade_exit")
async def process_trade_exit(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await state.clear()
    await callback.message.answer("🚪 Вышел из трейдинга.", reply_markup=get_work_keyboard())

@router.callback_query(F.data == "trade_cancel")
async def process_trade_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await state.clear()
    await callback.message.answer("❌ Ставка отменена.", reply_markup=get_work_keyboard())

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
        tutorial_step = await redis_client.hget(f"user:{user_id}", "tutorial_step")
        tutorial_message = ""
        if tutorial_step == "math":
            await redis_client.hset(f"user:{user_id}", mapping={"tutorial_step": "done", "tutorial_done": "1"})
            tutorial_message = "\n\n🎓 Обучение завершено! Ты освоил шахту и математику. Дальше можешь изучать остальные разделы бота."

        result_text = f"✅ Точно! +{MATH_REWARD:,} ₽!\nБаланс: {new_balance:,} ₽" + tutorial_message
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

@router.message(F.text == "🏪 Бизнесы")
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
        ok = await deduct_balance(user_id, biz_def["price"])
        if not ok:
            await callback.answer("Не хватает денег!", show_alert=True)
            return
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
            ok = await deduct_balance(user_id, cost)
            if not ok:
                await callback.answer("Не хватает денег!", show_alert=True)
                return
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
        ok = await deduct_balance(user_id, cost)
        if not ok:
            await message.answer("Не хватает денег! Попробуй меньше:")
            return
    else:
        biz_balance = biz.get("balance", 0)
        if biz_balance < cost:
            await message.answer(f"На счёте бизнеса не хватает {cost - biz_balance:,} ₽. Там: {biz_balance:,} ₽\nВведи меньше:")
            return
        biz["balance"] = biz_balance - cost

    biz["raw_stock"] = stock + amount
    biz.pop("empty_since", None)
    biz.pop("empty_notified", None)
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
    text = "🎰 добро пожаловать в казино 'лохотрон'\n\nЗа какой стол хочешь сесть?"
    try:
        photo = FSInputFile("images/casino.png")
        await message.answer_photo(photo=photo, caption=text, reply_markup=get_casino_keyboard())
    except FileNotFoundError:
        logger.warning("Файл images/casino.png не найден.")
        await message.answer(text, reply_markup=get_casino_keyboard())


@router.callback_query(F.data == "casino_menu")
async def casino_menu(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    await callback.message.answer(
        "🎰 Казино\n\nза какой стол хочешь сесть?",
        reply_markup=get_casino_keyboard()
    )

@router.message(F.text == "🎡 Рулетка")
async def casino_roulette(message: Message, state: FSMContext):
    await state.clear()

    photo = FSInputFile("images/roulette.png")

    await message.answer_photo(
        photo=photo,
        reply_markup=ReplyKeyboardRemove()
    )

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

    text = (
        f"🎡 Рулетка\n\n"
        f"🎯 На что ставишь?"
    )

    try:
        photo = FSInputFile("images/roulette_table.png")
        await message.answer_photo(photo=photo, caption=text, reply_markup=get_roulette_bet_keyboard(amount))
    except FileNotFoundError:
        logger.warning("Файл images/roulette_table.png не найден.")
        await message.answer(text, reply_markup=get_roulette_bet_keyboard(amount))


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

    ok = await deduct_balance(user_id, amount)
    if not ok:
        await callback.answer("Не хватает денег на эту ставку!", show_alert=True)
        return

    await callback.answer()

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
    won, payout_mult = roulette_bet_result(bet, number)

    if won and random.random() < ROULETTE_HOUSE_RIG:
        losing_numbers = [n for n in range(37) if not roulette_bet_result(bet, n)[0]]
        number = random.choice(losing_numbers)

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
            f"🎉 Пополнение: +{amount:,} ₽\n"
            f"💰 Баланс: <b>{new_balance:,} ₽</b>"
        )
    else:
        new_balance = await get_balance(user_id)

        result_text = (
            f"🎡 <b>СТОП!</b>\n\n"
            f"Выпало: {color} <b>{number}</b>\n"
            f"Твоя ставка: <b>{roulette_bet_name(bet)}</b>\n\n"
            f"❌ <b>ПРОИГРЫШ!</b>\n"
            f"💸 Списание: -{amount:,} ₽\n"
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

# ============================================================
# ДУЭЛИ
# ============================================================

DICE_EMOJIS = ["⚀", "⚁", "⚂", "⚃", "⚄", "⚅"]


@router.message(F.text == "🥊 Дуэли")
async def show_duel_menu(message: Message, state: FSMContext):
    await state.clear()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 В меню", callback_data="main_menu")]
    ])

    await message.answer(
        "🥊 Дуэли\n\n"
        "Напиши ник и сумму, кому хочешь кинуть дуэль.\n"
        "Формат: `ник сумма`\n",
        parse_mode="Markdown",
        reply_markup=kb
    )
    await state.set_state(DuelForm.waiting_for_target)


@router.message(DuelForm.waiting_for_target)
async def process_duel_challenge(message: Message, state: FSMContext):
    user_id = message.from_user.id
    text = message.text.strip()

    # --- Проверка кулдауна ---
    can_duel, remaining = await check_duel_cooldown(user_id)
    if not can_duel:
        await message.answer(f"⏳ Дуэль можно кинуть через {remaining} сек.")
        return

    parts = text.rsplit(maxsplit=1)
    if len(parts) != 2:
        await message.answer(
            "❌ Неверный формат. Пример: `Alex123 10000`\n"
            "Или нажми «🔙 Назад» для выхода.",
            parse_mode="Markdown"
        )
        return

    nick_str, amount_str = parts
    try:
        amount = int(amount_str)
    except ValueError:
        await message.answer("❌ Сумма должна быть числом. Пример: `Alex123 10000`", parse_mode="Markdown")
        return

    if amount <= 0:
        await message.answer("❌ Сумма должна быть больше 0.")
        return

    balance = await get_balance(user_id)
    if balance < amount:
        await message.answer(f"❌ Не хватает денег. Баланс: {balance:,} ₽")
        return

    target_id = await get_user_id_by_name_direct(nick_str)
    if not target_id:
        target_id = await get_user_id_by_username(nick_str)

    if not target_id:
        await message.answer(f"❌ Игрок «{nick_str}» не найден.")
        return

    if target_id == user_id:
        await message.answer("❌ Нельзя вызвать самого себя на дуэль!")
        return

    target_balance = await get_balance(target_id)
    if target_balance < amount:
        target_name = await get_user_name(target_id) or "Игрок"
        await message.answer(f"❌ У {target_name} недостаточно денег для этой ставки.")
        return

    duel_id = await create_duel(user_id, target_id, amount)
    challenger_name = await get_user_name(user_id) or "Игрок"
    target_name = await get_user_name(target_id) or "Игрок"

    await message.answer(
        f"🥊 Ты вызвал {target_name} на дуэль на {amount:,} ₽.\n"
        f"Ждём ответ..."
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Принять", callback_data=f"duel_accept:{duel_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"duel_decline:{duel_id}"),
        ]
    ])

    try:
        await bot.send_message(
            target_id,
            f"🥊 {challenger_name} вызывает тебя на дуэль на {amount:,} ₽.\n"
            f"Принять?",
            reply_markup=kb
        )
    except Exception:
        await message.answer("❌ Не удалось отправить вызов. Возможно, игрок заблокировал бота.")
        await redis_client.delete(f"duel:{duel_id}")

    await state.clear()


@router.callback_query(F.data.startswith("duel_accept:"))
async def duel_accept(callback: CallbackQuery, state: FSMContext):
    duel_id = callback.data.split(":", 1)[1]
    duel = await get_duel(duel_id)

    if not duel:
        await callback.answer("⏰ Дуэль истекла.", show_alert=True)
        return

    if callback.from_user.id != duel["target_id"]:
        await callback.answer("Это не твой вызов!", show_alert=True)
        return

    if duel["status"] != "pending":
        await callback.answer("Дуэль уже обработана.", show_alert=True)
        return

    await callback.answer()

 # --- Проверка кулдауна у принимающего ---
    can_duel, remaining = await check_duel_cooldown(callback.from_user.id)
    if not can_duel:
        await callback.answer(f"⏳ Дуэль можно принять через {remaining} сек.", show_alert=True)
        return
    
    ch_balance = await get_balance(duel["challenger_id"])
    tg_balance = await get_balance(duel["target_id"])

    if ch_balance < duel["amount"]:
        await callback.message.edit_text("❌ У вызывающего недостаточно денег. Дуэль отменена.")
        await update_duel_status(duel_id, "cancelled")
        try:
            await bot.send_message(duel["challenger_id"], "❌ У тебя недостаточно денег на дуэль. Вызов отменён.")
        except Exception:
            pass
        return

    if tg_balance < duel["amount"]:
        await callback.message.edit_text("❌ У тебя недостаточно денег. Дуэль отменена.")
        await update_duel_status(duel_id, "cancelled")
        return

    await update_duel_status(duel_id, "active")

    ok_ch = await deduct_balance(duel["challenger_id"], duel["amount"])
    ok_tg = await deduct_balance(duel["target_id"], duel["amount"])

    if not ok_ch:
        # Возвращаем деньги второму, если первому не хватило
        if ok_tg:
            await add_to_balance(duel["target_id"], duel["amount"])
        await callback.message.edit_text("❌ У вызывающего недостаточно денег. Дуэль отменена.")
        await update_duel_status(duel_id, "cancelled")
        try:
            await bot.send_message(duel["challenger_id"], "❌ У тебя недостаточно денег на дуэль. Вызов отменён.")
        except Exception:
            pass
        return

    if not ok_tg:
        # Первому уже списали — возвращаем
        await add_to_balance(duel["challenger_id"], duel["amount"])
        await callback.message.edit_text("❌ У тебя недостаточно денег. Дуэль отменена.")
        await update_duel_status(duel_id, "cancelled")
        return

    ch_name = await get_user_name(duel["challenger_id"]) or "Игрок"
    tg_name = await get_user_name(duel["target_id"]) or "Игрок"

    start_text = (
        f"🥊 Дуэль: {ch_name} vs {tg_name}\n"
        f"💰 Ставка: {duel['amount']:,} ₽\n\n"
        f"🎲 Бросаем кости..."
    )

    try:
        await callback.message.edit_text(start_text)
    except TelegramBadRequest:
        await callback.message.answer(start_text)

    try:
        await bot.send_message(duel["challenger_id"], start_text)
    except Exception:
        pass

    # Отправляем кубик обоим игрокам
    ch_msg = None
    tg_msg = None

    try:
        ch_msg = await bot.send_dice(duel["challenger_id"], emoji="🎲")
    except Exception:
        pass

    try:
        tg_msg = await bot.send_dice(duel["target_id"], emoji="🎲")
    except Exception:
        pass

    # Ждём завершение анимации кубика (~4 сек)
    await asyncio.sleep(4)

    ch_value = ch_msg.dice.value if ch_msg and ch_msg.dice else 0
    tg_value = tg_msg.dice.value if tg_msg and tg_msg.dice else 0

    # Если кому-то кубик не отправился — бросаем фолбэк-рандом
    if ch_value == 0:
        ch_value = random.randint(1, 6)
    if tg_value == 0:
        tg_value = random.randint(1, 6)

    if ch_value > tg_value:
        await add_to_balance(duel["challenger_id"], duel["amount"] * 2)
        result_text = (
            f"🥊 Дуэль: {ch_name} vs {tg_name}\n"
            f"💰 Ставка: {duel['amount']:,} ₽\n\n"
            f"🎲 {ch_name}: {ch_value}\n"
            f"🎲 {tg_name}: {tg_value}\n\n"
            f"🎉 Победил {ch_name}!\n"
            f"💰 Выигрыш: +{duel['amount']:,} ₽"
        )
    elif tg_value > ch_value:
        await add_to_balance(duel["target_id"], duel["amount"] * 2)
        result_text = (
            f"🥊 Дуэль: {ch_name} vs {tg_name}\n"
            f"💰 Ставка: {duel['amount']:,} ₽\n\n"
            f"🎲 {ch_name}: {ch_value}\n"
            f"🎲 {tg_name}: {tg_value}\n\n"
            f"🎉 Победил {tg_name}!\n"
            f"💰 Выигрыш: +{duel['amount']:,} ₽"
        )
    else:
        await add_to_balance(duel["challenger_id"], duel["amount"])
        await add_to_balance(duel["target_id"], duel["amount"])
        result_text = (
            f"🥊 Дуэль: {ch_name} vs {tg_name}\n"
            f"💰 Ставка: {duel['amount']:,} ₽\n\n"
            f"🎲 {ch_name}: {ch_value}\n"
            f"🎲 {tg_name}: {tg_value}\n\n"
            f"🤝 Ничья! Деньги возвращены."
        )

# --- XP и кулдаун для обоих ---
    _, ch_new_level, ch_up = await add_xp(duel["challenger_id"], XP_PER_DUEL)
    _, tg_new_level, tg_up = await add_xp(duel["target_id"], XP_PER_DUEL)

    await set_duel_cooldown(duel["challenger_id"])
    await set_duel_cooldown(duel["target_id"])

    await update_duel_status(duel_id, "finished")

    # ... отправка result_text обоим игрокам ...

    try:
        await callback.message.answer(result_text)
    except TelegramBadRequest:
        pass

    try:
        await bot.send_message(duel["challenger_id"], result_text)
    except Exception:
        pass

    if ch_up:
        await notify_level_up(duel["challenger_id"], ch_new_level)
    if tg_up:
        await notify_level_up(duel["target_id"], tg_new_level)


@router.callback_query(F.data.startswith("duel_decline:"))
async def duel_decline(callback: CallbackQuery, state: FSMContext):
    duel_id = callback.data.split(":", 1)[1]
    duel = await get_duel(duel_id)

    if not duel:
        await callback.answer("⏰ Дуэль истекла.", show_alert=True)
        return

    if callback.from_user.id != duel["target_id"]:
        await callback.answer("Это не твой вызов!", show_alert=True)
        return

    if duel["status"] != "pending":
        await callback.answer("Дуэль уже обработана.", show_alert=True)
        return

    await callback.answer()
    await update_duel_status(duel_id, "declined")

    ch_name = await get_user_name(duel["challenger_id"]) or "Игрок"
    tg_name = await get_user_name(duel["target_id"]) or "Игрок"

    try:
        await callback.message.edit_text(f"❌ {tg_name} отклонил дуэль от {ch_name}.")
    except TelegramBadRequest:
        await callback.message.answer(f"❌ {tg_name} отклонил дуэль от {ch_name}.")

    try:
        await bot.send_message(duel["challenger_id"], f"❌ {tg_name} отклонил твою дуэль.")
    except Exception:
        pass

# --- Уведомления о простое бизнеса из-за пустого склада ---
EMPTY_STOCK_NOTIFY_AFTER = 60 * 60
EMPTY_STOCK_CHECK_INTERVAL = 5 * 60

async def monitor_empty_businesses():
    while True:
        try:
            async for key in redis_client.scan_iter(match="user:*", count=100):
                if not key.startswith("user:") or key.count(":") != 1:
                    continue
                try:
                    user_id = int(key.split(":", 1)[1])
                except ValueError:
                    continue

                biz = await get_biz(user_id)
                if not biz:
                    continue

                # Сначала начисляем доход до момента остановки.
                await settle_and_save_biz(user_id, biz)
                stock = int(biz.get("raw_stock", 0))
                if stock > 0:
                    biz.pop("empty_since", None)
                    biz.pop("empty_notified", None)
                    await save_biz(user_id, biz)
                    continue

                now = time.time()
                empty_since = biz.get("empty_since")
                if empty_since is None:
                    # Для старых сохранений начинаем отсчёт с первого обнаружения нулевого склада.
                    biz["empty_since"] = now
                    await save_biz(user_id, biz)
                    continue

                if (now - float(empty_since) >= EMPTY_STOCK_NOTIFY_AFTER
                        and not biz.get("empty_notified")):
                    await bot.send_message(
                        user_id,
                        f"⚠️ Твой «{biz.get('name', 'бизнес')}» простаивает! "
                        "Затарись сырьём, чтобы не терять прибыль!"
                    )
                    biz["empty_notified"] = True
                    await save_biz(user_id, biz)

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка мониторинга пустого склада")

        await asyncio.sleep(EMPTY_STOCK_CHECK_INTERVAL)


# --- Универсальный хендлер ---
@router.message(F.text)
async def handle_unknown_text(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Используй кнопки 😡")

@dp.errors()
async def global_error_handler(event, exception):
    logger.error("Необработанная ошибка: %s", exception, exc_info=(type(exception), exception, exception.__traceback__))
    update = event.update
    callback = update.callback_query
    message = update.message or update.edited_message
    try:
        if callback:
            await callback.answer("Что-то пошло не так, попробуй ещё раз", show_alert=True)
            await send_main_menu(callback, callback.from_user.id)
        elif message and message.from_user:
            await message.answer("Что-то пошло не так, попробуй ещё раз")
            await send_main_menu(message, message.from_user.id)
    except Exception:
        logger.exception("Не удалось показать сообщение об ошибке")
    return True


dp.include_router(router)

# ============================================================
# ТОЧКА ВХОДА
# ============================================================

LOCK_FILE = "bot.lock"

async def main():
    # --- Lock-файл (защита от двойного запуска) ---
    if os.path.exists(LOCK_FILE):
        logger.warning("Lock-файл существует. Возможно, бот уже запущен.")
        with open(LOCK_FILE, "r") as f:
            old_pid = f.read().strip()
        logger.warning(f"PID предыдущего процесса: {old_pid}")

    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))

    logger.info("Запуск бота...")

    # --- Redis ---
    try:
        await init_redis()
    except Exception as e:
        logger.error(f"Redis: ошибка подключения — {e}")
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
        return

    # --- Загрузка Lua-скрипта для атомарного списания ---
    try:
        await _ensure_deduct_script()
        logger.info("Lua-скрипт deduct_balance загружен.")
    except Exception as e:
        logger.error(f"Не удалось загрузить Lua-скрипт: {e}")
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
        return

    # --- Фоновый мониторинг простаивающих бизнесов ---
    monitor_task = asyncio.create_task(monitor_empty_businesses())

    try:
        logger.info("Бот запущен. Polling started.")
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass
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