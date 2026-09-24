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
_case_cooldown_cache: dict[int, float] = {}
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


def parse_amount(value: str) -> int:
    """Принимает 1000000, 1 000 000, 100к, 1кк; также 1.5к/1,5к."""
    raw = value.strip().lower().replace("\u00a0", "").replace(" ", "").replace("_", "")
    raw = raw.replace(",", ".")
    multiplier = 1
    if raw.endswith("кк"):
        multiplier, raw = 1_000_000, raw[:-2]
    elif raw.endswith("к"):
        multiplier, raw = 1_000, raw[:-1]
    if not re.fullmatch(r"\d+(?:\.\d+)?", raw):
        raise ValueError("invalid amount")
    result = float(raw) * multiplier
    if not result.is_integer():
        raise ValueError("amount must be integer")
    return int(result)

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
    waiting_for_level = State()

class DuelForm(StatesGroup):
    waiting_for_target = State()

class TransferForm(StatesGroup):
    waiting_for_target = State()
    waiting_for_note = State()
    waiting_for_amount = State()

# ============================================================
# КОНСТАНТЫ HELP
# ============================================================
HELP_TEXT_MAIN = (
    "бот коммерсант - тут можно зарабатывать деньги, торговать, делать бизнес(и многое другое)\n\n"
    "жми кнопки ниже, расскажу про каждый раздел."
)

HELP_TEXT_TRADING = (
    "трейдинг — это торговля на рынке криптовалюты с разной степенью риска.\n\n"
    "как это работает:\n"
    "1. выбираешь риск: низкий(высокий шанс победы, но выигрыш небольшой), средний(шанс 50 на 50, выигрыш х2), высокий(маленький шанс, но выигрыш х5 от ставки!!).\n"
    "2. вводи сумму ставки\n"
    "3. бот проверяет рынок и показывает результат.\n\n"
)

HELP_TEXT_MINE = (
    "шахта — самый простой способ заработать первые деньги.\n"
    "можно прокачивать кирки, чем лучше кирка тем лучше руды ты можешь добывать"
)

HELP_TEXT_MATH = (
    "математика — решил пример = получил деньги.\n\n"
)

HELP_TEXT_BUSINESS = (
    "бизнесы — это пассивный доход: ты покупаешь бизнес, и он приносит деньги каждую минуту.\n"
    "бизнесу нужно сырьё. Если оно заканчивается, бизнес перестаёт работать и доход останавливается.\nуровень бизнеса можно повышать, чем выше уровень тем выше доход."
)

HELP_TEXT_TOP = ("топ — лучшие пользователи в боте.\n\nигроки которые занимают топ 1-5 каждые 3 дня получают награды")

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
    "рад снова с тобой увидеться, {name}.",
    "связь, {name}.",
    "я тебя могну, {name}.",
    "ку, {name}.",
    "салам, {name}.",
    "сап, {name}."
]
TOP_PROMPT_VARIANTS = [
    "🏆 выбери рейтинг:\nнапоминаю что каждые 3 дня люди в топе получают вознаграждения",
    "📊 посмотри топ игроков:\nнапоминаю что каждые 3 дня люди в топе получают вознаграждения!!",
    "🔥 рейтинг ждёт твоего выбора:",
    "⚡ кто сейчас в топе? Выбери категорию:\nза нахождение в топе игроки получают вознаграждения",
    "🎯 какой рейтинг хочешь увидеть?",
    "🌟 топ 5 каждого топа получают денежное вознаграждение",
]

# ============================================================
# КОНСТАНТЫ ЕЖЕДНЕВНОГО БОНУСА
# ============================================================
DAILY_BONUS_BASE = 10000          # базовая награда
DAILY_BONUS_STREAK_MULT = 1.00    # +100% за каждый день стрика
DAILY_BONUS_MAX_STREAK = 100     # потолок множителя
DAILY_BONUS_RANDOM_MIN = 1000    # случайная прибавка — минимум
DAILY_BONUS_RANDOM_MAX = 5000    # случайная прибавка — максимум
DAILY_BONUS_COOLDOWN = 86400     # 24 часа

# ============================================================
# КОНСТАНТЫ НАГРАД ЗА ТОП (баланс, рефералы, уровень)
# ============================================================
TOP_REWARD_INTERVAL = 3 * 86400   # раз в 3 дня
TOP_REWARD_CHECK_INTERVAL = 10 * 60   # как часто проверять, не пора ли награждать

BALANCE_TOP_REWARDS = {
    1: 2000000,
    2: 1000000,
    3: 500000,
    4: 100000,
    5: 100000,
}
REFERRAL_TOP_REWARDS = {
    1: 3000000,
    2: 2000000,
    3: 1000000,
    4: 500000,
    5: 500000,
}
LEVEL_TOP_REWARDS = {
    1: 500000,
    2: 300000,
    3: 150000,
    4: 70000,
    5: 70000,
}

# Конфиг для фоновой задачи: (название, ключ сортированного множества в Redis,
# таблица наград, ключ хранения времени последней выдачи, подпись для сообщения)
TOP_REWARD_CONFIGS = [
    {
        "key": "leaderboard:balance",
        "rewards": BALANCE_TOP_REWARDS,
        "last_ts_key": "top_reward:balance:last_ts",
        "label": "балансу",
        "emoji": "💰",
    },
    {
        "key": "referrals_top",
        "rewards": REFERRAL_TOP_REWARDS,
        "last_ts_key": "top_reward:referrals:last_ts",
        "label": "рефералам",
        "emoji": "👥",
    },
    {
        "key": "leaderboard:level",
        "rewards": LEVEL_TOP_REWARDS,
        "last_ts_key": "top_reward:level:last_ts",
        "label": "уровню",
        "emoji": "📈",
    },
]

# ============================================================
# КОНСТАНТЫ РЕФЕРАЛЬНОЙ СИСТЕМЫ
# ============================================================
REFERRAL_REWARD = 500000        # награда пригласившему
REFERRAL_NEWBIE_BONUS = 100000   # бонус новичку за регистрацию по ссылке

# ============================================================
# ЭКОНОМИКА: КОНСТАНТЫ
# ============================================================
MINE_COOLDOWN = 3
MATH_REWARD = 500
MATH_COOLDOWN = 10
RAW_PRICE = 1

PICKAXE_LEVELS = [
    {"name": "деревянная",  "reward": 10,   "cost": 0},
    {"name": "медная",  "reward": 50,   "cost": 100},
    {"name": "оловянная",  "reward": 150,   "cost": 1000},
    {"name": "кактусовая",  "reward": 300,   "cost": 4000},
    {"name": "каменная",    "reward": 600,  "cost": 12000},
    {"name": "железная",    "reward": 1200,  "cost": 36000},
    {"name": "свинцовая",    "reward": 2400,  "cost": 84000},
    {"name": "серебряная",    "reward": 4000,  "cost": 100000},
    {"name": "вольфрамовая",    "reward": 7000,  "cost": 200000},
    {"name": "костяная",    "reward": 10000,  "cost": 250000},
    {"name": "золотая",     "reward": 15000,  "cost": 300000},
    {"name": "карамельная",    "reward": 18000,  "cost": 300000},
    {"name": "железная",    "reward": 20000,  "cost": 350000},
    {"name": "платиновая",    "reward": 25000,  "cost": 500000},
    {"name": "алмазная",    "reward": 30000,  "cost": 700000},
    {"name": "аметистовая", "reward": 35000,  "cost": 600000},
    {"name": "кошмарная",    "reward": 40000,  "cost": 1000000},
    {"name": "смертоносная",    "reward": 45000,  "cost": 1500000},
    {"name": "незеритовая", "reward": 55000,  "cost": 2000000},
    {"name": "адамантитовая", "reward": 60000,  "cost": 3000000},
]

TRADING_MIN_BALANCE = 20000

TRADING_MODES = {
    "low":  {"multiplier": 1.2, "chance": 0.75},  # EV = -10% (казино в плюсе)
    "mid":  {"multiplier": 2.0, "chance": 0.48},  # EV = -4%
    "high": {"multiplier": 5.0, "chance": 0.18},  # EV = -10%
}

ROULETTE_HOUSE_RIG = 0.01
DUEL_TIMEOUT = 3600
DUEL_COOLDOWN = 60

XP_PER_MINE = 50
MATH_XP_REWARD = 100
XP_PER_TRADE = 100
XP_PER_DUEL = 200

# --- ЛВЛ РАЗБЛОКИРОВКИ ---
PROFILE_UNLOCK_LEVEL = 1
BONUS_UNLOCK_LEVEL = 2
MATH_UNLOCK_LEVEL = 3
DUEL_UNLOCK_LEVEL = 5
CASE_UNLOCK_LEVEL = 8
TRADING_UNLOCK_LEVEL = 10
BUSINESS_UNLOCK_LEVEL = 11
CASINO_UNLOCK_LEVEL = 15

