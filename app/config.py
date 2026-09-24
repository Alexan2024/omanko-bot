import os
from pathlib import Path


def _req(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Не задана переменная окружения {name}")
    return value


def _hm(raw: str) -> tuple[int, int]:
    h, _, m = raw.strip().partition(":")
    return int(h), int(m or 0)


VERSION = "5.4"

BOT_TOKEN = _req("BOT_TOKEN")
ADMIN_ID = int(_req("ADMIN_ID"))
CHANNEL_ID = _req("CHANNEL_ID")  # @username или -100...
ANTHROPIC_API_KEY = _req("ANTHROPIC_API_KEY")

# ---------- модели ----------
# Оценка материала — Sonnet, первичный фильтр — Haiku, тексты больших постов и заметок — Opus.
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5")
TRIAGE_MODEL = os.getenv("TRIAGE_MODEL", "claude-haiku-4-5-20251001")
WRITER_MODEL = os.getenv("WRITER_MODEL", "claude-opus-5")
USE_BATCH = os.getenv("USE_BATCH", "1") == "1"   # фоновая оценка пакетами: вдвое дешевле, но не мгновенно

TZ_NAME = os.getenv("TZ_NAME", "Europe/Moscow")
SCORE_THRESHOLD = int(os.getenv("SCORE_THRESHOLD", "7"))


# ---------- слоты публикации ----------
def _parse_slots(raw: str) -> list[tuple[int, int, str]]:
    """'10:00=std,12:00=mini' → [(10, 0, 'std'), (12, 0, 'mini')]"""
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        t, _, fmt = part.partition("=")
        fmt = (fmt or "std").strip().lower()
        if fmt not in ("std", "mini"):
            raise RuntimeError(f"SLOTS: неизвестный формат «{fmt}» (нужно std или mini)")
        out.append((*_hm(t), fmt))
    return sorted(out)


# 5 мини + 2 больших: большие утром и вечером, мини между ними
SLOTS = _parse_slots(os.getenv(
    "SLOTS", "10:00=std,12:00=mini,13:30=mini,15:00=mini,16:30=mini,18:00=mini,20:00=std"))
SLOT_LEAD_MIN = int(os.getenv("SLOT_LEAD_MIN", "30"))     # автомат: за сколько минут до слота анонс
PLAN_TIME = _hm(os.getenv("PLAN_TIME", "21:00"))          # план на завтра и вечернее напоминание
AUTO_MIN_SCORE = int(os.getenv("AUTO_MIN_SCORE", "8"))    # автомат публикует сам только от этой оценки
MAX_OFFERS = int(os.getenv("MAX_OFFERS", "2"))            # сколько раз предлагать пост, прежде чем снять
INBOX_TTL_HOURS = int(os.getenv("INBOX_TTL_HOURS", "48")) # сколько пост ждёт решения во входящих
SEMI_FALLBACK = os.getenv("SEMI_FALLBACK", "1").strip().lower() not in ("0", "off", "false", "no")  # страховка полуавтомата
FIND_EVERY_DAYS = int(os.getenv("FIND_EVERY_DAYS", "1"))  # слот находки: каждый день (1), через день (2)…

# Ручной режим: когда класть посты во входящие и сколько в день
DAILY_MAX = int(os.getenv("DAILY_MAX", "10"))
DELIVERY_HOURS = [int(h) for h in os.getenv("DELIVERY_HOURS", "10,14,19").split(",") if h.strip()]

# ---------- сбор и оценка ----------
COLLECT_TIMES = [_hm(t) for t in os.getenv("COLLECT_TIMES", "06:00,16:00").split(",") if t.strip()]
STOCK_DAYS = float(os.getenv("STOCK_DAYS", "2"))            # запас готовых постов — на сколько дней слотов
MAX_PER_RUN = int(os.getenv("MAX_PER_RUN", "12"))           # сколько материалов оценивать за проход
TRIAGE_MAX = int(os.getenv("TRIAGE_MAX", "80"))             # сколько новых материалов фильтровать за проход
REPEAT_DAYS = int(os.getenv("REPEAT_DAYS", "365"))       # один и тот же объект снова — не раньше, дней
CANDIDATE_TTL_DAYS = int(os.getenv("CANDIDATE_TTL_DAYS", "10"))  # отобранное, но не оценённое — сколько живёт
EVAL_PHOTOS = int(os.getenv("EVAL_PHOTOS", "8"))            # сколько фото смотрит Claude при оценке
THUMB_SIZE = int(os.getenv("THUMB_SIZE", "480"))            # размер превью для Claude, px
EVAL_TEXT_CHARS = int(os.getenv("EVAL_TEXT_CHARS", "3500")) # сколько текста статьи уходит на оценку

# ---------- расход ----------
DAILY_BUDGET_USD = float(os.getenv("DAILY_BUDGET_USD", "1.0"))  # потолок фоновых трат в сутки, $
DAILY_API_CALLS_MAX = int(os.getenv("DAILY_API_CALLS_MAX", "150"))
NOTES_WEB_SEARCHES = int(os.getenv("NOTES_WEB_SEARCHES", "6"))

# ---------- музейные коллекции ----------
MUSEUM_PER_RUN = int(os.getenv("MUSEUM_PER_RUN", os.getenv("MET_PER_RUN", "2")))  # объектов на музей за сбор
MUSEUM_DAILY_MAX = int(os.getenv("MUSEUM_DAILY_MAX", "3"))                        # музейных постов в день

# ---------- фото ----------
MIN_LONG_SIDE = int(os.getenv("MIN_LONG_SIDE", "1200"))
MIN_SHORT_SIDE = int(os.getenv("MIN_SHORT_SIDE", "700"))
MIN_PHOTOS_ARTICLE = int(os.getenv("MIN_PHOTOS_ARTICLE", "3"))   # для большого поста
MIN_PHOTOS_MINI = int(os.getenv("MIN_PHOTOS_MINI", "1"))         # для мини-поста
MAX_PHOTOS = 10
MINI_MAX_PHOTOS = int(os.getenv("MINI_MAX_PHOTOS", "4"))
CAPTION_LIMIT = 1024  # лимит подписи к фото в Telegram
MESSAGE_LIMIT = 4096
IMAGE_TTL_DAYS = int(os.getenv("IMAGE_TTL_DAYS", "14"))

# Недельный дайджест
DIGEST_DOW = os.getenv("DIGEST_DOW", "sun")
DIGEST_HOUR = int(os.getenv("DIGEST_HOUR", "20"))

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "ahmag.db"
IMG_DIR = DATA_DIR / "images"
ASSETS_DIR = Path(__file__).resolve().parent.parent / "data"
PROFILE_PATH = ASSETS_DIR / "ahmag_taste_profile.md"
ARCHIVE_PATH = ASSETS_DIR / "ahmag_posts.json"

# ---------- рубрики ----------
CATEGORIES = {
    "architecture": "Архитектура",   # вместе с интерьерами
    "art": "Искусство",              # вместе со скульптурой, инсталляциями, выставками
    "photography": "Фотография",
    "archive": "Архив",
    "cinema": "Кино",
}
CATEGORY_ALIASES = {
    "interiors": "architecture", "interior": "architecture",
    "sculpture": "art", "installation": "art", "exhibition": "art", "museum": "art", "design": "art",
    "photo": "photography", "film": "cinema",
}


def _parse_mix(raw: str) -> dict[str, float]:
    mix = {}
    for part in raw.split(","):
        k, _, v = part.partition("=")
        if k.strip() in CATEGORIES and v.strip():
            mix[k.strip()] = float(v)
    total = sum(mix.values()) or 1
    return {k: v / total for k, v in mix.items()}


# Архитектура — половина ленты, всё остальное — вторая половина
TARGET_MIX = _parse_mix(os.getenv(
    "MIX", "architecture=0.50,art=0.20,photography=0.15,archive=0.10,cinema=0.05"))

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