# ============================================================
# БИЗНЕСЫ: КОНСТАНТЫ
# ============================================================
BUSINESS_LIST = [
    {
        "name": "Ларёк «Всё по 67»",
        "price": 100_000,
        "income_per_min": 700,
        "raw_consumption_per_min": 280,
        "raw_capacity": 268_800,
    },
    {
        "name": "Шаурмечка",
        "price": 450_000,
        "income_per_min": 2_500,
        "raw_consumption_per_min": 1_000,
        "raw_capacity": 960_000,
    },
    {
        "name": "Магазин «Недорого, но сердито»",
        "price": 2_000_000,
        "income_per_min": 10_000,
        "raw_consumption_per_min": 4_000,
        "raw_capacity": 3_840_000,
    },
    {
        "name": "Ферма",
        "price": 6_000_000,
        "income_per_min": 28_000,
        "raw_consumption_per_min": 11_200,
        "raw_capacity": 10_752_000,
    },
    {
        "name": "Букмекерская контора",
        "price": 7_000_000,
        "income_per_min": 30_000,
        "raw_consumption_per_min": 12_000,
        "raw_capacity": 11_520_000,
    },
    {
        "name": "Заправка",
        "price": 15_000_000,
        "income_per_min": 60_000,
        "raw_consumption_per_min": 24_000,
        "raw_capacity": 23_040_000,
    },
    {
        "name": "Гипермаркет",
        "price": 30_000_000,
        "income_per_min": 104_000,
        "raw_consumption_per_min": 41_600,
        "raw_capacity": 39_936_000,
    },
    {
        "name": "Криптобиржа",
        "price": 50_000_000,
        "income_per_min": 154_000,
        "raw_consumption_per_min": 61_600,
        "raw_capacity": 59_136_000,
    },
    {
        "name": "Аэропорт",
        "price": 500_000_000,
        "income_per_min": 1_390_000,
        "raw_consumption_per_min": 556_000,
        "raw_capacity": 533_760_000,
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

    if biz.get("broken"):
        biz["last_collected"] = now
        return biz

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
        f"💳 На счету бизнеса: {biz_balance:,} ₽\n\n"
    )
    if biz.get("broken"):
        text += f"🛠 бизнес сломался! Починка стоит {int(biz.get('price', 0) * BUSINESS_REPAIR_COST_RATE):,} ₽."
    elif biz.get("raw_stock", 0) <= 0:
        text += "⚠️ бизнес встал — сырья ноль!\nжми «📦 Склад», затарься."
    else:
        text += "✅ бизнес работает!"

    rows = [
        [InlineKeyboardButton(text="📦 Склад", callback_data="biz_wh"), InlineKeyboardButton(text="🚀 Прокачать", callback_data="biz_up")],
        [InlineKeyboardButton(text="💰 Снять деньги", callback_data="biz_collect")],
    ]
    if biz.get("broken"):
        repair_cost = int(biz.get("price", 0) * BUSINESS_REPAIR_COST_RATE)
        rows.append([InlineKeyboardButton(text=f"🛠 Починить за {repair_cost:,} ₽", callback_data="biz_repair")])
    rows.extend([
        [InlineKeyboardButton(text="💸 Продать", callback_data="biz_sell")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="biz_refresh"), InlineKeyboardButton(text="🔙 Выйти", callback_data="biz_exit")],
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    return text, kb


def biz_no_biz_view():
    text = "🏪 Бизнесы\n\nу тебя нет бизнеса.\nжми кнопку ниже, выбери себе точку"
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
        f"📦 Расход: {biz.get('raw_consumption_per_min', 0):,}/мин\n"
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
        text = f"🚀 «{biz['name']}»\n\nуровень: {level}/3 — твой бизнес полностью вкачен!"
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
        f"💸 Продажа «{biz['name']}»\n\n"
        f"на руки получишь: {sell_price:,} ₽\n"
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
        # Читаем актуальное значение напрямую: get_balance() мог вернуть устаревший кэш.
        new_balance_raw = await redis_client.hget(f"user:{user_id}", "balance")
        new_balance = int(float(new_balance_raw or 0))
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
    """Регистрирует реферала; выплаты начисляются после достижения 5 уровня."""
    if new_user_id == referrer_id or await get_referrer(new_user_id):
        return None
    referrer_name = await get_user_name(referrer_id)
    if not referrer_name:
        return None

    await set_referrer(new_user_id, referrer_id)
    new_count = await increment_referral_count(referrer_id)
    await redis_client.hset(
        f"user:{new_user_id}", mapping={"referral_reward_pending": "1"}
    )
    try:
        await bot.send_message(
            referrer_id,
            f"🎉 По твоей ссылке зарегистрировался {referrer_name}!\n"
            f"🎁 Награда будет начислена, когда новичок достигнет 5 уровня.\n"
            f"👥 Всего рефералов: {new_count}"
        )
    except Exception:
        pass
    return new_count, referrer_name


async def pay_referral_reward_if_eligible(new_user_id: int, level: int) -> bool:
    """Выдаёт выплаты за реферала ровно один раз при достижении 5 уровня."""
    if level < 5:
        return False
    user_key = f"user:{new_user_id}"
    pending = await redis_client.hget(user_key, "referral_reward_pending")
    if pending != "1":
        return False
    referrer_id = await get_referrer(new_user_id)
    if not referrer_id:
        return False

    claimed = await redis_client.hsetnx(user_key, "referral_reward_pending", "0")
    if not claimed:
        return False

    await add_to_balance(referrer_id, REFERRAL_REWARD)
    await add_to_referral_earnings(referrer_id, REFERRAL_REWARD)
    await add_to_balance(new_user_id, REFERRAL_NEWBIE_BONUS)
    try:
        await bot.send_message(
            referrer_id,
            f"🎉 Твой реферал достиг 5 уровня!\n"
            f"💰 Награда: +<b>{REFERRAL_REWARD:,} ₽</b>",
            parse_mode="HTML",
        )
        await bot.send_message(
            new_user_id,
            f"🎁 Ты достиг 5 уровня! Бонус за приглашение: "
            f"<b>+{REFERRAL_NEWBIE_BONUS:,} ₽</b>",
            parse_mode="HTML",
        )
    except Exception:
        pass
    return True

async def get_top_referrals(limit: int = 10) -> list[tuple[int, int]]:
    raw = await redis_client.zrevrange("referrals_top", 0, limit - 1, withscores=True)
    return [(int(uid), int(score)) for uid, score in raw]

async def get_top_display_name(user_id: int) -> str:
    """Отображаемое имя игрока для топов: ник + @username (если он есть)."""
    name = await get_user_name(user_id) or "без ника"
    username = await get_username(user_id)
    if username and username != "без_username":
        return f"{name} (@{username})"
    return name

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
            f"🔧 <b>прокачка кирки</b>\n\n"
            f"твоя кирка: {current['name']} (макс. уровень!)\n"
            f"💰 доход: {current['reward']:,} ₽ за клик\n\n"
            f"поздравляю, ты достиг максимальной кирки ⛏"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="pickaxe_back")]
        ])
        return text, kb

    nxt = PICKAXE_LEVELS[current_level + 1]
    can_afford = balance >= nxt["cost"]

    text = (
        f"🔧 <b>прокачка кирки</b>\n\n"
        f"<b>текущая:</b> {current['name']} — {current['reward']:,} ₽/клик\n"
        f"<b>следующая:</b> {nxt['name']} — {nxt['reward']:,} ₽/клик\n"
        f"💸 цена: {nxt['cost']:,} ₽\n"
        f"💰 баланс: {balance:,} ₽"
    )

    if can_afford:
        btn = InlineKeyboardButton(
            text=f"✅ прокачать за {nxt['cost']:,} ₽",
            callback_data="pickaxe_upgrade"
        )
    else:
        btn = InlineKeyboardButton(
            text=f"❌ не хватает {nxt['cost'] - balance:,} ₽",
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

    # Выплаты за реферала — только при переходе на 5 уровень или выше.
    if old_level < 5 <= new_level:
        await pay_referral_reward_if_eligible(user_id, new_level)

    return new_xp, new_level, leveled_up

async def notify_level_up(user_id: int, new_level: int):
    """Отправляет сообщение о новом уровне."""
    text = f"🎉 <b>LEVEL UP!</b> Ты достиг {new_level} уровня!"

    unlocked = [name for name, lvl in UNLOCK_LEVELS.items() if lvl == new_level]
    if unlocked:
        text += "\n\n<b>теперь доступно:</b>\n" + "\n".join(f"• {name}" for name in unlocked)

    try:
        await bot.send_message(user_id, text, parse_mode="HTML")
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
        name = await get_top_display_name(uid)
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

# --- Проверка доступа по уровню ---
UNLOCK_LEVELS = {
    "📋 Профиль": PROFILE_UNLOCK_LEVEL,
    "🧮 Математика": MATH_UNLOCK_LEVEL,
    "🥊 Дуэли": DUEL_UNLOCK_LEVEL,
    "📈 Трейдинг": TRADING_UNLOCK_LEVEL,
    "🏪 Бизнесы": BUSINESS_UNLOCK_LEVEL,
    "🎰 Казино": CASINO_UNLOCK_LEVEL,
    "🎁 Ежедневный бонус": BONUS_UNLOCK_LEVEL,
    "📦 Кейсы": CASE_UNLOCK_LEVEL,
}

async def check_level_access(message: Message, user_id: int, required_level: int) -> bool:
    stats = await get_user_stats(user_id)
    level = stats["level"]
    if level < required_level:
        await message.answer(
            f"🔒 доступ откроется с <b>{required_level}</b> уровня.\n"
            f"твой уровень: <b>{level}</b>.",
            parse_mode="HTML"
        )
        return False
    return True

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
    greeting_text = greeting_template.format(name=f"{display_name}")
    
    # Формируем полный текст сообщения
    text = f"{greeting_text}\n<b>твой баланс:</b> {balance:,} ₽\nвыбирай куда направишься"
    # -------------------------------------

    try:
        photo = FSInputFile("images/glmenu.png")
        if isinstance(target, CallbackQuery):
            await target.message.answer_photo(photo=photo, caption=text, parse_mode="HTML", reply_markup=get_main_keyboard())
        else:
            await target.answer_photo(photo=photo, caption=text, parse_mode="HTML", reply_markup=get_main_keyboard())
    except FileNotFoundError:
        logger.warning("Файл images/glmenu.png не найден.")
        if isinstance(target, CallbackQuery):
            await target.message.answer(text, parse_mode="HTML", reply_markup=get_main_keyboard())
        else:
            await target.answer(text, parse_mode="HTML", reply_markup=get_main_keyboard())

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
        [InlineKeyboardButton(text="⭐ Выдать уровень", callback_data=f"admin_level:{player_id}")],
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
                parse_mode="HTML",
            )
            return
        except TelegramBadRequest:
            pass
    await bot_obj.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Доступ закрыт, ты не админ.")
        return
    await state.clear()
    sent = await message.answer(
        "🛡 <b>Админ-панель</b>\n\nВыбирай действие:",
        parse_mode="HTML",
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
        "🛡 <b>Админ-панель</b>\n\nВыбирай действие:",
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
        "🔍 Введи ник игрока или @username:\nНапример: <b>Alex123</b> или <b>@someuser</b>",
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
        f"👤 <b>Игрок #{player_id}</b>\n\n"
        f"⭐ Уровень: <b>{(await get_user_stats(player_id))['level']}</b>\n"
        f"📝 Ник: <b>{name}</b>\n"
        f"👤 Username: @{username}\n"
        f"💰 Баланс: <b>{balance:,} ₽</b>\n"
        f"🏪 Бизнес: {biz_text}\n"
        f"👥 Рефералов: {referral_count}"
    )
    kb = get_admin_player_keyboard(player_id)

    await state.update_data(admin_player_id=player_id)
    await _edit_or_answer(bot_obj, chat_id, msg_id, text, kb)
    await state.update_data(admin_msg_id=msg_id)


@router.callback_query(F.data.startswith("admin_level:"))
async def admin_level_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ закрыт, ты не админ.", show_alert=True)
        return
    player_id = int(callback.data.split(":")[1])
    await state.update_data(admin_player_id=player_id, admin_msg_id=callback.message.message_id)
    await state.set_state(AdminForm.waiting_for_level)
    await callback.answer()
    await _edit_or_answer(callback.bot, callback.message.chat.id, callback.message.message_id,
                          f"⭐ Введи новый уровень игрока #{player_id} (0 или больше):",
                          get_admin_back_keyboard(player_id))

@router.message(AdminForm.waiting_for_level)
async def admin_level_enter(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    player_id = data.get("admin_player_id")
    msg_id = data.get("admin_msg_id")
    try:
        level = int((message.text or "").strip())
        if level < 0 or level > 100000:
            raise ValueError
    except ValueError:
        try: await message.delete()
        except TelegramBadRequest: pass
        await _edit_or_answer(message.bot, message.chat.id, msg_id, "❌ Введи целый уровень от 0 до 100000:", get_admin_back_keyboard(player_id))
        return
    try: await message.delete()
    except TelegramBadRequest: pass
    xp = total_xp_for_level(level)
    await redis_client.hset(f"user:{player_id}:stats", mapping={"xp": str(xp), "level": str(level)})
    _stats_cache[player_id] = {"xp": xp, "level": level}
    await redis_client.zadd("leaderboard:level", {str(player_id): level})
    name = await get_user_name(player_id) or "без ника"
    await _edit_or_answer(message.bot, message.chat.id, msg_id,
                          f"✅ Уровень выдан!\n👤 {name} (#{player_id})\n⭐ Новый уровень: <b>{level}</b>",
                          get_admin_back_keyboard(player_id))
    await state.set_state(None)

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
        f"💰 Сейчас на балансе: <b>{current_balance:,} ₽</b>\n\n"
        f"Введи сумму для «{action_names[action]}»:",
    )


@router.message(AdminForm.waiting_for_amount)
async def admin_enter_amount(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    chat_id = message.chat.id

    try:
        amount = parse_amount(message.text)
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
        "set": f"Задать баланс = <b>{amount:,} ₽</b>",
        "add": f"Добавить <b>{amount:,} ₽</b> (станет {current_balance + amount:,} ₽)",
        "sub": f"Вычесть <b>{amount:,} ₽</b> (станет {current_balance - amount:,} ₽)",
    }

    text = (
        f"⚠️ <b>Подтверди действие:</b>\n\n"
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
        f"✅ <b>Готово!</b>\n\n"
        f"👤 Игрок: {name} (#{player_id})\n"
        f"💰 Новый баланс: <b>{new_balance:,} ₽</b>",
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

    text = "📊 <b>Топ игроков (админ-режим):</b>\n\n"
    for i, (uid, name, balance) in enumerate(balances[:20], 1):
        text += f"{i}. {name} (#{uid}) — <b>{balance:,} ₽</b>\n"

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
            "👥 <b>Топ по рефералам</b>\n\nПока пусто — никто никого не пригласил.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 В меню админа", callback_data="admin_main")]
            ]),
        )
        return

    text = "👥 <b>Топ по рефералам (админ-режим):</b>\n\n"
    for i, (uid, count) in enumerate(top, 1):
        name = await get_top_display_name(uid)
        text += f"{i}. {name} (#{uid}) — <b>{count}</b> реф.\n"

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
        [KeyboardButton(text="🎰 Казино"), KeyboardButton(text="📦 Кейсы"), KeyboardButton(text="🥊 Дуэли")],
        [KeyboardButton(text="🎁 Бонус"), KeyboardButton(text="🔗 Реф"), KeyboardButton(text="🏆 Топ")],
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
        [InlineKeyboardButton(text="🏆 Топ", callback_data="help_top")],
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
            InlineKeyboardButton(text="🟢 Низкий (x1.2)", callback_data="trade_mode:low"),
            InlineKeyboardButton(text="🟡 Средний (x2.0)", callback_data="trade_mode:mid"),
        ],
        [
            InlineKeyboardButton(text="🔴 Высокий (x5.0)", callback_data="trade_mode:high"),
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
        return "📜 <b>История сделок</b>\n\nПока сделок нет — самое время попробовать!", total_pages

    start = page * TRADE_HISTORY_PAGE_SIZE
    chunk = entries[start:start + TRADE_HISTORY_PAGE_SIZE]

    lines = [f"📜 <b>История сделок</b> (стр. {page + 1}/{total_pages})\n"]
    for i, item in enumerate(chunk, start=start + 1):
        mode = TRADE_MODE_NAMES.get(item.get("mode"), item.get("mode", "Сделка"))
        amount = item.get("amount", 0)
        result = item.get("result", 0)
        win = item.get("win")
        stamp = time.strftime("%d.%m %H:%M", time.localtime(item.get("ts", time.time())))
        status = "✅" if win else "❌"
        lines.append(
            f"{i}. {status} {mode} | 🕒 {stamp}\n"
            f"   Ставка: <b>{amount:,} ₽</b> | Итог: <b>{result:+,.0f} ₽</b>"
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
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "trade_history_back")
async def handle_trade_history_back(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    balance = await get_balance(callback.from_user.id)
    text = f"💰 твой баланс: <b>{balance:,} ₽</b>\nвыбери уровень риска:"
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_trading_mode_keyboard())
    except TelegramBadRequest:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=get_trading_mode_keyboard())

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

@router.message(Command("start", "menu"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    username = message.from_user.username
    await save_user_info(user_id, username)
    if username:
        _username_cache[user_id] = username.lstrip("@").lower()

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
            "👋 <b>дарова!</b> напиши свой эксклюзивный ник\n"
            "можно использовать русс/англ буквы и цифры\n",
            parse_mode="HTML",
        )
        await state.set_state(NameForm.waiting_for_name)
        return

    # Возвращающийся пользователь — обработать реферал сразу
    if referrer_id and referrer_id != user_id:
        result = await process_referral(user_id, referrer_id)
        if result:
            await message.answer(
                f"🎁 тебя пригласил <b>{result[1]}</b>! "
                f"бонус за регистрацию: +<b>{REFERRAL_NEWBIE_BONUS:,} ₽</b>",
                parse_mode="HTML",
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
        f"🤖 <b>Бот жив</b>\n{redis_ok}\n⏳ В строю уже: {uptime_str}",
        parse_mode="HTML",
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
    text = "📜 <b>Твои сделки:</b>\n\n"
    wins = 0
    total_profit = 0
    for i, t in enumerate(trades[-5:], 1):
        sign = "✅" if t["win"] else "❌"
        text += f"{i}. {sign} {t['mode']} | Ставка: {t['amount']:,} ₽ | Результат: {t['result']:+,.0f} ₽\n"
        if t["win"]:
            wins += 1
        total_profit += t["result"]
    text += f"\nВсего: {len(trades)} | Побед: {wins} | Общий результат: <b>{total_profit:+,.0f} ₽</b>"
    await message.answer(text, parse_mode="HTML")

@router.message(NameForm.waiting_for_name)
async def process_name(message: Message, state: FSMContext):
    name = message.text.strip()
    user_id = message.from_user.id
    if not is_admin(user_id):
        if not is_valid_name(name):
            await message.answer(
                "❌ ник — только буквы и цифры, 3–10 символов. без пробелов и всякой херни."
            )
            return
        if await is_name_taken(name):
            await message.answer(f"❌ ник «{name}» уже занят. придумай другой:")
            return
    else:
        if not is_valid_name(name):
            await message.answer("⚠️ коммерсант: ник 3–10 символов, буквы и цифры")
            return
        existing_id = await get_user_id_by_name_direct(name)
        if existing_id and existing_id != user_id:
            await message.answer(f"⚠️ ник «{name}» уже у игрока {existing_id}. Перезапишу.")

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
                f"\n🎁 тебя пригласил <b>{result[1]}</b>! "
                f"бонус: +<b>{REFERRAL_NEWBIE_BONUS:,} ₽</b>"
            )

    # Новый игрок проходит короткое обучение; флаг сохраняется в Redis.
    tutorial_done = await redis_client.hget(f"user:{user_id}", "tutorial_done")
    if not tutorial_done:
        await redis_client.hset(f"user:{user_id}", "tutorial_step", "mine")
    await message.answer(f"👋{ref_bonus_text}", parse_mode="HTML")
    if not tutorial_done:
        await message.answer(
            "🎓 <b>быстрое введение:</b>\nнажми 'работа', затем 'шахта', дальше разберешься\nповышение уровня = разблокировка новых функций\nприглашение друзей по рефке = хороший буст",
            parse_mode="HTML",
        )
    await send_main_menu(message, user_id)

@router.message(F.text == "📋 Профиль")
async def show_profile(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    if not await check_level_access(message, user_id, PROFILE_UNLOCK_LEVEL):
        return

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
        f"📋 <b>твой профиль</b>\n\n"
        f"💰 баланс: <b>{balance:,} ₽</b>\n"
        f"📈 уровень: <b>{level}</b>\n"
        f"⚡ XP: {xp_earned:,} / {xp_needed:,}\n"
        f"📊 [{bar}] {percent}%"
    )
    # -------------------------------------------

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💸 Перевести деньги", callback_data="transfer_start")],
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
        parse_mode="HTML",
        reply_markup=kb
    )

@router.callback_query(F.data == "transfer_start")
async def transfer_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="transfer_cancel")]
    ])
    await callback.message.answer("💸 напиши @username или ник, кому хочешь перевести деньги", reply_markup=kb)
    await state.set_state(TransferForm.waiting_for_target)
    await callback.answer()

@router.callback_query(F.data == "transfer_no_note")
async def transfer_no_note(callback: CallbackQuery, state: FSMContext):
    if await state.get_state() != TransferForm.waiting_for_note:
        await callback.answer()
        return
    await state.update_data(transfer_note="")
    await state.set_state(TransferForm.waiting_for_amount)
    await callback.message.edit_text("✍️ комментарий пропущен. теперь напиши сумму перевода.\nдля отмены введи /cancel.")
    await callback.answer()

@router.callback_query(F.data == "transfer_cancel")
async def transfer_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Перевод отменён.")
    await callback.answer()

@router.message(TransferForm.waiting_for_target)
async def transfer_target_received(message: Message, state: FSMContext):
    target_str = (message.text or "").strip()
    target_id = await resolve_target(target_str)
    if not target_id:
        await message.answer("❌ получатель не найден. укажи его @username или ник.")
        return
    if target_id == message.from_user.id:
        await message.answer("❌ нельзя переводить деньги самому себе.")
        return
    await state.update_data(transfer_target_id=target_id, transfer_target_str=target_str)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Без комментария", callback_data="transfer_no_note")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="transfer_cancel")]
    ])
    await state.set_state(TransferForm.waiting_for_note)
    await message.answer("📝 напиши комментарий к переводу или нажми «Без комментария».", reply_markup=kb)

@router.message(TransferForm.waiting_for_note)
async def transfer_note_received(message: Message, state: FSMContext):
    note = (message.text or "").strip()
    if not note:
        await message.answer("Напиши комментарий сообщением или нажми «Без комментария».")
        return
    if len(note) > 500:
        await message.answer("комментарий слишком длинный. максимум 500 символов.")
        return
    await state.update_data(transfer_note=note)
    await state.set_state(TransferForm.waiting_for_amount)
    await message.answer("💰 теперь напиши сумму перевода.\n например: 1000, 100к или 1кк. для отмены введи /cancel.")

@router.message(Command("cancel"), TransferForm.waiting_for_target)
@router.message(Command("cancel"), TransferForm.waiting_for_note)
@router.message(Command("cancel"), TransferForm.waiting_for_amount)
async def transfer_cancel_command(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ перевод отменён.")

@router.message(TransferForm.waiting_for_amount)
async def transfer_process(message: Message, state: FSMContext):
    try:
        amount = parse_amount((message.text or "").strip())
    except ValueError:
        await message.answer("❌ не удалось распознать сумму. Примеры: 1000000, 1 000 000, 100к, 1кк")
        return

    sender_id = message.from_user.id
    data = await state.get_data()
    target_id = data.get("transfer_target_id")
    target_str = data.get("transfer_target_str", str(target_id))
    note = data.get("transfer_note", "")

    if not target_id:
        await state.clear()
        await message.answer("❌ не удалось определить получателя. Начни перевод заново.")
        return

    if amount <= 0:
        await message.answer("❌ сумма должна быть больше нуля.")
        return

    commission = (amount * 5 + 99) // 100  # комиссия 5%, округление вверх
    total_cost = amount + commission

    # Проверка баланса
    if not await deduct_balance(sender_id, total_cost):
        current_balance = await get_balance(sender_id)
        await message.answer(
            f"❌ <b>недостаточно средств</b>\n"
            f"перевод: {amount:,} ₽\n"
            f"комиссия 5%: {commission:,} ₽\n"
            f"всего нужно: <b>{total_cost:,} ₽</b>\n"
            f"твой баланс: {current_balance:,} ₽",
            parse_mode="HTML",
        )
        return

    # --- ПОПЫТКА ПЕРЕВОДА И УВЕДОМЛЕНИЯ ---
    
    # Сначала начисляем деньги получателю (транзакция в БД уже прошла через deduct_balance)
    # Примечание: Если твоя функция deduct_balance делает commit сразу, 
    # то для отката нужно будет вызывать add_to_balance(sender_id, total_cost) при ошибке.
    await add_to_balance(target_id, amount)

    sender_name = await get_user_name(sender_id) or "Игрок"
    notification = f"💸 <b>{sender_name}</b> перевел тебе <b>{amount:,} ₽</b>"
    if note:
        notification += f"\nкомментарий: {note}"

    try:
        # Пытаемся отправить уведомление
        await bot.send_message(target_id, notification, parse_mode="HTML")
        success_msg = (
            f"✅ перевод <b>{amount:,} ₽</b> выполнен пользователю <b>{target_str}</b>\n"
            f"комиссия 5%: {commission:,} ₽\n"
            f"всего списано: <b>{total_cost:,} ₽</b>\n"
        )
        
    except TelegramBadRequest as e:
        # Ловим конкретную ошибку от Telegram
        if "chat not found" in str(e).lower() or "user was deleted" in str(e).lower():
            logger.warning(f"Не удалось уведомить пользователя {target_id}: чат не найден или пользователь заблокировал бота.")
            
            # ВАЖНО: Решаем, что делать с деньгами.
            # Вариант 1: Деньги остаются у получателя, но он не знает. (Плохо для UX)
            # Вариант 2 (РЕКОМЕНДУЕТСЯ): Отменяем перевод, так как сделка не завершена корректно.
            
            # Отменяем начисление получателю
            await deduct_balance(target_id, amount) 
            # Возвращаем деньги отправителю
            await add_to_balance(sender_id, total_cost)
            
            success_msg = (
                f"❌ не удалось выполнить перевод.\n"
                f"твои деньги ({total_cost:,} ₽) возвращены на баланс."
            )
        else:
            # Другая ошибка API, логируем и пробуем продолжить (или тоже отменяем, зависит от политики)
            logger.error(f"Ошибка при отправке уведомления: {e}")
            # Для безопасности экономики лучше тоже отменить перевод при любой ошибке API
            await deduct_balance(target_id, amount)
            await add_to_balance(sender_id, total_cost)
            success_msg = "❌ Произошла ошибка при выполнении перевода. Деньги возвращены."

    except Exception as e:
        # Неожиданные ошибки
        logger.exception("Неожиданная ошибка при переводе")
        # Откат транзакции
        await deduct_balance(target_id, amount)
        await add_to_balance(sender_id, total_cost)
        success_msg = "❌ произошла непредвиденная ошибка. Деньги возвращены."

    # Добавляем комментарий в финальное сообщение
    if note:
        success_msg += f"\n\nкомментарий: {note}"
    else:
        success_msg += "\n\nкомментарий: ---"
        
    success_msg += "\n\nнажми /menu, чтобы вернуться в главное меню"

    await message.answer(success_msg, parse_mode="HTML")
    await state.clear()

@router.message(F.text == "💼 Работа")
async def show_work_menu(message: Message, state: FSMContext):
    await state.clear()
    text = f"<b>выбирай, где хочешь работать:</b>"
    try:
        photo = FSInputFile("images/work.png")
        await message.answer_photo(photo=photo, caption=text, parse_mode="HTML", reply_markup=get_work_keyboard())
    except FileNotFoundError:
        logger.warning("Файл images/work.png не найден.")
        await message.answer(text, reply_markup=get_work_keyboard())

@router.message(F.text == "🛒 Магаз")
async def show_shop_menu(message: Message):
    await message.answer("Раздел «Магаз» пока в разработке — скоро зальём.")

# ============================================================
# КЕЙСЫ
# ============================================================
CASES = {
    "1": {"emoji": "🗿", "name": "каменный кейс", "cost": 6000,
        "outcomes": [(5, 0), (30, 3000), (25, 5000), (20, 8000), (15, 9000), (5, 12000)]},
    "2": {"emoji": "🥉", "name": "бронзовый кейс", "cost": 10000,
        "outcomes": [(5, 0), (30, 5000), (25, 8000), (20, 14000), (15, 16000), (5, 20000)]},
    "3": {"emoji": "🥈", "name": "серебряный кейс", "cost": 30000,
        "outcomes": [(5, 0), (30, 15000), (25, 24000), (20, 40000), (15, 48000), (5, 60000)]},
    "4": {"emoji": "🥇", "name": "золотой кейс", "cost": 100000,
        "outcomes": [(5, 0), (30, 50000), (25, 80000), (20, 140000), (15, 160000), (5, 200000)]},
    "5": {"emoji": "💎", "name": "алмазный кейс", "cost": 500000,
            "outcomes": [(5, 0), (30, 250000), (25, 400000), (20, 700000), (15, 800000), (5, 1000000)]},
    "6": {"emoji": "💠", "name": "платиновый кейс", "cost": 1000000,
            "outcomes": [(5, 0), (30, 500000), (25, 800000), (20, 1400000), (15, 1600000), (5, 2000000)]},
    "7": {"emoji": "⚜️", "name": "элитный кейс", "cost": 2000000,
            "outcomes": [(5, 0), (30, 1000000), (25, 1600000), (20, 2600000), (15, 3200000), (5, 4000000)]},
}
CASE_ORDER = ["1", "2", "3", "4", "5", "6", "7"]
CASE_OPEN_COOLDOWN = 1  # секунда между открытиями кейсов


def get_case_win_chance(case: dict) -> int:
    """Суммарный шанс выйти в плюс (приз больше стоимости кейса)."""
    return sum(chance for chance, prize in case["outcomes"] if prize > case["cost"])


def validate_cases():
    """Проверяет, что сумма шансов каждого кейса равна 100%."""
    for key, case in CASES.items():
        total = sum(chance for chance, _ in case["outcomes"])
        if total != 100:
            raise ValueError(f"Шансы кейса {key} должны составлять 100%, сейчас: {total}%")
        if any(chance < 0 or prize < 0 for chance, prize in case["outcomes"]):
            raise ValueError(f"В кейсе {key} обнаружены отрицательные значения")


validate_cases()


def get_case_text(index: int) -> str:
    key = CASE_ORDER[index]
    case = CASES[key]
    win_chance = get_case_win_chance(case)
    lines = [
        f"{case['emoji']} <b>{case['name']}</b>  ({index + 1}/{len(CASE_ORDER)})",
        "",
        f"💸 стоимость открытия: <b>{case['cost']:,} ₽</b>",
        "",
        "<b>🎁 призы и шансы выпадения:</b>",
    ]
    quote_lines = []
    for chance, prize in case["outcomes"]:
        profit = prize - case["cost"]
        sign = "+" if profit >= 0 else ""
        quote_lines.append(f"💰 <b>{prize:,} ₽</b> ({sign}{profit:,} ₽) — шанс <b>{chance}%</b>")
    lines.append(f"<blockquote>{chr(10).join(quote_lines)}</blockquote>")
    return "\n".join(lines)

def get_case_keyboard(index: int) -> InlineKeyboardMarkup:
    key = CASE_ORDER[index]
    case = CASES[key]
    prev_index = (index - 1) % len(CASE_ORDER)
    next_index = (index + 1) % len(CASE_ORDER)

    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="◀️", callback_data=f"cases_nav:{prev_index}"),
            InlineKeyboardButton(text=case["emoji"], callback_data="cases_noop"),
            InlineKeyboardButton(text="▶️", callback_data=f"cases_nav:{next_index}"),
        ],
        [InlineKeyboardButton(text=f"📦 Открыть за {case['cost']:,} ₽", callback_data=f"cases_open:{index}")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="cases_back")],
    ])


@router.message(F.text == "📦 Кейсы")
async def show_cases(message: Message, state: FSMContext):
    if not await check_level_access(message, message.from_user.id, CASE_UNLOCK_LEVEL): 
            return
    await state.clear()
    await message.answer(
        get_case_text(0),
        parse_mode="HTML",
        reply_markup=get_case_keyboard(0),
    )


@router.callback_query(F.data.startswith("cases_nav:"))
async def cases_nav(callback: CallbackQuery):
    index = int(callback.data.split(":", 1)[1])
    await callback.answer()
    try:
        await callback.message.edit_text(
            get_case_text(index),
            parse_mode="HTML",
            reply_markup=get_case_keyboard(index),
        )
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "cases_noop")
async def cases_noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data == "cases_back")
async def cases_back(callback: CallbackQuery):
    user_id = callback.from_user.id
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await callback.answer()
    await send_main_menu(callback, user_id)


@router.callback_query(F.data.startswith("cases_open:"))
async def cases_open(callback: CallbackQuery):
    user_id = callback.from_user.id
    index = int(callback.data.split(":", 1)[1])
    if index < 0 or index >= len(CASE_ORDER):
        await callback.answer("Такого кейса нет.", show_alert=True)
        return

    case = CASES[CASE_ORDER[index]]
    cost = case["cost"]

    now = time.time()
    last_open = _case_cooldown_cache.get(user_id, 0)
    remaining = last_open + CASE_OPEN_COOLDOWN - now
    if remaining > 0:
        await callback.answer("⏳ подожди секунду перед следующим открытием.", show_alert=True)
        return
    _case_cooldown_cache[user_id] = now

    if not await deduct_balance(user_id, cost):
        balance = await get_balance(user_id)
        await callback.answer(f"Недостаточно средств. Баланс: {balance:,} ₽", show_alert=True)
        return

    await callback.answer("Кейс открывается…")
    chances = [chance for chance, _ in case["outcomes"]]
    prizes = [prize for _, prize in case["outcomes"]]
    prize = random.choices(prizes, weights=chances, k=1)[0]

    # Анимация: одно сообщение несколько раз редактируется.
    frames = [
        f"{case['emoji']} <b>{case['name']}</b>\n\n🔒 Кейс запущен…",
        f"{case['emoji']} <b>{case['name']}</b>\n\n🎁 <code>［□□□□□］</code>",
        f"{case['emoji']} <b>{case['name']}</b>\n\n🎁 <code>［■□□□□］</code>",
        f"{case['emoji']} <b>{case['name']}</b>\n\n🎁 <code>［■■■□□］</code>",
        f"{case['emoji']} <b>{case['name']}</b>\n\n🎁 <code>［■■■■□］</code>",
        f"{case['emoji']} <b>{case['name']}</b>\n\n🎁 <code>［■■■■■］</code>",
        f"{case['emoji']} <b>{case['name']}</b>\n\n✨ Определяем приз…",
    ]
    for frame in frames:
        try:
            await callback.message.edit_text(frame, parse_mode="HTML")
        except TelegramBadRequest:
            pass
        await asyncio.sleep(0.45)

    if prize > 0:
        new_balance = await add_to_balance(user_id, prize)
        profit = prize - cost
        sign = "+" if profit >= 0 else ""
        result_text = (
            f"{case['emoji']} <b>{case['name']}</b>\n\n"
            f"🎉 выпало: <b>{prize:,} ₽</b>\n"
            f"💳 баланс: <b>{new_balance:,} ₽</b>"
        )
    else:
        new_balance = await get_balance(user_id)
        result_text = (
            f"{case['emoji']} <b>{case['name']}</b>\n\n"
            f"💨 приз не выпал.\n"
            f"потрачено: <b>{cost:,} ₽</b>\n"
            f"💳 баланс: <b>{new_balance:,} ₽</b>"
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Открыть ещё", callback_data=f"cases_open:{index}")],
        [InlineKeyboardButton(text="🔙 К кейсам", callback_data=f"cases_nav:{index}")],
        [InlineKeyboardButton(text="🏠 В меню", callback_data="cases_back")],
    ])
    try:
        await callback.message.edit_text(result_text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest:
        await callback.message.answer(result_text, parse_mode="HTML", reply_markup=kb)

@router.message(F.text == "🏆 Топ")
async def show_top(message: Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Топ по балансу", callback_data="public_top:balance")],
        [InlineKeyboardButton(text="👥 Топ по рефералам", callback_data="public_top:referrals")],
        [InlineKeyboardButton(text="📈 Топ по уровню", callback_data="public_top:level")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="public_top:back_to_main")],
    ])
    prompt_text = random.choice(TOP_PROMPT_VARIANTS)
    try:
        photo = FSInputFile("images/123.png")
        await message.answer_photo(photo=photo, reply_markup=ReplyKeyboardRemove())
    except FileNotFoundError:
        await message.answer("🏆", reply_markup=ReplyKeyboardRemove())
    await message.answer(prompt_text, reply_markup=kb)


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
        await callback.message.edit_text("🏆 <b>Выбери рейтинг:</b>", parse_mode="HTML", reply_markup=kb)
        return

    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 К выбору топа", callback_data="public_top:menu")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="public_top:back_to_main")],
    ])

    # Медали для топ-3
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}

    if kind == "balance":
        balances = await get_all_balances()
        if not balances:
            result_text = "🏆 <b>Топ по балансу</b>\n\nПока пусто — никто не играл."
        else:
            result_text = "💰 <b>Топ по балансу:</b>\n\n"
            for i, (uid, name, balance) in enumerate(balances[:10], 1):
                medal = medals.get(i, f"{i}.")
                result_text += f"{medal} {name} — <b>{balance:,} ₽</b>\n"
            if len(balances) > 10:
                result_text += f"\n...и ещё {len(balances) - 10} челиков"

    elif kind == "referrals":
        top = await get_top_referrals(10)
        if not top:
            result_text = "👥 <b>Топ по рефералам</b>\n\nПока пусто — никто никого не пригласил."
        else:
            result_text = "👥 <b>Топ по рефералам:</b>\n\n"
            for i, (uid, count) in enumerate(top, 1):
                name = await get_top_display_name(uid)
                medal = medals.get(i, f"{i}.")
                result_text += f"{medal} {name} — <b>{count}</b> реф.\n"

    elif kind == "level":
        top = await get_top_levels(10)
        if not top:
            result_text = "📈 <b>Топ по уровням</b>\n\nПока пусто — никто не получил XP."
        else:
            result_text = "📈 <b>Топ по уровням:</b>\n\n"
            for i, (uid, lvl) in enumerate(top, 1):
                name = await get_top_display_name(uid)
                medal = medals.get(i, f"{i}.")
                result_text += f"{medal} {name} — <b>{lvl}</b> ур.\n"
    else:
        await callback.answer("Неизвестный рейтинг.", show_alert=True)
        return

    try:
        await callback.message.edit_text(result_text, parse_mode="HTML", reply_markup=back_kb)
    except TelegramBadRequest:
        await callback.message.answer(result_text, parse_mode="HTML", reply_markup=back_kb)


# ============================================================
# ЕЖЕДНЕВНЫЙ БОНУС — ХЕНДЛЕРЫ
# ============================================================
@router.message(F.text == "🎁 Бонус")
async def handle_daily_bonus(message: Message, state: FSMContext):
    if not await check_level_access(message, message.from_user.id, BONUS_UNLOCK_LEVEL):
            return
    user_id = message.from_user.id
    await state.clear()

    can, remaining = await can_claim_daily(user_id)
    streak = await get_daily_streak(user_id)

    if can:
        preview_amount = await daily_bonus_amount(streak)
        text = (
            f"🎁 <b>ежедневный бонус</b>\n\n"
            f"🔥 серия: <b>{streak}</b> дн. подряд\n"
            f"💰 сегодня получишь: ~<b>{preview_amount:,} ₽</b>\n\n"
            f"жми «забрать», чтобы получить награду!"
        )
        kb = get_daily_bonus_keyboard(True)
    else:
        text = (
            f"🎁 <b>ежедневный бонус</b>\n\n"
            f"🔥 серия: <b>{streak}</b> дн. подряд\n"
            f"⏳ бонус уже забран. приходи через:\n"
            f"⏰ <b>{format_cooldown(remaining)}</b>"
        )
        kb = get_daily_bonus_keyboard(False)

    try:
        photo = FSInputFile("images/daily_bonus.png")
        await message.answer_photo(photo=photo, caption=text, parse_mode="HTML", reply_markup=kb)
    except FileNotFoundError:
        logger.warning("Файл images/daily_bonus.png не найден.")
        await message.answer(text, parse_mode="HTML", reply_markup=kb)

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
            f"⏳ бонус уже забран. приходи через {hours} ч {mins} мин {secs} сек.",
            reply_markup=get_daily_bonus_keyboard(False)
        )
        return

    amount, new_streak = await claim_daily_bonus(user_id)
    new_balance = await get_balance(user_id)

    text = (
        f"🎁 <b>ежедневный бонус забран!</b>\n\n"
        f"💰 получено: +<b>{amount:,} ₽</b>\n"
        f"🔥 серия: <b>{new_streak}</b> дн. подряд\n"
        f"💳 баланс: <b>{new_balance:,} ₽</b>\n\n"
        f"возвращайся завтра — серия продолжится!"
    )
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_daily_bonus_keyboard(False))
    except TelegramBadRequest:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=get_daily_bonus_keyboard(False))


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
        await message.answer("⛏ ты продвинулся, умничка")
    pickaxe_lvl = await get_pickaxe_level(user_id)
    pickaxe_name = PICKAXE_LEVELS[pickaxe_lvl]["name"]
    reward = get_mine_reward_for_pickaxe(pickaxe_lvl)
    text = (
        f"⛏ ты в шахте\n\n"
        f"🔧 кирка: <b>{pickaxe_name}</b>\n"
        f"💰 за клик: <b>{reward:,} ₽</b>\n"
    )
    try:
        photo = FSInputFile("images/mine.png")
        await message.answer_photo(photo=photo, caption=text, parse_mode="HTML", reply_markup=get_mine_keyboard())
    except FileNotFoundError:
        logger.warning("Файл images/mine.png не найден.")
        await message.answer(text, parse_mode="HTML", reply_markup=get_mine_keyboard())

@router.callback_query(F.data == "help_top")
async def handle_help_top(callback: CallbackQuery):
    text = HELP_TEXT_TOP
    try:
        await callback.message.edit_text(text, reply_markup=get_help_menu_keyboard())
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=get_help_menu_keyboard())
    await callback.answer()


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
        await message.answer(f"⏳ обожди {remaining} сек.")
        return

    pickaxe_lvl = await get_pickaxe_level(user_id)
    reward = get_mine_reward_for_pickaxe(pickaxe_lvl)
    pickaxe_name = PICKAXE_LEVELS[pickaxe_lvl]["name"]

    new_balance = await add_to_balance(user_id, reward)
    _, new_level, leveled_up = await add_xp(user_id, XP_PER_MINE)

    text = (
        f"⛏у тебя в руках {pickaxe_name} кирка\n+<b>{reward:,} ₽</b> +{XP_PER_MINE} XP!\n"
        f"Баланс: <b>{new_balance:,} ₽</b>"
    )
    await message.answer(text, parse_mode="HTML")

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
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


# --- Прокачка кирки: кнопка inline "Прокачать" ---
@router.callback_query(MineForm.in_mine, F.data == "pickaxe_upgrade")
async def handle_pickaxe_upgrade_do(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    await callback.answer()

    pickaxe_lvl = await get_pickaxe_level(user_id)

    if pickaxe_lvl >= len(PICKAXE_LEVELS) - 1:
        await callback.answer("уже максимальный уровень!", show_alert=True)
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
        f"🎉 кирка улучшена: {PICKAXE_LEVELS[pickaxe_lvl]['name']} → <b>{nxt['name']}</b>!\n"
        f"новый доход: <b>{nxt['reward']:,} ₽/клик</b>\n\n"
    )

    if tutorial_step == "mine":
        upgrade_msg += (
            "\n\nтвоя первая прокачка кирки! дальше меньше!!"
        )
    try:
        await callback.message.edit_text(upgrade_msg + text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest:
        await callback.message.answer(upgrade_msg + text, parse_mode="HTML", reply_markup=kb)


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
        f"⛏ ты в шахте\n\n"
        f"🔧 кирка: <b>{pickaxe_name}</b>\n"
        f"💰 за клик: <b>{reward:,} ₽</b>\n"
    )
    await callback.message.answer(text, parse_mode="HTML", reply_markup=get_mine_keyboard())

@router.message(F.text == "🔗 Реф")
async def handle_ref(message: Message):
    user_id = message.from_user.id
    referral_count = await get_referral_count(user_id)
    referral_earnings = await get_referral_earnings(user_id)
    bot_info = await message.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"

    text = (
        f"🔗 <b>Реферальная система</b>\n\n"
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
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "ref_top")
async def handle_ref_top(callback: CallbackQuery):
    user_id = callback.from_user.id
    await callback.answer()

    if not is_admin(user_id):
        cooldown_key = f"cooldown:reftop:{user_id}"
        ok = await redis_client.set(cooldown_key, "1", nx=True, ex=60)
        if not ok:
            ttl = await redis_client.ttl(cooldown_key)
            await callback.message.answer(f"⏳ топ можно глянуть через {ttl} сек.")
            return

    top = await get_top_referrals(10)
    if not top:
        text = "👥 <b>Топ по рефералам</b>\n\nПока пусто — никто никого не пригласил."
    else:
        text = "👥 <b>Топ по рефералам:</b>\n\n"
        for i, (uid, count) in enumerate(top, 1):
            name = await get_top_display_name(uid)
            text += f"{i}. {name} — <b>{count}</b> реф.\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="ref_back_to_info")],
    ])

    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)


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
        f"🔗 <b>Реферальная система</b>\n\n"
        f"Твоя ссылка:\n`{ref_link}`\n\n"
        f"👥 Приглашено: <b>{referral_count}</b> чел.\n"
        f"💰 Заработано с рефералов: <b>{referral_earnings:,} ₽</b>\n\n"
        f"За каждого приглашённого — <b>{REFERRAL_REWARD:,} ₽</b>\n"
        f"Новичку за регистрацию по ссылке — <b>{REFERRAL_NEWBIE_BONUS:,} ₽</b>"
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
    if not await check_level_access(message, message.from_user.id, TRADING_UNLOCK_LEVEL):
        return
    user_id = message.from_user.id
    balance = await get_balance(user_id)

    if balance < TRADING_MIN_BALANCE:
        await message.answer(
            f"❌ не хватает денег для трейдинга.\n"
            f"минимальный порог входа: <b>{TRADING_MIN_BALANCE:,} ₽</b>\n"
            "найди деньги и подключайся к трейдингу",
            parse_mode="HTML",
            reply_markup=get_work_keyboard()
        )
        return

    await message.answer("💻", reply_markup=ReplyKeyboardRemove())
    await message.answer(
        "курсы не продам\n"
        f"💰 твой баланс: <b>{balance:,} ₽</b>\n"
        "выбери уровень риска:",
        parse_mode="HTML",
        reply_markup=get_trading_mode_keyboard()
    )

@router.callback_query(F.data.startswith("trade_mode:"))
async def choose_risk(callback: CallbackQuery, state: FSMContext):
    mode = callback.data.split(":")[1]
    await state.update_data(trade_mode=mode)
    await callback.answer()

    balance = await get_balance(callback.from_user.id)
    await callback.message.edit_text(
        f"режим: <b>{mode}</b>\n"
        f"💰 баланс: <b>{balance:,} ₽</b>\n"
        "введи сумму ставки:",
        parse_mode="HTML",
        reply_markup=get_trading_result_keyboard2()
    )
    await state.set_state(TradingForm.waiting_for_amount)

@router.message(TradingForm.waiting_for_amount)
async def process_trading_amount(message: Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    amount_msg_id = data.get("amount_msg_id")

    try:
        amount = parse_amount(message.text)
        if amount <= 0:
            await message.answer("сумма должна быть больше 00. попробуй ещё раз:")
            return
    except ValueError:
        await message.answer("введи целое число (например, 67):")
        return

    balance = await get_balance(user_id)
    if amount > balance:
        await message.answer(f"не хватает денег! баланс: {balance:,} ₽\nвведи меньше:")
        return

    if amount_msg_id:
        try:
            await message.bot.edit_message_reply_markup(
                chat_id=message.chat.id, message_id=amount_msg_id, reply_markup=None
            )
        except TelegramBadRequest:
            pass

    await state.update_data(amount=amount)
    caption_text = f"📊 график актива\nставка: <b>{amount:,} ₽</b>\nкуда пойдёт график?"
    photo_path = "images/graph.png"
    if not os.path.exists(photo_path):
        logger.warning(f"Файл {photo_path} не найден. Отправляем только текст.")
        await message.answer(text=caption_text, parse_mode="HTML", reply_markup=get_trading_direction_keyboard())
    else:
        try:
            photo = FSInputFile(photo_path)
            await message.answer_photo(photo=photo, caption=caption_text, parse_mode="HTML", reply_markup=get_trading_direction_keyboard())
        except Exception as e:
            logger.error(f"Ошибка отправки фото: {e}")
            await message.answer(text=caption_text, parse_mode="HTML", reply_markup=get_trading_direction_keyboard())
    await state.set_state(TradingForm.waiting_for_direction)

TRADE_STEPS = [
    "📊 анализирую рынок...",
    "📈 читаю график...",
    "🔍 ищу точку входа...",
    "🧮 рассчитываю сделку...",
]

@router.callback_query(TradingForm.waiting_for_direction, F.data.in_({"trade_up", "trade_down"}))
async def handle_trade_direction(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    data = await state.get_data()

    mode = data.get("trade_mode")
    amount = data.get("amount")
    direction = "up" if callback.data == "trade_up" else "down"

    if not mode or not amount:
        await callback.answer("❌ сессия истекла. начни трейдить заново.", show_alert=True)
        await state.clear()
        return

    # Ставку списываем атомарно ДО анимации: баланс сразу отражает риск.
    if not await deduct_balance(user_id, int(amount)):
        balance = await get_balance(user_id)
        await callback.answer(f"Недостаточно средств. Баланс: {balance:,} ₽", show_alert=True)
        await state.clear()
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        return

    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)

    # Сначала отправляем первую фразу
    msg = await callback.message.answer(TRADE_STEPS[0])

    # Меняем фразы по очереди
    for step in TRADE_STEPS[1:]:
        await asyncio.sleep(random.uniform(0.6, 1.0))  # пауза между шагами
        await msg.edit_text(step)

    # Финальная пауза перед результатом (чтобы не было слишком быстро)
    await asyncio.sleep(random.uniform(0.8, 1.2))

    info = TRADING_MODES[mode]
    won = random.random() < info["chance"]
    multiplier = info["multiplier"]

    if won:
        # Ставка уже списана; возвращаем её с множителем.
        payout = int(amount * multiplier)
        profit = payout - int(amount)
        await add_to_balance(user_id, payout)
        result_text = (
            f"🎉 <b>рынок на твоей стороне!</b>\n"
            f"режим: {mode}\n"
            f"направление: {'📈 Вверх' if direction == 'up' else '📉 Вниз'}\n"
            f"ставка: {amount:,} ₽\n"
            f"чистая прибыль: +<b>{profit:,} ₽</b> (выплата {payout:,} ₽, x{multiplier})"
        )
        await log_trade(user_id, mode, amount, profit, True)
    else:
        # Проигрыш: ставка уже списана перед анимацией.
        result_text = (
            f"💥 <b>сделка ушла в минус...</b>\n"
            f"режим: {mode}\n"
            f"направление: {'📈 Вверх' if direction == 'up' else '📉 Вниз'}\n"
            f"ставка: <b>{amount:,} ₽</b> сгорела\n"
        )
        await log_trade(user_id, mode, amount, -amount, False)

    _, new_level, leveled_up = await add_xp(user_id, XP_PER_TRADE)
    await msg.edit_text(result_text, parse_mode="HTML", reply_markup=get_trading_result_keyboard())

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
            f"❌ не хватает денег для трейдинга (нужно <b>{TRADING_MIN_BALANCE:,} ₽</b>).",
            parse_mode="HTML",
            reply_markup=get_work_keyboard()
        )
        await state.clear()
        return

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    await callback.message.answer(
        f"💰 твой баланс: <b>{balance:,} ₽</b>\n"
        "выбери уровень риска:",
        parse_mode="HTML",
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
    await callback.message.answer("ты закончил трейдинг, надеюсь ты в плюсе", reply_markup=get_work_keyboard())

@router.callback_query(F.data == "trade_cancel")
async def process_trade_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await state.clear()
    await callback.message.answer("❌ ставка отменена.", reply_markup=get_work_keyboard())

# ============================================================
# МАТЕМАТИКА
# ============================================================

@router.message(F.text == "🧮 Математика")
async def handle_math(message: Message, state: FSMContext):
    """Вход в математику — без кулдауна, первый пример сразу."""
    if not await check_level_access(message, message.from_user.id, MATH_UNLOCK_LEVEL):
        return
    problem_text, answer = await generate_math_problem(message.from_user.id)
    await state.update_data(math_answer=answer)
    await state.set_state(MathForm.waiting_for_answer)

    await message.answer(
        f"🧮",
        reply_markup=ReplyKeyboardRemove()
    )
    sent = await message.answer(
        f"реши пример!\n\n<b>{problem_text}</b>\n\nпиши ответ числом:",
        parse_mode="HTML",
        reply_markup=get_math_keyboard()
    )
    await state.update_data(problem_msg_id=sent.message_id)

MATH_CORRECT_PHRASES = [
    "✅ Точно!",
    "🎉 Молодец!",
    "🔥 Верно!",
    "💯 Попадание!",
    "🚀 Точно в цель!",
    "🌟 Ты гений!",
    "📊 Данные сошлись!"
]

@router.message(MathForm.waiting_for_answer)
async def process_math_answer(message: Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    correct_answer = data.get("math_answer")
    problem_msg_id = data.get("problem_msg_id")
    if correct_answer is None:
        await message.answer("этот пример уже решён. жми «Следующий»!")
        return
    try:
        user_answer = int(message.text.strip())
    except ValueError:
        await message.answer("❌ пиши число(например:67)")
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
        _, new_level, leveled_up = await add_xp(user_id, MATH_XP_REWARD)

        # Выбираем случайную фразу
        prefix = random.choice(MATH_CORRECT_PHRASES)

        result_text = (
            f"<b>{prefix}</b> +<b>{MATH_REWARD:,} ₽</b> и +{MATH_XP_REWARD} XP!\n"
            f"Баланс: <b>{new_balance:,} ₽</b>"
        )
        if leveled_up:
            await notify_level_up(user_id, new_level)
    else:
        result_text = f"❌ Мимо. Правильный ответ: <b>{correct_answer}</b>"
    await message.answer(result_text, parse_mode="HTML", reply_markup=get_math_keyboard())
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
        f"🧮 реши пример!\n\n<b>{problem_text}</b>\n\nпиши ответ числом:",
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
    await callback.message.answer("🚪 ты вышел с математики(правильно сделал)", reply_markup=get_work_keyboard())

# ============================================================
# БИЗНЕС
# ============================================================

@router.message(F.text == "🏪 Бизнесы")
async def handle_my_businesses(message: Message, state: FSMContext):
    if not await check_level_access(message, message.from_user.id, BUSINESS_UNLOCK_LEVEL):
        return
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
            "выбирай, чем займешься:",
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
            await callback.answer("у тебя уже есть бизнес! сначала продай его.", show_alert=True)
            return
        balance = await get_balance(user_id)
        if balance < biz_def["price"]:
            await callback.answer("не хватает денег!", show_alert=True)
            return
        await callback.answer()
        ok = await deduct_balance(user_id, biz_def["price"])
        if not ok:
            await callback.answer("не хватает денег!", show_alert=True)
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
            "broken": False,
            "last_break_check": time.time(),
            "last_collected": time.time(),
        }
        await save_biz(user_id, new_biz)
        text, kb = biz_manage_view(new_biz)
        await callback.message.edit_text(
            f"✅ взял «{biz_def['name']}» за <b>{biz_def['price']:,} ₽</b>!\n\n" + text,
            parse_mode="HTML",
            reply_markup=kb
        )
        return

    biz = await get_biz(user_id)
    if not biz:
        await callback.answer()
        text, kb = biz_no_biz_view()
        await biz_edit(callback, text, kb)
        return

    # Не начисляем пассивный доход при каждом действии внутри бизнеса.
    # Расчёт выполняется при входе в меню управления (biz_manage / команда меню).

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
            f"введи количество сырья для закупки.\n"
            f"цена: {RAW_PRICE} ₽ за штуку\n"
            f"свободно на складе: <b>{space:,}</b>\n"
            f"оплата: {source_text}",
            parse_mode="HTML",
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
                await callback.answer(f"не хватает {cost - balance:,} ₽", show_alert=True)
                return
            await callback.answer()
            ok = await deduct_balance(user_id, cost)
            if not ok:
                await callback.answer("не хватает денег!", show_alert=True)
                return
        else:
            biz_balance = biz.get("balance", 0)
            if biz_balance < cost:
                await callback.answer(f"на счёте бизнеса не хватает {cost - biz_balance:,} ₽", show_alert=True)
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

    if data == "biz_repair":
        if not biz.get("broken"):
            await callback.answer("бизнес не сломан.", show_alert=True)
            return
        repair_cost = int(biz.get("price", 0) * BUSINESS_REPAIR_COST_RATE)
        if not await deduct_balance(user_id, repair_cost):
            await callback.answer(f"не хватает {repair_cost:,} ₽ на ремонт.", show_alert=True)
            return
        biz["broken"] = False
        biz.pop("broken_since", None)
        biz["last_collected"] = time.time()
        await save_biz(user_id, biz)
        await callback.answer("бизнес починен!")
        text, kb = biz_manage_view(biz)
        await biz_edit(callback, "🛠 <b>бизнес успешно починен!</b>\n\n" + text, kb)
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
            f"⚠️ ты точно хочешь продать «{biz['name']}»?\n\n"
            f"на руки получишь: <b>{sell_price:,} ₽</b>\n\n"
            f"после продажи бизнес исчезнет, бабки упадут на баланс"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, продаю", callback_data="biz_sell_do"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="biz_manage"),
            ],
        ])
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        return

    if data == "biz_sell_do":
        await callback.answer("Продали.")
        sell_price = biz_sell_price(biz)
        await add_to_balance(user_id, sell_price)
        await save_biz(user_id, None)
        text, kb = biz_no_biz_view()
        await callback.message.edit_text(
            f"✅ бизнес продан! на руках: <b>{sell_price:,} ₽</b>.\n\n" + text,
            parse_mode="HTML",
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
            f"💰 Забрал <b>{biz_balance:,} ₽</b>!\n\n" + text,
            parse_mode="HTML",
            reply_markup=kb
        )
        return

# --- БИЗНЕС: ввод количества сырья ---
@router.message(BusinessForm.waiting_for_raw)
async def process_raw_amount(message: Message, state: FSMContext):
    user_id = message.from_user.id

    try:
        amount = parse_amount(message.text)
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
        await message.answer(f"на складе не хватает места! Свободно: {space:,}\nвведи меньше:")
        return

    cost = amount * RAW_PRICE

    if source == "user":
        balance = await get_balance(user_id)
        if balance < cost:
            await message.answer(f"не хватает {cost - balance:,} ₽. баланс: {balance:,} ₽\nвведи меньше:")
            return
        ok = await deduct_balance(user_id, cost)
        if not ok:
            await message.answer("Не хватает денег! Попробуй меньше:")
            return
    else:
        biz_balance = biz.get("balance", 0)
        if biz_balance < cost:
            await message.answer(f"На счёте бизнеса не хватает {cost - biz_balance:,} ₽. Там: {biz_balance:,} ₽\nвведи меньше:")
            return
        biz["balance"] = biz_balance - cost

    biz["raw_stock"] = stock + amount
    biz.pop("empty_since", None)
    biz.pop("empty_notified", None)
    await save_biz(user_id, biz)
    await state.clear()

    await message.answer(
        f"✅ затарил <b>{amount:,}</b> шт. сырья за <b>{cost:,} ₽</b>\n"
        f"склад: {biz['raw_stock']:,}/{capacity:,}",
        parse_mode="HTML",
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
        f"🎡 <b>Рулетка</b>\n\n"
        f"💰 Твой баланс: <b>{balance:,} ₽</b>\n\n"
        f"Введи сумму ставки:",
        parse_mode="HTML",
        reply_markup=get_roulette_amount_keyboard(0)
    )
    await state.update_data(amount_msg_id=sent.message_id, amount=0)


@router.message(F.text == "🎰 Казино")
async def show_casino(message: Message, state: FSMContext):
    if not await check_level_access(message, message.from_user.id, CASINO_UNLOCK_LEVEL):
        return
    await state.clear()
    text = "🎰 добро пожаловать в казино 'лохотрон'\n\nза какой стол хочешь сесть?"
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
        amount = parse_amount(message.text)
    except (TypeError, ValueError):
        await message.answer("❌ Введи целое число, например: 52000")
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
        f"🎡 <b>рулетка 'risk=rich'</b>\n\n"
        f"🎯 на что ставишь?"
    )

    try:
        photo = FSInputFile("images/roulette_table.png")
        await message.answer_photo(photo=photo, caption=text, parse_mode="HTML", reply_markup=get_roulette_bet_keyboard(amount))
    except FileNotFoundError:
        logger.warning("Файл images/roulette_table.png не найден.")
        await message.answer(text, parse_mode="HTML", reply_markup=get_roulette_bet_keyboard(amount))


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
        f"🎯 ставка: <b>{roulette_bet_name(bet)}</b>\n"
        f"💰 сумма: <b>{amount:,} ₽</b>"
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
                    f"🎯 ставка: <b>{roulette_bet_name(bet)}</b>\n"
                    f"💰 сумма: <b>{amount:,} ₽</b>"
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
            f"выпало: {color} <b>{number}</b>\n"
            f"твоя ставка: <b>{roulette_bet_name(bet)}</b>\n\n"
            f"✅ <b>ВЫЙГРЫШ!</b>\n"
            f"🎉 пополнение: +{amount:,} ₽\n"
            f"💰 баланс: <b>{new_balance:,} ₽</b>"
        )
    else:
        new_balance = await get_balance(user_id)

        result_text = (
            f"🎡 <b>СТОП!</b>\n\n"
            f"выпало: {color} <b>{number}</b>\n"
            f"твоя ставка: <b>{roulette_bet_name(bet)}</b>\n\n"
            f"❌ <b>ПРОИГРЫШ!</b>\n"
            f"💸 списание: -{amount:,} ₽\n"
            f"💰 баланс: <b>{new_balance:,} ₽</b>"
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
    if not await check_level_access(message, message.from_user.id, DUEL_UNLOCK_LEVEL):
        return
    await state.clear()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 В меню", callback_data="main_menu")]
    ])

    await message.answer(
        "🥊 <b>Дуэли</b>\n\n"
        "напиши ник и сумму, кому хочешь кинуть дуэль\n"
        "формат: `ник сумма`\n",
        parse_mode="HTML",
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
        await message.answer(f"⏳ дуэль можно кинуть только через {remaining} сек.")
        return

    parts = text.rsplit(maxsplit=1)
    if len(parts) != 2:
        await message.answer(
            "❌ Неверный формат\n Пример: `killer 69000`\n"
            "или нажми «🔙 Назад» для выхода.",
            parse_mode="Markdown"
        )
        return

    nick_str, amount_str = parts
    try:
        amount = parse_amount(amount_str)
    except ValueError:
        await message.answer("❌ сумма должна быть числом. Пример: `убийца52 676767`", parse_mode="Markdown")
        return

    if amount <= 0:
        await message.answer("❌ сумма должна быть больше 0.")
        return

    balance = await get_balance(user_id)
    if balance < amount:
        await message.answer(f"❌ не хватает денег. Баланс: {balance:,} ₽")
        return

    target_id = await get_user_id_by_name_direct(nick_str)
    if not target_id:
        target_id = await get_user_id_by_username(nick_str)

    if not target_id:
        await message.answer(f"❌ игрок «{nick_str}» не найден.")
        return

    if target_id == user_id:
        await message.answer("❌ ты как собираешь против себя играть?")
        return

    target_balance = await get_balance(target_id)
    if target_balance < amount:
        target_name = await get_user_name(target_id) or "Игрок"
        await message.answer(f"❌ у {target_name} недостаточно денег для этой ставки.")
        return

    duel_id = await create_duel(user_id, target_id, amount)
    challenger_name = await get_user_name(user_id) or "Игрок"
    target_name = await get_user_name(target_id) or "Игрок"

    await message.answer(
        f"🥊 ты вызвал <b>{target_name}</b> на дуэль на <b>{amount:,} ₽</b>.\n"
        f"ждём ответ...\n введи /menu чтобы продолжить играть, пока ожидаешь",
        parse_mode="HTML",
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
            f"🥊 <b>{challenger_name}</b> вызывает тебя на дуэль на <b>{amount:,} ₽</b>.\n"
            f"принять?",
            parse_mode="HTML",
            reply_markup=kb
        )
    except Exception:
        await message.answer("❌ не удалось отправить вызов")
        await redis_client.delete(f"duel:{duel_id}")

    await state.clear()


@router.callback_query(F.data.startswith("duel_accept:"))
async def duel_accept(callback: CallbackQuery, state: FSMContext):
    duel_id = callback.data.split(":", 1)[1]
    duel = await get_duel(duel_id)

    if not duel:
        await callback.answer("⏰ дуэль истекла.", show_alert=True)
        return

    if callback.from_user.id != duel["target_id"]:
        await callback.answer("это не твой вызов!", show_alert=True)
        return

    if duel["status"] != "pending":
        await callback.answer("дуэль уже обработана.", show_alert=True)
        return

    await callback.answer()

 # --- Проверка кулдауна у принимающего ---
    can_duel, remaining = await check_duel_cooldown(callback.from_user.id)
    if not can_duel:
        await callback.answer(f"⏳ дуэль можно принять через {remaining} сек.", show_alert=True)
        return
    
    ch_balance = await get_balance(duel["challenger_id"])
    tg_balance = await get_balance(duel["target_id"])

    if ch_balance < duel["amount"]:
        await callback.message.edit_text("❌ у вызывающего недостаточно денег. дуэль отменена.")
        await update_duel_status(duel_id, "cancelled")
        try:
            await bot.send_message(duel["challenger_id"], "❌ у тебя недостаточно денег на дуэль. вызов отменён.")
        except Exception:
            pass
        return

    if tg_balance < duel["amount"]:
        await callback.message.edit_text("❌ у тебя недостаточно денег. Дуэль отменена.")
        await update_duel_status(duel_id, "cancelled")
        return

    await update_duel_status(duel_id, "active")

    ok_ch = await deduct_balance(duel["challenger_id"], duel["amount"])
    ok_tg = await deduct_balance(duel["target_id"], duel["amount"])

    if not ok_ch:
        # Возвращаем деньги второму, если первому не хватило
        if ok_tg:
            await add_to_balance(duel["target_id"], duel["amount"])
        await callback.message.edit_text("❌ у вызывающего недостаточно денег. Дуэль отменена.")
        await update_duel_status(duel_id, "cancelled")
        try:
            await bot.send_message(duel["challenger_id"], "❌ у тебя недостаточно денег на дуэль. Вызов отменён.")
        except Exception:
            pass
        return

    if not ok_tg:
        # Первому уже списали — возвращаем
        await add_to_balance(duel["challenger_id"], duel["amount"])
        await callback.message.edit_text("❌ у тебя недостаточно денег. Дуэль отменена.")
        await update_duel_status(duel_id, "cancelled")
        return

    ch_name = await get_user_name(duel["challenger_id"]) or "Игрок"
    tg_name = await get_user_name(duel["target_id"]) or "Игрок"

    start_text = (
        f"🥊 дуэль: <b>{ch_name}</b> vs <b>{tg_name}</b>\n"
        f"💰 ставка: <b>{duel['amount']:,} ₽</b>\n\n"
        f"🎲 бросаем кости..."
    )

    try:
        await callback.message.edit_text(start_text, parse_mode="HTML")
    except TelegramBadRequest:
        await callback.message.answer(start_text, parse_mode="HTML")

    try:
        await bot.send_message(duel["challenger_id"], start_text, parse_mode="HTML")
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
            f"🥊 дуэль: <b>{ch_name}</b> vs <b>{tg_name}</b>\n"
            f"💰 ставка: {duel['amount']:,} ₽\n\n"
            f"🎲 {ch_name}: {ch_value}\n"
            f"🎲 {tg_name}: {tg_value}\n\n"
            f"🎉 <b>победил {ch_name}!</b>\n"
            f"💰 выигрыш: +<b>{duel['amount']:,} ₽</b>"
        )
    elif tg_value > ch_value:
        await add_to_balance(duel["target_id"], duel["amount"] * 2)
        result_text = (
            f"🥊 дуэль: <b>{ch_name}</b> vs <b>{tg_name}</b>\n"
            f"💰 ставка: {duel['amount']:,} ₽\n\n"
            f"🎲 {ch_name}: {ch_value}\n"
            f"🎲 {tg_name}: {tg_value}\n\n"
            f"🎉 <b>победил {tg_name}!</b>\n"
            f"💰 выигрыш: +<b>{duel['amount']:,} ₽</b>"
        )
    else:
        await add_to_balance(duel["challenger_id"], duel["amount"])
        await add_to_balance(duel["target_id"], duel["amount"])
        result_text = (
            f"🥊 дуэль: <b>{ch_name}</b> vs <b>{tg_name}</b>\n"
            f"💰 ставка: {duel['amount']:,} ₽\n\n"
            f"🎲 {ch_name}: {ch_value}\n"
            f"🎲 {tg_name}: {tg_value}\n\n"
            f"🤝 <b>ничья!</b> неньги возвращены."
        )

# --- XP и кулдаун для обоих ---
    _, ch_new_level, ch_up = await add_xp(duel["challenger_id"], XP_PER_DUEL)
    _, tg_new_level, tg_up = await add_xp(duel["target_id"], XP_PER_DUEL)

    await set_duel_cooldown(duel["challenger_id"])
    await set_duel_cooldown(duel["target_id"])

    await update_duel_status(duel_id, "finished")

    # ... отправка result_text обоим игрокам ...

    try:
        await callback.message.answer(result_text, parse_mode="HTML")
    except TelegramBadRequest:
        pass

    try:
        await bot.send_message(duel["challenger_id"], result_text, parse_mode="HTML")
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
        await callback.answer("⏰ дуэль истекла.", show_alert=True)
        return

    if callback.from_user.id != duel["target_id"]:
        await callback.answer("это не твой вызов!", show_alert=True)
        return

    if duel["status"] != "pending":
        await callback.answer("дуэль уже обработана.", show_alert=True)
        return

    await callback.answer()
    await update_duel_status(duel_id, "declined")

    ch_name = await get_user_name(duel["challenger_id"]) or "Игрок"
    tg_name = await get_user_name(duel["target_id"]) or "Игрок"

    try:
        await callback.message.edit_text(f"❌ <b>{tg_name}</b> отклонил дуэль от <b>{ch_name}</b>.", parse_mode="HTML")
    except TelegramBadRequest:
        await callback.message.answer(f"❌ <b>{tg_name}</b> отклонил дуэль от <b>{ch_name}</b>.", parse_mode="HTML")

    try:
        await bot.send_message(duel["challenger_id"], f"❌ <b>{tg_name}</b> отклонил твою дуэль.", parse_mode="HTML")
    except Exception:
        pass

# --- Уведомления о простое бизнеса из-за пустого склада ---
EMPTY_STOCK_NOTIFY_AFTER = 60 * 60
EMPTY_STOCK_CHECK_INTERVAL = 5 * 60
BUSINESS_AUTO_SELL_AFTER = 5 * 86400
BUSINESS_REPAIR_COST_RATE = 0.20
BUSINESS_BREAK_CHECK_INTERVAL = 86400
BUSINESS_BREAK_CHANCE = 0.10

async def monitor_empty_businesses():
    """Проверяет простой и поломки. При простое без сырья 5 суток бизнес продаётся автоматически."""
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

                now = time.time()
                await settle_and_save_biz(user_id, biz)
                stock = int(biz.get("raw_stock", 0))

                # Ежедневная проверка вероятности поломки: 10% за сутки.
                last_check = float(biz.get("last_break_check", now))
                if not biz.get("broken") and stock > 0 and now - last_check >= BUSINESS_BREAK_CHECK_INTERVAL:
                    biz["last_break_check"] = now
                    if random.random() < BUSINESS_BREAK_CHANCE:
                        biz["broken"] = True
                        biz["broken_since"] = now
                        biz["last_collected"] = now
                        await save_biz(user_id, biz)
                        try:
                            await bot.send_message(user_id, f"🛠 Бизнес «{biz.get('name', 'бизнес')}» сломался!\nПочинка стоит 20% от стоимости: <b>{int(biz.get('price', 0) * BUSINESS_REPAIR_COST_RATE):,} ₽</b>.", parse_mode="HTML")
                        except Exception:
                            pass
                    else:
                        await save_biz(user_id, biz)

                if stock > 0:
                    biz.pop("empty_since", None)
                    biz.pop("empty_notified", None)
                    await save_biz(user_id, biz)
                    continue

                empty_since = biz.get("empty_since")
                if empty_since is None:
                    biz["empty_since"] = now
                    await save_biz(user_id, biz)
                    continue

                if now - float(empty_since) >= BUSINESS_AUTO_SELL_AFTER:
                    payout = int(biz.get("price", 0) * 0.5)
                    name = biz.get("name", "бизнес")
                    await add_to_balance(user_id, payout)
                    await save_biz(user_id, None)
                    try:
                        await bot.send_message(user_id, f"🏚 бизнес «{name}» забрало госсударство: он простаивал без сырья более 5 дней.\n💰 начислено 50% стоимости: <b>{payout:,} ₽</b>.", parse_mode="HTML")
                    except Exception:
                        pass
                    continue

                if now - float(empty_since) >= EMPTY_STOCK_NOTIFY_AFTER and not biz.get("empty_notified"):
                    try:
                        await bot.send_message(user_id, f"⚠️ твой «{biz.get('name', 'бизнес')}» простаивает! пополни склад, иначе через 5 дней его заберет государство.")
                    except Exception:
                        pass
                    biz["empty_notified"] = True
                    await save_biz(user_id, biz)

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка мониторинга бизнесов")

        await asyncio.sleep(EMPTY_STOCK_CHECK_INTERVAL)


# --- Награды топ-игрокам (баланс, рефералы, уровень) ---
async def _reward_one_top(config: dict, now: float):
    """Награждает топ игроков одного лидерборда, если пришло время."""
    last_ts_raw = await redis_client.get(config["last_ts_key"])
    last_ts = float(last_ts_raw) if last_ts_raw else 0

    if now - last_ts < TOP_REWARD_INTERVAL:
        return

    rewards = config["rewards"]
    top = await redis_client.zrevrange(config["key"], 0, len(rewards) - 1, withscores=True)

    for i, (uid_raw, _score) in enumerate(top, 1):
        reward = rewards.get(i)
        if not reward:
            continue
        try:
            user_id = int(uid_raw)
        except (TypeError, ValueError):
            continue

        new_balance = await add_to_balance(user_id, reward)
        name = await get_user_name(user_id) or "Игрок"

        try:
            await bot.send_message(
                user_id,
                f"🏆 ты в топ-{i} по {config['label']}!\n"
                f"{config['emoji']} награда: +<b>{reward:,} ₽</b>\n"
                f"💳 баланс: <b>{new_balance:,} ₽</b>",
                parse_mode="HTML",
            )
        except Exception:
            pass

        logger.info(f"награда за топ-{i} по {config['label']}: {name} ({user_id}) получил {reward}")

    await redis_client.set(config["last_ts_key"], str(now))


async def reward_top_players():
    """Раз в TOP_REWARD_INTERVAL награждает игроков из топа каждого лидерборда."""
    while True:
        try:
            now = time.time()
            for config in TOP_REWARD_CONFIGS:
                await _reward_one_top(config, now)

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка начисления наград за топ")

        await asyncio.sleep(TOP_REWARD_CHECK_INTERVAL)


# --- Универсальный хендлер ---
@router.message(F.text)
async def handle_unknown_text(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("используй кнопки😡\nчтобы переместиться в главное меню используй команду /menu")

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
    # --- Фоновая выдача наград топ-игрокам ---
    top_reward_task = asyncio.create_task(reward_top_players())

    try:
        logger.info("Бот запущен. Polling started.")
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        monitor_task.cancel()
        top_reward_task.cancel()
        for t in (monitor_task, top_reward_task):
            try:
                await t
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