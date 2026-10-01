import os
import io
import re
import json
import math
import asyncio
import logging
import secrets
import unicodedata
from datetime import datetime, time as dtime, timezone, timedelta
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageCms, features
import pillow_avif  # noqa: F401 — регистрирует AVIF-декодер в Pillow
try:
    # HEIC/HEIF (оригиналы с iPhone, присланные файлом). Если пакета нет —
    # бот работает как раньше, просто HEIC не откроется.
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIF_OK = True
except Exception:
    HEIF_OK = False
try:
    # SVG-логотипы партнёров. Нужен системный libcairo2 (он в Dockerfile).
    # Если его нет — бот работает, просто попросит PNG вместо SVG.
    import cairosvg
    SVG_OK = True
except Exception:
    SVG_OK = False
import numpy as np
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                      InputMediaPhoto, InputMediaDocument)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, ConversationHandler,
    TypeHandler, BaseUpdateProcessor
)
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut
from telegram.helpers import escape_markdown

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("BOT_TOKEN")

# ============ Состояния диалога ============
CHOOSING_TYPE = 0
WAITING_PHOTOS = 1
CHOOSING_FORMAT = 2
CHOOSING_HASHTAG = 3
WAITING_TITLE = 4
CHOOSING_CHANNEL = 5
WAITING_PARTNER_LOGO = 6
WAITING_CUSTOM_HASHTAG = 7
WAITING_STORE_TEXT = 8
CHOOSING_STORE_COLOR = 9
STORE_COLOR_SLIDER = 10
COVER_DARK_SLIDER = 11
CHOOSING_PARTNER = 12
WAITING_PARTNER_NAME = 13
PARTNER_TUNE = 14
CHOOSING_COLLAB_KIND = 15
COLLAB_COLOR = 16
PARTNER_MANAGE = 17

# Состояния рассылки (отдельный диалог, значения не пересекаются с основным)
BROADCAST_MSG = 100
BROADCAST_CONFIRM = 101

BASE = os.path.dirname(os.path.abspath(__file__))

# ============ Рассылка: админ и хранилище пользователей ============
# ID администратора (только он может слать рассылку). Берётся из переменной
# окружения ADMIN_ID в Railway. Свой ID можно узнать командой /myid.
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)

# Скрытые команды подписки на еженедельный отчёт. Нигде в меню не светятся —
# раздаёшь их вручную тем, кому нужен доступ. Можно переименовать через
# переменные окружения в Railway (Telegram-команды: латиница/цифры/нижнее
# подчёркивание). Если секрет «утёк» — просто поменяй имя команды.
SUBSCRIBE_CMD = os.environ.get("STATS_SUBSCRIBE_CMD", "stats_on")
UNSUBSCRIBE_CMD = os.environ.get("STATS_UNSUBSCRIBE_CMD", "stats_off")


def _resolve_data_dir():
    """Где хранить users.json и stats.json так, чтобы пережило передеплой.
    Railway сам выставляет RAILWAY_VOLUME_MOUNT_PATH, когда к сервису подключён
    Volume — это самый надёжный признак постоянного хранилища. Если его нет,
    пробуем /data (на случай ручного монтирования), иначе пишем рядом с ботом —
    но это эфемерно: при следующем деплое всё обнулится.
    Возвращает (папка, постоянное_ли)."""
    vol = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    candidates = ([(vol, True)] if vol else []) + [("/data", False), (BASE, False)]
    for d, persistent in candidates:
        try:
            if os.path.isdir(d) and os.access(d, os.W_OK):
                return d, persistent
        except Exception:
            pass
    return BASE, False


DATA_DIR, STORAGE_PERSISTENT = _resolve_data_dir()
USERS_FILE = os.path.join(DATA_DIR, "users.json")
STATS_FILE = os.path.join(DATA_DIR, "stats.json")
SUBS_FILE = os.path.join(DATA_DIR, "stats_subs.json")
LAST_FILE = os.path.join(DATA_DIR, "last_settings.json")   # «🔁 Ещё раз» переживает деплой
PARTNERS_FILE = os.path.join(DATA_DIR, "partners.json")    # пресеты коллабов (общие)
PARTNERS_DIR = os.path.join(DATA_DIR, "partners")          # их логотипы


def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, type(default)) else default
    except Exception:
        return default


def save_json(path: str, data) -> None:
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        logger.error(f"Не смог сохранить {os.path.basename(path)}: {e}")


def load_users() -> set:
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return set(int(x) for x in json.load(f))
    except Exception:
        return set()


def save_users(users) -> None:
    try:
        tmp = USERS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(users), f)
        os.replace(tmp, USERS_FILE)
    except Exception as e:
        logger.error(f"Не смог сохранить список пользователей: {e}")


def add_user(chat_id: int) -> None:
    users = load_users()
    if chat_id not in users:
        users.add(chat_id)
        save_users(users)


def remove_users(ids) -> None:
    users = load_users()
    users -= set(ids)
    save_users(users)


# ============ Статистика производства ============
# Один завершённый цикл (нажал /start → выбрал тип/канал → прислал фото →
# получил картинки) = один «пост». В цикле может быть несколько фото — это
# «обработанные фотографии». Каждое событие пишем в stats.json одной строкой:
# дата (UTC, ISO), канал, режим (type1/cover), сколько фото реально обработано.

MSK = timezone(timedelta(hours=3))  # Москва — UTC+3, без переходов на летнее
REPORT_HOUR_MSK = 19  # час отправки еженедельного отчёта по пятницам (МСК)
_STATS_CAP = 5000  # держим файл в узде: храним последние N событий
_RU_MONTHS = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def load_subscribers() -> set:
    try:
        with open(SUBS_FILE, "r", encoding="utf-8") as f:
            return set(int(x) for x in json.load(f))
    except Exception:
        return set()


def save_subscribers(subs) -> None:
    try:
        tmp = SUBS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(subs), f)
        os.replace(tmp, SUBS_FILE)
    except Exception as e:
        logger.error(f"Не смог сохранить подписчиков статистики: {e}")


def load_stats() -> list:
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def save_stats(events) -> None:
    try:
        tmp = STATS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(events, f, ensure_ascii=False)
        os.replace(tmp, STATS_FILE)
    except Exception as e:
        logger.error(f"Не смог сохранить статистику: {e}")


def record_post(channel: str, mode: str, n_photos: int,
                partner: str = None, kind: str = None) -> None:
    if n_photos <= 0:
        return
    events = load_stats()
    ev = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "channel": channel,
        "mode": mode if mode in ("type1", "cover", "store", "collab") else "type1",
        "photos": int(n_photos),
    }
    if partner:
        ev["partner"] = partner      # имя партнёра коллаба
    if kind:
        ev["kind"] = kind            # коллаб: type1 (брендинг) | cover (обложка)
    events.append(ev)
    if len(events) > _STATS_CAP:
        events = events[-_STATS_CAP:]
    save_stats(events)


def _plural_post(n: int) -> str:
    n100, n10 = abs(n) % 100, abs(n) % 10
    if 11 <= n100 <= 14:
        return "постов"
    if n10 == 1:
        return "пост"
    if 2 <= n10 <= 4:
        return "поста"
    return "постов"


def _ru_date(d: datetime) -> str:
    return f"{d.day} {_RU_MONTHS[d.month]}"


def build_weekly_report(events, until=None) -> str:
    """Сводка за 7 дней до момента until (по МСК): всего и по каналам +
    разбивка фото по типам."""
    until = until or datetime.now(MSK)
    since = until - timedelta(days=7)

    chans = {k: {"posts": 0, "photos": 0} for k in CHANNELS}
    total_posts = total_photos = type1_photos = cover_photos = 0
    store_photos = collab_photos = 0
    partners = {}

    for e in events:
        try:
            ts = datetime.fromisoformat(e["ts"])
        except Exception:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts = ts.astimezone(MSK)
        if not (since < ts <= until):
            continue
        ch = e.get("channel", "base")
        if ch not in chans:
            ch = "base"
        ph = int(e.get("photos", 0))
        chans[ch]["posts"] += 1
        chans[ch]["photos"] += ph
        total_posts += 1
        total_photos += ph
        mode = e.get("mode")
        if mode == "cover":
            cover_photos += ph
        elif mode == "store":
            store_photos += ph
        elif mode == "collab":
            collab_photos += ph
            if e.get("partner"):
                partners[e["partner"]] = partners.get(e["partner"], 0) + 1
        else:
            type1_photos += ph

    period = f"{_ru_date(since)} — {_ru_date(until)}"
    if total_posts == 0:
        return (f"📊 *Итоги недели* ({period})\n\n"
                "Тишина в эфире — за неделю ни одного поста. "
                "Контент сам себя не сделает 😉")

    lines = [
        f"📊 *Итоги недели* ({period})",
        "",
        f"🔥 Всего: *{total_posts}* {_plural_post(total_posts)} · "
        f"*{total_photos}* фото",
        "",
        "*По каналам:*",
    ]
    for k in CHANNELS:
        c = chans[k]
        if c["posts"] == 0:
            continue
        lines.append(f"• {CHANNELS[k]['title']}: "
                     f"{c['posts']} {_plural_post(c['posts'])}, {c['photos']} фото")
    lines += [
        "",
        "*По типам (фото):*",
        f"🏷 Брендинг — {type1_photos}",
        f"🖼 Обложка — {cover_photos}",
        f"🤝 Коллаб — {collab_photos}" + (
            " (" + ", ".join(f"{escape_markdown(n, version=1)} ×{c}" for n, c in
                             sorted(partners.items(), key=lambda kv: -kv[1])[:5]) + ")"
            if partners else ""),
        f"🛍 STORE — {store_photos}",
    ]
    return "\n".join(lines)


# ============ Визуальная карточка статистики ============
# Одна вертикальная PNG: бары по неделям + две тепловые карты (каналы × час
# дня, каналы × день недели). Источник — те же события stats.json, что и у
# текстового отчёта. Рисуем Pillow'ом (он и так в стеке), шрифты — Nunito из
# репо через load_black/load_semibold. Если событий нет — возвращаем None,
# и тогда карточку просто не отправляем.

_STAT_CH_COLORS = {
    "base":   (236, 238, 242),
    "news":   (90, 156, 255),
    "girls": (255, 110, 196),
    "music":  (255, 176, 60),
    "agency": (60, 214, 180),
    "gastro": (255, 96, 96),
    "dom":    (168, 130, 255),
}
_STAT_BG = (18, 20, 25)
_STAT_CARD = (24, 27, 33)
_STAT_GRID = (44, 48, 56)
_STAT_INK = (235, 237, 240)
_STAT_MUTED = (140, 146, 156)
_STAT_ACCENT = (255, 176, 60)
_STAT_WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _stat_events_msk(events):
    """События с валидным ts -> список (ts_msk, channel, mode, photos)."""
    out = []
    for e in events:
        try:
            ts = datetime.fromisoformat(e["ts"])
        except Exception:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts = ts.astimezone(MSK)
        ch = e.get("channel", "base")
        if ch not in CHANNELS:
            ch = "base"
        out.append((ts, ch, e.get("mode", "type1"), int(e.get("photos", 0))))
    return out


def _stat_rrect(d, box, r, fill):
    """rounded_rectangle с защитой от слишком большого радиуса (Pillow ругается,
    если радиус больше половины меньшей стороны)."""
    x0, y0, x1, y1 = box
    if x1 <= x0 or y1 <= y0:
        return
    r = max(0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    d.rounded_rectangle(box, radius=r, fill=fill)


def _stat_draw_bars_legend(d, x0, y, w):
    """Экспликация для бар-чарта: цветной свотч + название канала.
    Одна горизонтальная строка по центру; если не влезает по ширине —
    автоматически переносится на 2 ряда (по 3 элемента)."""
    font = load_semibold(17)
    swatch = 14
    text_gap = 8   # между свотчем и текстом
    item_gap = 22  # между элементами
    items = []
    for key in CHANNELS:
        title = CHANNELS[key]["title"]
        try:
            tw = int(d.textlength(title, font=font))
        except Exception:
            tw = len(title) * 10
        items.append((key, title, tw))

    def _row_width(row):
        if not row:
            return 0
        return (sum(swatch + text_gap + tw for _, _, tw in row)
                + item_gap * (len(row) - 1))

    if _row_width(items) <= w:
        rows = [items]
    else:
        half = (len(items) + 1) // 2
        rows = [items[:half], items[half:]]

    row_h = 22
    for ri, row in enumerate(rows):
        rw = _row_width(row)
        cx = x0 + max(0, (w - rw) // 2)
        ry = y + ri * row_h
        for key, title, tw in row:
            color = _STAT_CH_COLORS.get(key, _STAT_INK)
            _stat_rrect(d, (cx, ry + 2, cx + swatch, ry + swatch + 2), 3, color)
            d.text((cx + swatch + text_gap, ry - 2), title,
                   font=font, fill=_STAT_INK)
            cx += swatch + text_gap + tw + item_gap


def _stat_draw_bars(d, x0, y0, w, h, msk_events, until):
    """Stacked-бары: посты по неделям (8 недель) с разбивкой по каналам."""
    d.text((x0, y0), "Посты по неделям", font=load_black(34), fill=_STAT_INK)
    d.text((x0, y0 + 40), "последние 8 недель · по каналам",
           font=load_semibold(19), fill=_STAT_MUTED)
    # Экспликация: цвет → канал. Без неё юзеру приходилось гадать,
    # какой бар чему соответствует.
    _stat_draw_bars_legend(d, x0, y0 + 74, w)
    weeks = 8
    lbl = load_semibold(17)
    buckets = [{k: 0 for k in CHANNELS} for _ in range(weeks)]
    start = until - timedelta(days=7 * weeks)
    for ts, ch, _mode, _ph in msk_events:
        if ts <= start or ts > until:
            continue
        idx = int((ts - start).days // 7)
        idx = max(0, min(idx, weeks - 1))
        buckets[idx][ch] += 1
    totals = [sum(b.values()) for b in buckets]
    maxtot = max(totals) if any(totals) else 1
    plot_top = y0 + 114  # сдвинуто вниз, чтобы освободить место под экспликацию
    plot_bot = y0 + h - 40
    plot_h = plot_bot - plot_top
    for g in range(5):
        gy = plot_bot - plot_h * g / 4
        d.line((x0, gy, x0 + w, gy), fill=_STAT_GRID, width=1)
        d.text((x0 - 8, gy - 9), str(round(maxtot * g / 4)),
               font=lbl, fill=_STAT_MUTED, anchor="ra")
    bw = w / weeks
    barw = bw * 0.56
    for wi in range(weeks):
        cx = x0 + bw * (wi + 0.5)
        yb = plot_bot
        for key in CHANNELS:
            v = buckets[wi][key]
            if v <= 0:
                continue
            bh = plot_h * v / maxtot
            _stat_rrect(d, (cx - barw / 2, yb - bh, cx + barw / 2, yb), 4,
                        _STAT_CH_COLORS.get(key, _STAT_INK))
            yb -= bh + 2
        wd = start + timedelta(days=7 * (wi + 1) - 1)
        d.text((cx, plot_bot + 10), wd.strftime("%d.%m"),
               font=lbl, fill=_STAT_MUTED, anchor="ma")


def _stat_draw_heat(d, x0, y0, w, h, msk_events, mode, title_txt, sub_txt):
    """Тепловая карта каналы × время. mode='hour' (24 колонки) | 'weekday' (7)."""
    d.text((x0, y0), title_txt, font=load_black(34), fill=_STAT_INK)
    d.text((x0, y0 + 40), sub_txt, font=load_semibold(19), fill=_STAT_MUTED)
    ncols = 24 if mode == "hour" else 7
    rows = list(CHANNELS.keys())
    grid = [[0] * ncols for _ in rows]
    for ts, ch, _mode, _ph in msk_events:
        ci = rows.index(ch) if ch in rows else 0
        col = ts.hour if mode == "hour" else ts.weekday()
        grid[ci][col] += 1
    maxv = max((max(r) for r in grid), default=0) or 1
    lbl = load_semibold(17)
    sm = load_semibold(14)
    label_w = 150
    gx0 = x0 + label_w
    gy0 = y0 + 86
    cell = (w - label_w) / ncols
    ch_h = 40
    for ci, key in enumerate(rows):
        ry = gy0 + ci * ch_h
        d.text((gx0 - 14, ry + ch_h / 2 - 11), CHANNELS[key]["title"],
               font=lbl, fill=_STAT_INK, anchor="ra")
        for c in range(ncols):
            t = (grid[ci][c] / maxv) ** 0.7 if maxv else 0
            cc = tuple(int(_STAT_CARD[i] + (_STAT_ACCENT[i] - _STAT_CARD[i]) * t)
                       for i in range(3))
            cx = gx0 + c * cell
            _stat_rrect(d, (cx + 1, ry + 1, cx + cell - 1, ry + ch_h - 3), 3, cc)
    axis_y = gy0 + len(rows) * ch_h + 6
    if mode == "hour":
        for c in range(ncols):
            if c % 3 == 0:
                d.text((gx0 + c * cell + cell / 2, axis_y), f"{c:02d}",
                       font=sm, fill=_STAT_MUTED, anchor="ma")
    else:
        for c in range(ncols):
            d.text((gx0 + c * cell + cell / 2, axis_y), _STAT_WEEKDAYS[c],
                   font=sm, fill=_STAT_MUTED, anchor="ma")


def render_stats_card(events, until=None):
    """Единая PNG-карточка (бары + 2 тепловые карты). Возвращает PNG-байты
    или None, если валидных событий нет."""
    msk_events = _stat_events_msk(events)
    if not msk_events:
        return None
    until = until or datetime.now(MSK)
    W = 1000
    pad = 56
    inner = W - pad * 2
    n_ch = len(CHANNELS)
    h_bars = 400  # 360 → 400: +40px на строку легенды над графиком
    h_heat = n_ch * 40 + 130
    gap = 36
    H = pad + h_bars + gap + h_heat + gap + h_heat + pad
    img = Image.new("RGB", (W, H), _STAT_BG)
    d = ImageDraw.Draw(img)
    _stat_rrect(d, (pad - 24, pad - 24, W - (pad - 24), H - (pad - 24)), 28, _STAT_CARD)
    y = pad
    _stat_draw_bars(d, pad, y, inner, h_bars, msk_events, until)
    y += h_bars + gap
    _stat_draw_heat(d, pad, y, inner, h_heat, msk_events, "hour",
                    "Когда постим", "каналы × час дня (МСК) · по всей истории")
    y += h_heat + gap
    _stat_draw_heat(d, pad, y, inner, h_heat, msk_events, "weekday",
                    "Дни недели", "каналы × день недели (МСК) · по всей истории")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue()


FORMATS = {
    "4:5": (1920, 2400),
    "2:3": (1920, 2880),
    "1:1": (1920, 1920),
    "3:2": (1920, 1280),
    "Адаптивный": None,
}

# Кнопка «без хештега» (значение-метка, проверяется в рендере) и метка «свой хештег»
NO_HASHTAG = "— Без хештега —"
CUSTOM_HASHTAG_CB = "__custom__"

# Общий набор тегов для всех каналов
COMMON_HASHTAGS = [
    "#art", "#archives", "#community", "#item", "#paper",
    "#space", "#style", "#cinema", "#architecture", "#cars", "#fashion"
]

# Хештеги по каналам: общий набор, у гастро — общий + четыре своих.
# Править теги в одном месте: общий — в COMMON_HASHTAGS, гастро-специфику — в его строке.
CHANNEL_HASHTAGS = {
    "base":   COMMON_HASHTAGS,
    "news":   COMMON_HASHTAGS,
    "girls": COMMON_HASHTAGS,
    "music":  COMMON_HASHTAGS,
    "agency": COMMON_HASHTAGS,
    "gastro": COMMON_HASHTAGS + ["#books", "#recommendation", "#interior", "#movies", "#moscow"],
    "dom":    COMMON_HASHTAGS,
}

# ============ Тип 1 (брендинг) — БЕЗ ИЗМЕНЕНИЙ ============
LOGO_W = 56
LOGO_H = 71
LOGO_LEFT = 92
LOGO_BOTTOM = 70
HASHTAG_RIGHT = 80
HASHTAG_BOTTOM = 79
HASHTAG_SIZE = 51
BRIGHTNESS_OFFSET = 45
ALPHA = 0.95

# ============ Коллаборация — параметры ============
# Нижняя строка «ÖMANKÖ × партнёр». Координаты в опорных единицах канваса 1920px.
COLLAB_WORDMARK_W = 327          # полный вордмарк ÖMANKÖ (ширина)
COLLAB_WORDMARK_H = 71           # высота (нативные пропорции вордмарка: 71/327 ≈ 0.219)
COLLAB_GAP = 43                  # пропуск между элементами (лого — × — лого)
COLLAB_X_GLYPH = "×"             # разделитель. Поставь "x", если нужна именно буква
COLLAB_X_SIZE = HASHTAG_SIZE     # 51 — шрифт/размер как у хештегов в брендинге
COLLAB_PARTNER_H = 58            # высота лого партнёра (ширина — пропорционально)
COLLAB_BOTTOM = LOGO_BOTTOM      # 70 — отступ снизу как у брендинга
COLLAB_WORDMARK_CENTER_DROP = 9  # центр вордмарка для выравнивания — на 9px ниже геометрического
# Цвет строки: True — адаптивный под фон (как брендинг); False — всегда белый
COLLAB_ADAPTIVE = True

# ============ Обложка — параметры ============
# Заголовок (Nunito Sans Black)
COVER_TITLE_SIZE_FEED = 135      # абсолютный размер на всех соотношениях ленты
COVER_TITLE_LS_FEED = -0.03      # letter-spacing -3%
COVER_TITLE_SIZE_STORY = 77      # на канвас 1080x1920
COVER_TITLE_LS_STORY = -0.06     # letter-spacing -6%
COVER_TITLE_BOTTOM_IG = 452      # 424 + 28 (поднят выше)
COVER_TITLE_BOTTOM_TG = 382      # 354 + 28 (поднят выше)
COVER_LINE_SPACING = 1.08        # межстрочный множитель
TITLE_SIDE_MARGIN = 0.06         # мин. поле слева/справа для заголовка и бабла (доля ширины)
CUSTOM_HASHTAG_MAX = 24          # макс. длина своего хештега (без #)

# Вордмарк ÖMANKÖ (всегда белый)
WORDMARK_W_FEED = 326            # низ ленты, отступ снизу 65
WORDMARK_BOTTOM_FEED = 65
WORDMARK_W_STORY = 195           # верх сторис, отступ сверху 168
WORDMARK_TOP_STORY = 168

# Бабл с хештегом
BUBBLE_TEXT_SIZE = HASHTAG_SIZE  # 51 — как в обычных постах
# Лента: бабл сверху по центру
FEED_BUBBLE_PAD_X = 48           # горизонтальный паддинг текста в бабле (лента)
# Сторис IG: бабл под заголовком
IG_BUBBLE_BOTTOM = 215
IG_BUBBLE_W = 387
IG_BUBBLE_H = 135
IG_BUBBLE_RADIUS = 41
# Сторис TG: бабл под заголовком
TG_BUBBLE_BOTTOM = 161
TG_BUBBLE_W = 430
TG_BUBBLE_H = 115
TG_BUBBLE_RADIUS = 17

# Бабл в ленте: тёмный, почти непрозрачный, с хештегом внутри
FEED_BUBBLE_ALPHA = 0.85
FEED_BUBBLE_FILL = (0, 0, 0)
# Бабл в сторис: ПУСТОЙ (без хештега), цвет инвертный к фону:
#   тёмный фон → светлый бабл, светлый фон → тёмный бабл
STORY_BUBBLE_ALPHA = 0.50

# Градиент под заголовком: чёрный снизу вверх, адаптивный
GRAD_ALPHA_DARK = 0.18           # фон тёмный → слабый градиент
GRAD_ALPHA_LIGHT = 0.62          # фон светлый → плотный
GRAD_ALPHA_CEIL = 0.99           # потолок плотности (почти полная чернота на максимуме)
GRAD_RISE_STORY = 900            # высота градиента над низом (на 1080w)

# Ручной регулятор затемнения обложек: число = СДВИГ плотности относительно
# адаптивной базы (1.0 = база без сдвига = текущее поведение). Сдвиг работает
# одинаково сильно и на тёмном, и на светлом фоне — в отличие от множителя.
DARK_LEVELS = [0.4, 0.7, 1.0, 1.4, 1.8]
DARK_DEFAULT_IDX = 2
DARK_LEVEL_NAMES = ["min", "светлее", "норма", "темнее", "max"]

STORY_SIZE = (1080, 1920)

# Пер-ратио геометрия обложек (ленты). Размеры абсолютные на своём канвасе.
# bubble_top — отступ бабла от верха; title_bottom — отступ заголовка от низа.
COVER_FORMATS = {
    "4:5": dict(size=(1920, 2400), bubble_h=126, bubble_top=68, title_bottom=365),
    "3:4": dict(size=(1920, 2560), bubble_h=126, bubble_top=68, title_bottom=385),
    "1:1": dict(size=(2400, 2400), bubble_h=158, bubble_top=85, title_bottom=411),
    "3:2": dict(size=(3600, 2400), bubble_h=126, bubble_top=68, title_bottom=440),
}
# Кнопка раньше называлась «2:3», хотя холст 1920×2560 — это 3:4. Старое имя
# понимаем (например, в сохранённых настройках повтора).
COVER_KEY_ALIASES = {"2:3": "3:4"}


def cover_key(format_key: str) -> str:
    return COVER_KEY_ALIASES.get(format_key, format_key)


def nearest_cover_spec(w: int, h: int) -> dict:
    """Фиксированный формат обложки с ближайшими к холсту пропорциями —
    по нему адаптивная обложка берёт отступы и масштабирует их."""
    r = w / h
    return min(COVER_FORMATS.values(),
               key=lambda sp: abs(math.log(r * sp["size"][1] / sp["size"][0])))

# ============ ÖMANKÖ STORE ============
# Витрина магазина: фикс. вертикаль 2000×2500, фон-фото (cover-fit),
# угловой Ö (адаптивный цвет, как в брендинге) + подпись в 2 строки тем же
# цветом. Шрифт Nunito BOLD (не Sans), мелкий. Все значения — абсолютные px
# на холсте 2000×2500.
STORE_SIZE = (2000, 2500)
STORE_LOGO_W = 59
STORE_LOGO_H = 74
STORE_LOGO_LEFT = 95            # отступ лого слева
STORE_LOGO_BOTTOM = 76          # отступ лого снизу
STORE_TEXT_GAP = 20            # отступ текста от правого края лого
STORE_TEXT_SIZE = 15.62        # кегль подписи (Nunito Bold)
STORE_TEXT_BOTTOM = 86          # низ нижней строки от низа холста
STORE_LINE_HEIGHT = 0.9        # межстрочный интервал = 90% кегля

# Цвет графики STORE: None — адаптивный (как в брендинге), либо фикс. (r,g,b).
STORE_COLOR_LIGHT = (0xE4, 0xE4, 0xE5)   # светлый пресет #E4E4E5
STORE_COLOR_DARK = (0x68, 0x68, 0x68)    # тёмный пресет  #686868
# ЧБ-слайдер: позиции 0..STORE_GRAY_STEPS, белый (255) → чёрный (0).
STORE_GRAY_STEPS = 14
STORE_GRAY_DEFAULT_IDX = 7

# ============ Каналы сетки ============
# У каждого канала ДВА варианта лого (белые PNG, прозрачный фон):
#   type1_logo  — для режима «Тип 1» (угловой логотип внизу слева)
#   story_logo  — для обложек, используется ТОЛЬКО в сторис (IG/TG)
# Геометрия:
#   type1_box = (w, h, left, bottom) в координатах канваса 1920px (как LOGO_*),
#               масштабируется вместе с лентой.
#   story_box = (w, h) в координатах сторис 1080×1920; отступ сверху общий
#               (WORDMARK_TOP_STORY), лого центрируется по горизонтали.
# None в поле лого/бокса => базовое поведение:
#   type1 None  -> рисуем векторный Ö (адаптивный, размеры LOGO_*)
#   story None  -> широкий вордмарк ÖMANKÖ (как раньше)
# В ЛЕНТЕ обложки вордмарк ВСЕГДА базовый ÖMANKÖ (по каналу не меняется).
CHANNELS = {
    "base":   {"title": "основа ÖMANKÖ",
               "type1_logo": None, "type1_box": None,
               "story_logo": None, "story_box": None},
    "news":   {"title": "Ö NEWS",
               "type1_logo": "logo_type1_news.png",   "type1_box": (72, 112, 91, 47),
               "story_logo": "logo_cover_news.png",    "story_box": (196, 81)},
    "girls": {"title": "Ö GIRLS",
               "type1_logo": "logo_type1_girls.png", "type1_box": (79, 102, 91, 42),
               "story_logo": "logo_cover_girls.png",  "story_box": (196, 81)},
    "music":  {"title": "Ö MUSIC",
               "type1_logo": "logo_type1_music.png",  "type1_box": (89, 107, 91, 52),
               "story_logo": "logo_cover_music.png",   "story_box": (196, 81)},
    "agency": {"title": "Ö AGENCY",  # спека пока не задана — базовое поведение
               "type1_logo": None, "type1_box": None,
               "story_logo": None, "story_box": None},
    "gastro": {"title": "Ö GASTRO",
               "type1_logo": "logo_type1_gastro.png", "type1_box": (75, 119, 91, 40),
               "story_logo": "logo_cover_gastro.png",  "story_box": (196, 81)},
    "dom":    {"title": "Ö DÖM",  # обложки пока не заданы — в сторис базовый вордмарк
               "type1_logo": "logo_dom.png", "type1_box": (198, 90, 78, 52),
               "story_logo": None, "story_box": None},
}

BASE_WORDMARK_FILE = "wordmark_white.png"  # широкий ÖMANKÖ: лента + база сторис
BASE_O_LOGO_FILE = "main_o.png"            # угловой логотип Ö (брендинг базы/agency + стор)

# Кэши: базовый вордмарк и логотипы каналов (грузим с диска один раз)
_WORDMARK_CACHE = {}
_LOGO_CACHE = {}


def _load_logo(fname: str):
    """Загрузка PNG-логотипа канала с кэшем. None, если файла нет."""
    if fname in _LOGO_CACHE:
        return _LOGO_CACHE[fname]
    path = os.path.join(BASE, fname)
    if not os.path.exists(path):
        logger.warning("Лого канала '%s' не найдено — откат на базовое поведение", fname)
        _LOGO_CACHE[fname] = None
        return None
    img = Image.open(path).convert("RGBA")
    _LOGO_CACHE[fname] = img
    return img


# ============ Открытие фото: ориентация + цветовой профиль ============
_SRGB_PROFILE = ImageCms.createProfile("sRGB")


def open_photo(data: bytes, need=None) -> Image.Image:
    """Открывает присланное фото и приводит к «честному» RGB в sRGB.

    need — список размеров холстов (w, h), под которые будем рендерить. Если
    фото — большой JPEG, декодер сразу читает его уменьшенным в 2/4/8 раз, но
    не меньше любого из холстов (draft-режим libjpeg). Это в разы быстрее и
    вдвое экономнее по памяти, а качество не страдает: дальше всё равно идёт
    LANCZOS до точного размера. None — читать как есть (адаптивный формат).

    1) EXIF-ориентация: файлы с телефона хранят поворот в метаданных, Pillow
       его сам не применяет — без этого вертикальный кадр мог лечь на бок.
    2) Цветовой профиль: iPhone снимает в Display P3. Простой convert("RGB")
       отбрасывает профиль, и цвета тускнеют/сдвигаются. Здесь переводим
       пиксели из встроенного профиля в sRGB (стандарт для веба и Telegram).
    Любая ошибка на шаге 2 — тихий откат к прежнему поведению."""
    img = Image.open(io.BytesIO(data))
    if need and img.format == "JPEG":
        try:
            w, h = img.size
            ori = img.getexif().get(0x0112, 1)
            dw, dh = (h, w) if ori in (5, 6, 7, 8) else (w, h)  # как увидим после поворота
            s = max(max(cw / dw, ch / dh) for cw, ch in need)
            if s <= 0.5:  # смысл есть, только если можно уменьшить хотя бы вдвое
                img.draft(img.mode, (math.ceil(w * s), math.ceil(h * s)))
        except Exception as e:
            logger.warning(f"draft-декодирование не применилось: {e}")
    try:
        img = ImageOps.exif_transpose(img)
    except Exception as e:
        logger.warning(f"EXIF-ориентация не применилась: {e}")

    icc = img.info.get("icc_profile")
    if icc:
        try:
            src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            desc = (ImageCms.getProfileDescription(src) or "").lower()
            if "srgb" not in desc:  # sRGB → sRGB гонять незачем
                if img.mode == "CMYK":
                    return ImageCms.profileToProfile(img, src, _SRGB_PROFILE,
                                                     outputMode="RGB")
                if img.mode != "RGB":
                    img = img.convert("RGB")
                return ImageCms.profileToProfile(img, src, _SRGB_PROFILE,
                                                 outputMode="RGB")
        except Exception as e:
            logger.warning(f"ICC-профиль не применился ({e}) — беру как есть")
    return img.convert("RGB")


UPSCALE_WARN = 1.2  # растянули больше чем на 20% — предупреждаем


def upscale_factor(img: Image.Image, canvas_size) -> float:
    """Во сколько раз исходник растянут, чтобы заполнить холст (cover-fit)."""
    cw, ch = canvas_size
    return max(cw / img.width, ch / img.height)


def nfc(text: str) -> str:
    """Склеиваем «и + ˘» в «й» и т.п. — иначе буква рисуется двумя глифами."""
    return unicodedata.normalize("NFC", text or "")


def to_jpeg(img: Image.Image, quality: int = 92) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


# Рендер — тяжёлая синхронная работа. Гоняем её в отдельном потоке, чтобы бот
# не «замерзал» для остальных, и ограничиваем число одновременных рендеров,
# чтобы не выжрать память Railway, если постят сразу несколько человек.
_RENDER_SEM = asyncio.Semaphore(2)


async def run_render(fn, *args, **kwargs):
    async with _RENDER_SEM:
        return await asyncio.to_thread(fn, *args, **kwargs)


# ============ Кернинг ============
# Текст с трекингом рисуем по букве — иначе не задать межбуквенный интервал.
# Чтобы при этом не терять кернинг шрифта (пары AV, ТА, Г.), шаг буквы берём
# как длина(пара) − длина(вторая буква). Лигатуры и контекстные замены при
# замере отключаем — меряем только кернинг. Без libraqm (нет libfribidi в
# системе) Pillow кернинг не умеет — тогда шаги равны обычным ширинам букв,
# то есть результат ровно как раньше.
_PROBE = ImageDraw.Draw(Image.new("RGB", (4, 4)))
_KERN_ONLY = ["-liga", "-clig", "-dlig", "-calt"]


def glyph_advances(text: str, font) -> list:
    if not text:
        return []
    raqm = getattr(font, "layout_engine", None) == ImageFont.Layout.RAQM

    def length(t):
        if raqm:
            return _PROBE.textlength(t, font=font, features=_KERN_ONLY)
        return _PROBE.textlength(t, font=font)

    adv = []
    for i, c in enumerate(text):
        if i + 1 < len(text):
            nxt = text[i + 1]
            adv.append(length(c + nxt) - length(nxt))
        else:
            adv.append(length(c))
    return adv


# ============ Автоподгонка текста (защита от вылета за края) ============
def _tracked_width(text: str, font, ls_px: float) -> float:
    """Ширина строки с трекингом и кернингом (как её рисует draw_centered_title)."""
    if not text:
        return 0.0
    return sum(glyph_advances(text, font)) + ls_px * (len(text) - 1)


def fit_size(text_lines, loader, size, max_w, ls_ratio=0.0, min_size=10):
    """Максимальный кегль ≤ size, при котором самая широкая строка влезает в
    max_w. Если и так влезает — возвращает size как есть (спека не меняется)."""
    size = int(size)
    while size > min_size:
        font = loader(size)
        widest = max((_tracked_width(ln, font, round(size * ls_ratio))
                      for ln in text_lines), default=0)
        if widest <= max_w:
            return size
        # шаг пропорционально перелёту, но минимум на 1px
        size = max(min_size, min(size - 1, int(size * max_w / widest)))
    return min_size


# ============ Общие утилиты ============
def get_average_color(img: Image.Image, x: int, y: int, w: int, h: int):
    x = max(0, x); y = max(0, y)
    x2 = min(x + w, img.width)
    y2 = min(y + h, img.height)
    if x2 <= x or y2 <= y:
        return 0.0, 0.0, 0.0
    region = img.crop((x, y, x2, y2)).convert("RGB")
    arr = np.array(region).reshape(-1, 3).mean(axis=0)
    return float(arr[0]), float(arr[1]), float(arr[2])


def brightness_of(r, g, b):
    return (r * 299 + g * 587 + b * 114) / 1000


def adaptive_percent(r, g, b, force=None) -> int:
    """Классика брендинга: тёмный фон → осветляем на 45%, светлый → затемняем.
    force='light'/'dark' — ручной выбор направления (коллаб: «светлый/тёмный»)."""
    if force == "light":
        return BRIGHTNESS_OFFSET
    if force == "dark":
        return -BRIGHTNESS_OFFSET
    return BRIGHTNESS_OFFSET if brightness_of(r, g, b) < 128 else -BRIGHTNESS_OFFSET


def adjust_brightness(r, g, b, percent):
    if percent > 0:
        r = min(255, r + (255 - r) * percent / 100)
        g = min(255, g + (255 - g) * percent / 100)
        b = min(255, b + (255 - b) * percent / 100)
    else:
        p = abs(percent)
        r = max(0, r - r * p / 100)
        g = max(0, g - g * p / 100)
        b = max(0, b - b * p / 100)
    return int(r), int(g), int(b)


def fit_image_to_canvas(img: Image.Image, canvas_w: int, canvas_h: int) -> Image.Image:
    """Заполнение канваса с центрированием и обрезкой (cover)."""
    canvas = Image.new("RGB", (canvas_w, canvas_h), (0, 0, 0))
    img_ratio = img.width / img.height
    canvas_ratio = canvas_w / canvas_h
    if img_ratio > canvas_ratio:
        draw_h = canvas_h
        draw_w = int(draw_h * img_ratio)
        offset_x = (canvas_w - draw_w) // 2
        offset_y = 0
    else:
        draw_w = canvas_w
        draw_h = int(draw_w / img_ratio)
        offset_x = 0
        offset_y = (canvas_h - draw_h) // 2
    resized = img.resize((draw_w, draw_h), Image.LANCZOS)
    canvas.paste(resized, (offset_x, offset_y))
    return canvas


def load_semibold(size: int) -> ImageFont.FreeTypeFont:
    path = os.path.join(BASE, "Nunito-SemiBold.ttf")
    try:
        return ImageFont.truetype(path, size)
    except Exception as e:
        logger.error(f"SemiBold не найден ({e}), системный fallback")
        for sf in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",):
            if os.path.exists(sf):
                return ImageFont.truetype(sf, size)
        return ImageFont.load_default(size=size)


def load_black(size: int) -> ImageFont.FreeTypeFont:
    path = os.path.join(BASE, "NunitoSans-Black.ttf")
    try:
        return ImageFont.truetype(path, size)
    except Exception as e:
        logger.error(f"Black не найден ({e}), системный fallback")
        for sf in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",):
            if os.path.exists(sf):
                return ImageFont.truetype(sf, size)
        return ImageFont.load_default(size=size)


def load_bold(size) -> ImageFont.FreeTypeFont:
    """Nunito BOLD через вариативный шрифт (ось Weight → Bold/700).
    Это настоящий Nunito Bold, не Sans. Размер принимает float."""
    path = os.path.join(BASE, "Nunito-VariableFont_wght.ttf")
    try:
        f = ImageFont.truetype(path, size)
        try:
            f.set_variation_by_name(b"Bold")
        except Exception as e:
            logger.warning(f"Nunito Bold-вариация не выставилась ({e}) — вес по умолчанию")
        return f
    except Exception as e:
        logger.error(f"Nunito variable не найден ({e}) — фолбэк SemiBold/системный")
        try:
            return ImageFont.truetype(os.path.join(BASE, "Nunito-SemiBold.ttf"), size)
        except Exception:
            for sf in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",):
                if os.path.exists(sf):
                    return ImageFont.truetype(sf, size)
            return ImageFont.load_default(size=int(size))


def get_wordmark() -> Image.Image:
    """Базовый широкий вордмарк ÖMANKÖ (белый). Используется в ленте обложки
    и как фолбэк в сторис для каналов без своего story-лого."""
    if "_base" in _WORDMARK_CACHE:
        return _WORDMARK_CACHE["_base"]
    img = Image.open(os.path.join(BASE, BASE_WORDMARK_FILE)).convert("RGBA")
    _WORDMARK_CACHE["_base"] = img
    return img


def _tint_white_logo(logo: Image.Image, color: tuple, alpha: float) -> Image.Image:
    """Заливает непрозрачные пиксели белого силуэта цветом `color`,
    сохраняя альфа-края. `alpha` — общая прозрачность (0..1)."""
    r, g, b = int(color[0]), int(color[1]), int(color[2])
    solid = Image.new("RGBA", logo.size, (r, g, b, 0))
    a = logo.split()[3].point(lambda p: int(p * alpha))
    solid.putalpha(a)
    return solid


def paste_type1_channel_logo(canvas_rgba, fname, x, y, w, h, color):
    """Тип 1: вставка лого канала, перекрашенного под фон (адаптивно)."""
    logo = _load_logo(fname)
    if logo is None:
        return False
    resized = logo.resize((w, h), Image.LANCZOS)
    tinted = _tint_white_logo(resized, color, ALPHA)
    canvas_rgba.alpha_composite(tinted, (x, y))
    return True


def paste_story_channel_logo(canvas_rgba, fname, cx, y_top, w, h):
    """Сторис обложки: вставка лого канала фиксированного размера (белый, как есть)."""
    logo = _load_logo(fname)
    if logo is None:
        return False
    resized = logo.resize((w, h), Image.LANCZOS)
    canvas_rgba.alpha_composite(resized, (int(cx - w / 2), y_top))
    return True


# ============ Тип 1: логотип Ö ============
def draw_logo(canvas: Image.Image, x: int, y: int, w: int, h: int, color: tuple):
    """Угловой логотип Ö. Берётся из PNG (main_o.png) — белый силуэт на
    прозрачном фоне, адаптивно перекрашивается под фон тем же цветом, что и
    раньше (как лого остальных каналов). Размеры/отступы/цвет не меняются.
    Если PNG не найден — векторный фолбэк (прежнее поведение)."""
    logo = _load_logo(BASE_O_LOGO_FILE)
    if logo is not None:
        resized = logo.resize((w, h), Image.LANCZOS)
        tinted = _tint_white_logo(resized, color, ALPHA)
        canvas.alpha_composite(tinted, (x, y))
        return

    # --- фолбэк: векторный Ö (прежнее поведение, если файла нет) ---
    vlogo = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(vlogo)
    r, g, b = color
    a = int(255 * ALPHA)
    fill = (r, g, b, a)
    sx = w / 365
    sy = h / 459
    outer = [0, int(94 * sy), w - 1, h - 1]
    inner = [int(84 * sx), int(179 * sy), int(280 * sx), int(375 * sy)]
    d.ellipse(outer, fill=fill)
    d.ellipse(inner, fill=(0, 0, 0, 0))
    d.ellipse([int(79 * sx), int(0 * sy), int(164 * sx), int(85 * sy)], fill=fill)
    d.ellipse([int(201 * sx), int(0 * sy), int(286 * sx), int(85 * sy)], fill=fill)
    canvas.paste(vlogo, (x, y), vlogo)


def process_image(img: Image.Image, format_key: str, hashtag: str, channel: str = "base") -> Image.Image:
    """ТИП 1 — брендинг. Логотип: у базового/agency — векторный Ö (как раньше),
    у остальных каналов — свой PNG-логотип своего размера, адаптивно перекрашенный."""
    fmt = FORMATS[format_key]
    if fmt is None:
        if img.width < 1920:
            scale = 1920 / img.width
            canvas_w = 1920
            canvas_h = int(img.height * scale)
        else:
            canvas_w, canvas_h = img.size
    else:
        canvas_w, canvas_h = fmt

    scale = canvas_w / 1920

    # Геометрия логотипа: своя у канала (type1_box), иначе дефолтные LOGO_*
    ch = CHANNELS.get(channel, CHANNELS["base"])
    if ch["type1_box"]:
        bw, bh, bleft, bbottom = ch["type1_box"]
        logo_w = int(bw * scale)
        logo_h = int(bh * scale)
        logo_x = int(bleft * scale)
        logo_y = canvas_h - int(bbottom * scale) - logo_h
    else:
        logo_w = int(LOGO_W * scale)
        logo_h = int(LOGO_H * scale)
        logo_x = int(LOGO_LEFT * scale)
        logo_y = canvas_h - int(LOGO_BOTTOM * scale) - logo_h

    canvas = fit_image_to_canvas(img, canvas_w, canvas_h)

    # ЛОГОТИП — адаптивный цвет по фону под ним
    r, g, b = get_average_color(canvas, logo_x, logo_y, logo_w, logo_h)
    percent = BRIGHTNESS_OFFSET if brightness_of(r, g, b) < 128 else -BRIGHTNESS_OFFSET
    logo_color = adjust_brightness(r, g, b, percent)
    canvas_rgba = canvas.convert("RGBA")
    placed = False
    if ch["type1_logo"]:
        placed = paste_type1_channel_logo(canvas_rgba, ch["type1_logo"],
                                          logo_x, logo_y, logo_w, logo_h, logo_color)
    if not placed:
        draw_logo(canvas_rgba, logo_x, logo_y, logo_w, logo_h, logo_color)
    canvas = canvas_rgba.convert("RGB")

    # ХЕШТЕГ: не заходит левее лого (+60px воздуха)
    return draw_corner_hashtag(canvas, hashtag, scale, logo_x + logo_w + int(60 * scale))


def draw_corner_hashtag(canvas: Image.Image, hashtag: str, scale: float,
                        left_limit: int, force=None) -> Image.Image:
    """Хештег в правом нижнем углу (брендинг и коллаб). Длинный хештег не
    заходит левее left_limit — кегль уменьшается. force — ручное направление
    цвета ('light'/'dark'), None — адаптивно, как всегда."""
    if not hashtag or hashtag == NO_HASHTAG:
        return canvas
    canvas_w, canvas_h = canvas.size
    hashtag_right = int(HASHTAG_RIGHT * scale)
    hashtag_bottom = int(HASHTAG_BOTTOM * scale)
    hashtag_size = int(HASHTAG_SIZE * scale)
    max_tag_w = canvas_w - hashtag_right - left_limit
    hashtag_size = fit_size([hashtag], load_semibold, hashtag_size,
                            max_tag_w, ls_ratio=-0.007)
    sample_x = max(0, canvas_w - hashtag_right - int(200 * scale))
    sample_y = max(0, canvas_h - hashtag_bottom - hashtag_size)
    hr, hg, hb = get_average_color(canvas, sample_x, sample_y, int(200 * scale), hashtag_size + 20)
    hcr, hcg, hcb = adjust_brightness(hr, hg, hb, adaptive_percent(hr, hg, hb, force))

    overlay = canvas.convert("RGBA")
    draw = ImageDraw.Draw(overlay)
    font = load_semibold(hashtag_size)
    fill = (hcr, hcg, hcb, int(255 * ALPHA))
    spacing = int(hashtag_size * (-0.007))
    total_w = 0
    char_widths = []
    for glyph in hashtag:
        bbox = draw.textbbox((0, 0), glyph, font=font)
        cw = bbox[2] - bbox[0]
        char_widths.append(cw)
        total_w += cw + spacing
    total_w -= spacing
    tx = canvas_w - hashtag_right - total_w
    ty = canvas_h - hashtag_bottom - hashtag_size
    cx = tx
    for glyph, cw in zip(hashtag, char_widths):
        draw.text((cx, ty), glyph, font=font, fill=fill)
        cx += cw + spacing
    return overlay.convert("RGB")


# ============ Коллаборация — рендер ============
def collab_layout(partner: dict, unit: float, canvas_w: int, anchor_bottom=None,
                  anchor_top=None, max_w=None, native_wordmark=False) -> dict:
    """Раскладка связки «ÖMANKÖ × партнёр» по спеке коллаба (опорный холст
    1920px), умноженной на unit. Вордмарк 327×71, пропуск 43, «×» (как хештег,
    51), пропуск 43, лого партнёра 58px в высоту × scale из пресета. Элементы на
    общей оси: центр вордмарка, опущенный на 9px. Группа — по центру холста.
    anchor_bottom — y низа вордмарка (лента), anchor_top — y верха (сторис).
    native_wordmark — высота вордмарка по родным пропорциям PNG (как в обложке)."""
    eps = 1e-6
    wm_w = max(1, int(COLLAB_WORDMARK_W * unit + eps))
    if native_wordmark:
        wm = get_wordmark()
        wm_h = max(1, round(wm_w * wm.height / wm.width))
    else:
        wm_h = max(1, int(COLLAB_WORDMARK_H * unit + eps))
    gap = int(COLLAB_GAP * unit + eps)
    x_size = max(1, int(COLLAB_X_SIZE * unit + eps))
    p_img = partner["img"]
    p_h = max(1, int(COLLAB_PARTNER_H * unit * float(partner.get("scale", 1.0)) + eps))
    p_w = max(1, round(p_img.width * (p_h / p_img.height)))
    font = load_semibold(x_size)
    xb = _PROBE.textbbox((0, 0), COLLAB_X_GLYPH, font=font)
    x_w, x_h = xb[2] - xb[0], xb[3] - xb[1]
    group_w = wm_w + gap + x_w + gap + p_w
    if max_w and group_w > max_w and unit > 0.05:
        # очень широкий лого партнёра — ужимаем всю связку, якорь не двигаем
        return collab_layout(partner, unit * max_w / group_w, canvas_w, anchor_bottom,
                             anchor_top, max_w, native_wordmark)
    wm_top = anchor_top if anchor_top is not None else anchor_bottom - wm_h
    left = (canvas_w - group_w) // 2
    center_y = wm_top + wm_h / 2 + COLLAB_WORDMARK_CENTER_DROP * unit
    p_center = center_y + float(partner.get("dy", 0)) * unit
    x_left = left + wm_w + gap
    strip_top = int(min(wm_top, center_y - x_h / 2, p_center - p_h / 2))
    strip_bottom = int(max(wm_top + wm_h, center_y + x_h / 2, p_center + p_h / 2))
    return dict(
        left=left, right=left + group_w, group_w=group_w,
        wm_w=wm_w, wm_h=wm_h, wm_top=wm_top,
        font=font, x_pos=(int(x_left - xb[0]), int(center_y - x_h / 2 - xb[1])),
        p_img=p_img, p_w=p_w, p_h=p_h, p_left=x_left + x_w + gap,
        p_top=int(p_center - p_h / 2),
        strip=(left, strip_top, group_w, max(1, strip_bottom - strip_top)),
    )


def collab_color(canvas: Image.Image, layout: dict, force=None) -> tuple:
    """Адаптивный цвет связки по полосе фона под ней (как в брендинге)."""
    sr, sg, sb = get_average_color(canvas, *layout["strip"])
    return adjust_brightness(sr, sg, sb, adaptive_percent(sr, sg, sb, force))


def collab_draw(overlay: Image.Image, L: dict, color: tuple, alpha: float) -> None:
    wm_rs = get_wordmark().resize((L["wm_w"], L["wm_h"]), Image.LANCZOS)
    overlay.alpha_composite(_tint_white_logo(wm_rs, color, alpha), (L["left"], L["wm_top"]))
    fill = (int(color[0]), int(color[1]), int(color[2]), int(255 * alpha))
    ImageDraw.Draw(overlay).text(L["x_pos"], COLLAB_X_GLYPH, font=L["font"], fill=fill)
    p_rs = L["p_img"].resize((L["p_w"], L["p_h"]), Image.LANCZOS)
    overlay.alpha_composite(_tint_white_logo(p_rs, color, alpha), (L["p_left"], L["p_top"]))


def process_collab(img: Image.Image, format_key: str, partner: dict,
                   hashtag=None, color_mode=None) -> Image.Image:
    """КОЛЛАБОРАЦИЯ (брендинг) — внизу по центру «ÖMANKÖ × партнёр», справа
    опционально хештег. Низ строки — как у логотипа в брендинге (COLLAB_BOTTOM).
    partner — пресет: {"img": белый силуэт, "scale": 1.0, "dy": 0}.
    color_mode: None/'auto' — адаптивно, 'light'/'dark' — ручное направление."""
    fmt = FORMATS[format_key]
    if fmt is None:
        if img.width < 1920:
            up = 1920 / img.width
            canvas_w = 1920
            canvas_h = int(img.height * up)
        else:
            canvas_w, canvas_h = img.size
    else:
        canvas_w, canvas_h = fmt

    scale = canvas_w / 1920
    force = color_mode if color_mode in ("light", "dark") else None
    canvas = fit_image_to_canvas(img, canvas_w, canvas_h)
    L = collab_layout(partner, scale, canvas_w,
                      anchor_bottom=canvas_h - int(COLLAB_BOTTOM * scale),
                      max_w=canvas_w * (1 - 2 * TITLE_SIDE_MARGIN))
    color = collab_color(canvas, L, force) if COLLAB_ADAPTIVE else (255, 255, 255)
    overlay = canvas.convert("RGBA")
    collab_draw(overlay, L, color, ALPHA)
    canvas = overlay.convert("RGB")
    # Хештег справа — как в брендинге, но не заходит на связку
    return draw_corner_hashtag(canvas, hashtag, scale, L["right"] + int(60 * scale), force)


# ============ Коллаборация — логотип партнёра ============
LOGO_MAX_SIDE = 1600       # исходник логотипа храним не больше этого
LOGO_SILHOUETTE_H = 600    # высота готового силуэта (с запасом для любых холстов)
MASK_NAMES = {"sil": "силуэт", "light": "светлое = лого", "dark": "тёмное = лого"}


def is_svg(data: bytes, filename: str = "") -> bool:
    head = data[:1024].lstrip().lower()
    return (filename or "").lower().endswith(".svg") or head.startswith(b"<svg") or (
        head.startswith(b"<?xml") and b"<svg" in data[:4096].lower())


def rasterize_logo(data: bytes, filename: str = "") -> Image.Image:
    """Любой присланный логотип → RGBA. SVG растеризуем (cairosvg)."""
    if is_svg(data, filename):
        if not SVG_OK:
            raise ValueError("svg_unsupported")
        img = Image.open(io.BytesIO(cairosvg.svg2png(bytestring=data, output_height=900)))
    else:
        img = Image.open(io.BytesIO(data))
        try:
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass
    img = img.convert("RGBA")
    if max(img.size) > LOGO_MAX_SIDE:
        img.thumbnail((LOGO_MAX_SIDE, LOGO_MAX_SIDE), Image.LANCZOS)
    return img


def _has_transparency(alpha: np.ndarray) -> bool:
    return float((alpha < 250).mean()) > 0.01


def logo_has_inner_contrast(src: Image.Image) -> bool:
    """Прозрачный логотип, внутри которого есть и тёмное, и светлое (бейдж с
    надписью внутри круга). Только для таких нужен выбор маски."""
    a = np.asarray(src).astype(np.float32)
    alpha = a[..., 3]
    if not _has_transparency(alpha):
        return False
    solid = alpha > 200
    if solid.sum() < 50:
        return False
    lum = (a[..., 0] * 0.299 + a[..., 1] * 0.587 + a[..., 2] * 0.114)[solid]
    # У одноцветного лого одна из долей ровно 0; у бейджа светлая надпись
    # занимает от ~1% (мелкое слово на плашке) до 10% (надпись в круге).
    return float((lum < 90).mean()) > 0.01 and float((lum > 170).mean()) > 0.01


def logo_silhouette(src: Image.Image, mask_mode: str = "sil") -> Image.Image:
    """Логотип → белый силуэт на прозрачном фоне, обрезанный по краям.
    • Есть прозрачность — берём альфу (mask_mode: силуэт / светлое / тёмное).
    • Фона нет (JPG, фото, PNG на белом) — фон определяем по рамке картинки и
      вычитаем: остаётся то, что отличается от фона (с мягкими краями)."""
    a = np.asarray(src).astype(np.float32)
    rgb, alpha = a[..., :3], a[..., 3] / 255.0
    if _has_transparency(a[..., 3]):
        if mask_mode in ("light", "dark"):
            lum = (rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114) / 255.0
            m = alpha * (lum if mask_mode == "light" else 1.0 - lum)
            m = m / m.max() if m.max() > 0 else m
        else:
            m = alpha
    else:
        border = np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]])
        bg = np.median(border, axis=0)
        dist = np.sqrt(((rgb - bg) ** 2).sum(-1))
        d = dist / max(float(dist.max()), 1.0)
        m = np.clip((d - 0.12) / 0.38, 0.0, 1.0)  # шум JPEG и сам фон → 0
    mask = Image.fromarray(np.round(m * 255).astype(np.uint8), "L")
    bbox = mask.getbbox()
    if not bbox:
        raise ValueError("empty_logo")
    mask = mask.crop(bbox)
    out = Image.new("RGBA", mask.size, (255, 255, 255, 0))
    out.putalpha(mask)
    if out.height > LOGO_SILHOUETTE_H:
        out = out.resize((max(1, round(out.width * LOGO_SILHOUETTE_H / out.height)),
                          LOGO_SILHOUETTE_H), Image.LANCZOS)
    return out


PREVIEW_BG_COVER = (30, 31, 36)      # «затемнённое фото» под обложкой
PREVIEW_BG_BRAND = (222, 218, 209)   # светлое фото в брендинге


def render_partner_preview(silhouette: Image.Image, scale: float, dy: float) -> bytes:
    """Превью пресета в реальном размере (холст 1920px), два настоящих случая:
    сверху — белым, как связка ляжет в обложку и сторис; снизу — адаптивным
    цветом на светлом фото, как в брендинге. По белому хорошо видно и
    чистоту вырезки логотипа."""
    partner = {"img": silhouette, "scale": scale, "dy": dy}
    strips = []
    for bg, cover_style in ((PREVIEW_BG_COVER, True), (PREVIEW_BG_BRAND, False)):
        strip = Image.new("RGB", (1920, 300), bg)
        L = collab_layout(partner, 1.0, 1920, anchor_bottom=187,
                          max_w=1920 * (1 - 2 * TITLE_SIDE_MARGIN))
        ov = strip.convert("RGBA")
        if cover_style:
            collab_draw(ov, L, (255, 255, 255), 1.0)
        else:
            collab_draw(ov, L, collab_color(strip, L), ALPHA)
        strips.append(ov.convert("RGB"))
    out = Image.new("RGB", (1920, 600))
    out.paste(strips[0], (0, 0))
    out.paste(strips[1], (0, 300))
    out = out.resize((1280, 400), Image.LANCZOS)
    return to_jpeg(out, quality=90)


# ============ Коллаборация — пресеты партнёров (общие для команды) ============
# partners.json: {id: {name, scale, dy, mask, hashtag, uses, last_used, ...}}
# partners/<id>.png — белый силуэт для рендера, <id>_src.png — исходник (для
# смены маски и правок). Всё на Volume рядом со статистикой.
PARTNER_NAME_MAX = 32
PARTNER_SCALE_STEP = 0.08
PARTNER_SCALE_MIN, PARTNER_SCALE_MAX = 0.5, 2.5
PARTNER_DY_STEP, PARTNER_DY_MAX = 4, 40
_PARTNER_IMG_CACHE = {}


def load_partners() -> dict:
    return load_json(PARTNERS_FILE, {})


def save_partners(partners: dict) -> None:
    save_json(PARTNERS_FILE, partners)


def sorted_partners() -> list:
    """[(id, preset)] — последние использованные сверху, потом новые."""
    ps = load_partners()
    return sorted(ps.items(), key=lambda kv: (kv[1].get("last_used") or kv[1].get("created_at") or ""),
                  reverse=True)


def _partner_files(pid: str):
    return (os.path.join(PARTNERS_DIR, f"{pid}.png"), os.path.join(PARTNERS_DIR, f"{pid}_src.png"))


def save_partner_images(pid: str, silhouette: Image.Image, src: Image.Image = None) -> None:
    os.makedirs(PARTNERS_DIR, exist_ok=True)
    sil_path, src_path = _partner_files(pid)
    silhouette.save(sil_path, "PNG")
    if src is not None:
        src.save(src_path, "PNG")
    _PARTNER_IMG_CACHE.pop(pid, None)


def load_partner_image(pid: str, src: bool = False):
    if not src and pid in _PARTNER_IMG_CACHE:
        return _PARTNER_IMG_CACHE[pid]
    try:
        img = Image.open(_partner_files(pid)[1 if src else 0]).convert("RGBA")
    except Exception:
        return None
    if not src:
        _PARTNER_IMG_CACHE[pid] = img
    return img


def partner_for_render(pid: str):
    """Пресет в виде, готовом для рендера, или None (удалён / нет файла)."""
    if not pid:
        return None
    p = load_partners().get(pid)
    img = load_partner_image(pid) if p else None
    if img is None:
        return None
    return {"id": pid, "name": p["name"], "img": img,
            "scale": float(p.get("scale", 1.0)), "dy": float(p.get("dy", 0))}


def touch_partner(pid: str, hashtag=None) -> None:
    ps = load_partners()
    p = ps.get(pid)
    if not p:
        return
    p["uses"] = int(p.get("uses", 0)) + 1
    p["last_used"] = datetime.now(timezone.utc).isoformat()
    if hashtag is not None:
        p["hashtag"] = hashtag
    save_partners(ps)


def delete_partner(pid: str) -> None:
    ps = load_partners()
    ps.pop(pid, None)
    save_partners(ps)
    for path in _partner_files(pid):
        try:
            os.remove(path)
        except OSError:
            pass
    _PARTNER_IMG_CACHE.pop(pid, None)


def partner_name_taken(name: str, except_pid: str = None) -> bool:
    key = name.casefold()
    return any(p.get("name", "").casefold() == key and pid != except_pid
               for pid, p in load_partners().items())


def name_from_filename(fname: str):
    """Подсказка имени партнёра из имени файла: aaa_magazine_logo_white.png → «aaa magazine»."""
    stem = os.path.splitext(os.path.basename(fname or ""))[0]
    stem = re.sub(r"[_\-.]+", " ", stem)
    stem = re.sub(r"(?i)\b(logo|logotype|лого|логотип|white|black|wh|bl|белый|ч[её]рный|"
                  r"final|copy|копия|png|svg|jpe?g|\d{3,})\b", " ", stem)
    stem = " ".join(stem.split())
    return stem[:PARTNER_NAME_MAX] if len(stem) >= 2 else None


# ============ Обложка: примитивы ============
def paste_wordmark(canvas_rgba: Image.Image, target_w: int, cx: int, y_top: int):
    wm = get_wordmark()
    ratio = wm.height / wm.width
    target_h = max(1, round(target_w * ratio))
    resized = wm.resize((target_w, target_h), Image.LANCZOS)
    x = int(cx - target_w / 2)
    canvas_rgba.alpha_composite(resized, (x, y_top))
    return target_h


def apply_bottom_gradient(canvas: Image.Image, brightness: float, rise: int,
                          dark_level: float = 1.0) -> Image.Image:
    """Чёрный градиент снизу вверх.

    Плотность складывается из двух частей:
      • АДАПТИВ — базовая alpha по яркости фона в зоне заголовка (тёмный фон →
        слабый градиент, светлый → плотный). Это поведение при dark_level=1.0.
      • РУЧНОЙ сдвиг dark_level — прибавляется к базовой alpha (Светлее/Темнее).
        1.0 = как было; <1 светлее; >1 темнее. Сдвиг действует одинаково сильно
        при любой яркости фона. Итог клампим [0 … GRAD_ALPHA_CEIL].
    """
    cw, ch = canvas.size
    t = max(0.0, min(1.0, brightness / 255.0))
    base_alpha = GRAD_ALPHA_DARK + (GRAD_ALPHA_LIGHT - GRAD_ALPHA_DARK) * t
    # dark_level — это сдвиг относительно базы: 1.0 = без сдвига, <1 светлее, >1 темнее.
    alpha = max(0.0, min(GRAD_ALPHA_CEIL, base_alpha + (dark_level - 1.0)))
    max_alpha = int(255 * alpha)
    rise = min(rise, ch)
    # вертикальный градиент: 0 сверху rise-зоны -> max_alpha у низа
    ramp = np.linspace(0, max_alpha, rise).astype(np.uint8).reshape(-1, 1)
    ramp = np.repeat(ramp, cw, axis=1)
    mask_full = np.zeros((ch, cw), dtype=np.uint8)
    mask_full[ch - rise:ch, :] = ramp
    mask = Image.fromarray(mask_full, mode="L")
    black = Image.new("RGBA", (cw, ch), (0, 0, 0, 255))
    base = canvas.convert("RGBA")
    base = Image.composite(black, base, mask)
    return base


def draw_centered_title(canvas_rgba: Image.Image, text: str, size: int,
                        ls_ratio: float, bottom_offset: int):
    cw, ch = canvas_rgba.size
    lines = [ln for ln in text.split("\n")]
    if not lines:
        return
    # Автоподгонка: если самая длинная строка шире холста минус поля (6% с
    # каждой стороны) — уменьшаем кегль. Нормальные заголовки не меняются.
    size = fit_size(lines, load_black, size, cw * (1 - 2 * TITLE_SIDE_MARGIN),
                    ls_ratio=ls_ratio)
    font = load_black(size)
    draw = ImageDraw.Draw(canvas_rgba)
    ls_px = round(size * ls_ratio)
    ascent, descent = font.getmetrics()
    line_adv = int(size * COVER_LINE_SPACING)
    line_visual = ascent + descent
    n = len(lines)
    last_top = (ch - bottom_offset) - line_visual
    first_top = last_top - (n - 1) * line_adv
    cx = cw / 2
    fill = (255, 255, 255, 255)
    for i, line in enumerate(lines):
        # ширина строки с трекингом и кернингом
        widths = glyph_advances(line, font)
        total = sum(widths) + ls_px * (len(line) - 1 if len(line) > 1 else 0)
        x = cx - total / 2
        y = first_top + i * line_adv
        for c, w in zip(line, widths):
            draw.text((x, y), c, font=font, fill=fill)
            x += w + ls_px


def draw_bubble(canvas_rgba: Image.Image, center_x: int, center_y: int,
                bubble_w, bubble_h: int, radius: int, bg_img: Image.Image,
                label=None, color_mode="dark", alpha=0.85,
                text_size=BUBBLE_TEXT_SIZE, pad_x=FEED_BUBBLE_PAD_X):
    """Бабл. label=None -> пустой бабл (сторис).
    color_mode: 'dark' (фикс. тёмный, лента) | 'adaptive_invert' (инверт к фону, сторис).
    bubble_w=None -> авто-ширина под текст (лента)."""
    font = None
    if label:
        # Бабл с длинным своим хештегом не должен вылезать за края ленты
        max_text_w = canvas_rgba.size[0] * (1 - 2 * TITLE_SIDE_MARGIN) - 2 * pad_x
        font = load_semibold(fit_size([label], load_semibold, text_size, max_text_w))
    d = ImageDraw.Draw(canvas_rgba)
    tw = d.textlength(label, font=font) if label else 0
    if bubble_w is None:
        bubble_w = int(tw + 2 * pad_x)
    left = int(center_x - bubble_w / 2)
    top = int(center_y - bubble_h / 2)
    right = left + bubble_w
    bottom = top + bubble_h

    # цвет фона под баблом
    r, g, b = get_average_color(bg_img, left, top, bubble_w, bubble_h)
    dark_bg = brightness_of(r, g, b) < 128
    if color_mode == "adaptive_invert":
        fill_rgb = (255, 255, 255) if dark_bg else (0, 0, 0)
    else:
        fill_rgb = FEED_BUBBLE_FILL
    fill = (fill_rgb[0], fill_rgb[1], fill_rgb[2], int(255 * alpha))

    layer = Image.new("RGBA", canvas_rgba.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle([left, top, right, bottom], radius=radius, fill=fill)
    canvas_rgba.alpha_composite(layer)

    if label:
        d = ImageDraw.Draw(canvas_rgba)
        bbox = d.textbbox((0, 0), label, font=font)
        txt_h = bbox[3] - bbox[1]
        tx = center_x - tw / 2
        ty = center_y - txt_h / 2 - bbox[1]
        d.text((tx, ty), label, font=font, fill=(255, 255, 255, 255))


# ============ Обложка: рендер вариантов ============
def render_cover_feed(img: Image.Image, format_key: str, title: str, hashtag: str,
                      dark_level: float = 1.0, partner: dict = None) -> Image.Image:
    spec = COVER_FORMATS.get(cover_key(format_key))
    k = 1.0  # масштаб элементов: у фиксированных форматов размеры абсолютные
    if spec is None:
        # Адаптивный: канвас по картинке (мин. ширина 1920). Отступы и размеры
        # берём у фиксированного формата с ближайшими пропорциями и масштабируем
        # под холст — иначе на оригиналах 3000–4000px всё выходило мелким.
        if img.width < 1920:
            sc = 1920 / img.width
            canvas_w, canvas_h = 1920, int(img.height * sc)
        else:
            canvas_w, canvas_h = img.size
        spec = nearest_cover_spec(canvas_w, canvas_h)
        k = canvas_w / spec["size"][0]
    else:
        canvas_w, canvas_h = spec["size"]
    bubble_h = round(spec["bubble_h"] * k)
    bubble_top = round(spec["bubble_top"] * k)
    title_bottom = round(spec["title_bottom"] * k)

    title_size = round(COVER_TITLE_SIZE_FEED * k)
    wm_w = round(WORDMARK_W_FEED * k)
    wm_bottom = round(WORDMARK_BOTTOM_FEED * k)
    radius = bubble_h // 2  # полная «таблетка»

    base = fit_image_to_canvas(img, canvas_w, canvas_h)

    # градиент — по яркости в зоне заголовка
    region_y = max(0, canvas_h - title_bottom - title_size * 2)
    br_r, br_g, br_b = get_average_color(base, 0, region_y, canvas_w, title_size * 2)
    grad_rise = min(canvas_h, title_bottom + title_size * 4)
    canvas = apply_bottom_gradient(base, brightness_of(br_r, br_g, br_b), grad_rise,
                                   dark_level=dark_level)

    # заголовок
    draw_centered_title(canvas, title, title_size, COVER_TITLE_LS_FEED, title_bottom)

    # бабл сверху по центру — тёмный плотный, с хештегом
    if hashtag and hashtag != NO_HASHTAG:
        cy = bubble_top + bubble_h // 2
        bg_for_bubble = canvas.convert("RGB")
        label = "# " + hashtag.lstrip("#")
        draw_bubble(canvas, canvas_w // 2, cy, None, bubble_h, radius, bg_for_bubble,
                    label=label, color_mode="dark", alpha=FEED_BUBBLE_ALPHA,
                    text_size=round(BUBBLE_TEXT_SIZE * k), pad_x=round(FEED_BUBBLE_PAD_X * k))

    if partner:
        # коллаб: вместо вордмарка — связка «ÖMANKÖ × партнёр», белая, как вордмарк.
        # Вордмарк в ней той же ширины и на том же отступе, что обычно в ленте.
        L = collab_layout(partner, (WORDMARK_W_FEED / COLLAB_WORDMARK_W) * k, canvas_w,
                          anchor_bottom=canvas_h - wm_bottom, native_wordmark=True,
                          max_w=canvas_w * (1 - 2 * TITLE_SIDE_MARGIN))
        collab_draw(canvas, L, (255, 255, 255), 1.0)
        return canvas.convert("RGB")

    # вордмарк снизу по центру — в ленте ВСЕГДА базовый ÖMANKÖ (по каналу не меняется)
    ratio = get_wordmark().height / get_wordmark().width
    wm_h = round(wm_w * ratio)
    wm_y = canvas_h - wm_bottom - wm_h
    paste_wordmark(canvas, wm_w, canvas_w // 2, wm_y)

    return canvas.convert("RGB")


def render_cover_story(img: Image.Image, variant: str, title: str, hashtag: str,
                       channel: str = "base", dark_level: float = 1.0,
                       partner: dict = None) -> Image.Image:
    cw, ch = STORY_SIZE
    if variant == "ig":
        title_bottom = COVER_TITLE_BOTTOM_IG
        b_w, b_h, b_r, b_bottom = IG_BUBBLE_W, IG_BUBBLE_H, IG_BUBBLE_RADIUS, IG_BUBBLE_BOTTOM
    else:  # tg
        title_bottom = COVER_TITLE_BOTTOM_TG
        b_w, b_h, b_r, b_bottom = TG_BUBBLE_W, TG_BUBBLE_H, TG_BUBBLE_RADIUS, TG_BUBBLE_BOTTOM

    base = fit_image_to_canvas(img, cw, ch)

    # градиент по яркости в зоне заголовка
    region_y = ch - title_bottom - COVER_TITLE_SIZE_STORY * 2
    br_r, br_g, br_b = get_average_color(base, 0, max(0, region_y), cw, COVER_TITLE_SIZE_STORY * 2)
    canvas = apply_bottom_gradient(base, brightness_of(br_r, br_g, br_b), GRAD_RISE_STORY,
                                   dark_level=dark_level)

    # лого сверху по центру:
    #  - канал со своим story-лого → фиксированный размер (story_box), белый как есть
    #  - база/agency → широкий вордмарк ÖMANKÖ (как раньше)
    chan = CHANNELS.get(channel, CHANNELS["base"])
    placed = False
    if partner:
        # коллаб: связка в размере сторис-вордмарка (195px), верх на том же месте
        L = collab_layout(partner, WORDMARK_W_STORY / COLLAB_WORDMARK_W, cw,
                          anchor_top=WORDMARK_TOP_STORY, native_wordmark=True,
                          max_w=cw * (1 - 2 * TITLE_SIDE_MARGIN))
        collab_draw(canvas, L, (255, 255, 255), 1.0)
        placed = True
    elif chan["story_logo"] and chan["story_box"]:
        lw, lh = chan["story_box"]
        placed = paste_story_channel_logo(canvas, chan["story_logo"], cw // 2, WORDMARK_TOP_STORY, lw, lh)
    if not placed:
        paste_wordmark(canvas, WORDMARK_W_STORY, cw // 2, WORDMARK_TOP_STORY)

    # заголовок
    draw_centered_title(canvas, title, COVER_TITLE_SIZE_STORY, COVER_TITLE_LS_STORY, title_bottom)

    # бабл под заголовком — ПУСТОЙ (без хештега), цвет инвертный к фону
    cy = ch - b_bottom - b_h // 2
    bg_for_bubble = canvas.convert("RGB")
    draw_bubble(canvas, cw // 2, cy, b_w, b_h, b_r, bg_for_bubble,
                label=None, color_mode="adaptive_invert", alpha=STORY_BUBBLE_ALPHA)

    return canvas.convert("RGB")


# ============ Клавиатуры ============
_BACK_BTN = InlineKeyboardButton("⬅️ Назад", callback_data="nav:back")


def back_keyboard():
    """Клавиатура из одной кнопки «Назад» — для текстовых шагов (фото/заголовок)."""
    return InlineKeyboardMarkup([[_BACK_BTN]])


def type_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏷 Брендинг", callback_data="type:type1")],
        [InlineKeyboardButton("🖼 Обложка", callback_data="type:cover")],
        [InlineKeyboardButton("🤝 Коллаборация", callback_data="type:collab")],
        [InlineKeyboardButton("🛍 ÖMANKÖ STORE", callback_data="type:store")],
    ])


def channel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(CHANNELS["base"]["title"], callback_data="channel:base")],
        [InlineKeyboardButton(CHANNELS["news"]["title"], callback_data="channel:news"),
         InlineKeyboardButton(CHANNELS["girls"]["title"], callback_data="channel:girls")],
        [InlineKeyboardButton(CHANNELS["music"]["title"], callback_data="channel:music"),
         InlineKeyboardButton(CHANNELS["agency"]["title"], callback_data="channel:agency")],
        [InlineKeyboardButton(CHANNELS["gastro"]["title"], callback_data="channel:gastro"),
         InlineKeyboardButton(CHANNELS["dom"]["title"], callback_data="channel:dom")],
        [_BACK_BTN],
    ])


def format_keyboard():
    keys = list(FORMATS.keys())
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(k, callback_data=f"fmt:{k}") for k in keys[:4]],
        [InlineKeyboardButton(keys[4], callback_data=f"fmt:{keys[4]}")],
        [_BACK_BTN],
    ])


def cover_format_keyboard():
    """Форматы ленты обложки — по её собственным холстам (там 3:4, а не 2:3)."""
    keys = list(COVER_FORMATS.keys())
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(k, callback_data=f"fmt:{k}") for k in keys],
        [InlineKeyboardButton("Адаптивный", callback_data="fmt:Адаптивный")],
        [_BACK_BTN],
    ])


def hashtag_keyboard(channel: str = "base", recent: str = None):
    """recent — хештег, с которым в прошлый раз постили этого партнёра:
    отдельной кнопкой наверху."""
    tags = CHANNEL_HASHTAGS.get(channel, COMMON_HASHTAGS)
    items = [NO_HASHTAG] + [t for t in tags if t != recent]
    rows, row = [], []
    if recent and recent != NO_HASHTAG:
        rows.append([InlineKeyboardButton(f"↩️ {recent}", callback_data=f"tag:{recent}")])
    for tag in items:
        row.append(InlineKeyboardButton(tag, callback_data=f"tag:{tag}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    # «Свой хештег» — отдельной строкой на всю ширину, для всех каналов
    rows.append([InlineKeyboardButton("✏️ Свой хештег", callback_data=f"tag:{CUSTOM_HASHTAG_CB}")])
    rows.append([_BACK_BTN])
    return InlineKeyboardMarkup(rows)


def dark_meter(idx: int) -> str:
    """Текстовый индикатор уровня затемнения: ●●●○○ + подпись ступени."""
    n = len(DARK_LEVELS)
    filled = "●" * (idx + 1) + "○" * (n - idx - 1)
    return f"{filled} ({DARK_LEVEL_NAMES[idx]})"


def cover_dark_keyboard(idx: int):
    """Слайдер затемнения градиента обложки (с живым превью).
    ☀️ Светлее / 🌑 Темнее — двигают уровень; ✅ Сгенерировать — финальный рендер.
    Края гаснут. Префикс cdark: не пересекается с другими хендлерами."""
    left = InlineKeyboardButton(
        "☀️ Светлее" if idx > 0 else "· · ·",
        callback_data="cdark:down" if idx > 0 else "cdark:noop")
    right = InlineKeyboardButton(
        "🌑 Темнее" if idx < len(DARK_LEVELS) - 1 else "· · ·",
        callback_data="cdark:up" if idx < len(DARK_LEVELS) - 1 else "cdark:noop")
    apply = InlineKeyboardButton("✅ Сгенерировать", callback_data="cdark:apply")
    return InlineKeyboardMarkup([[left, right], [apply], [_BACK_BTN]])


# ---- Коллаб: партнёры, вид поста, цвет связки ----
PARTNERS_PER_PAGE = 8
COLLAB_COLORS = {"auto": "🎯 Авто", "light": "⬜ Светлый", "dark": "⬛ Тёмный"}


def partner_list_keyboard(page: int = 0, manage: bool = False):
    """Список пресетов: последние использованные сверху, по 2 в ряд."""
    ps = sorted_partners()
    pages = max(1, math.ceil(len(ps) / PARTNERS_PER_PAGE))
    page = max(0, min(page, pages - 1))
    prefix = "pm" if manage else "pt"
    rows, row = [], []
    for pid, p in ps[page * PARTNERS_PER_PAGE:(page + 1) * PARTNERS_PER_PAGE]:
        row.append(InlineKeyboardButton(("✏️ " if manage else "") + p["name"],
                                        callback_data=f"{prefix}:{pid}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    if pages > 1:
        rows.append([
            InlineKeyboardButton("◀️" if page > 0 else "·",
                                 callback_data=f"{prefix}:page:{page - 1}" if page > 0 else f"{prefix}:noop"),
            InlineKeyboardButton(f"{page + 1} / {pages}", callback_data=f"{prefix}:noop"),
            InlineKeyboardButton("▶️" if page < pages - 1 else "·",
                                 callback_data=f"{prefix}:page:{page + 1}" if page < pages - 1 else f"{prefix}:noop"),
        ])
    if not manage:
        rows.append([InlineKeyboardButton("➕ Новый партнёр", callback_data="pt:new")])
        if ps:
            rows.append([InlineKeyboardButton("⚙️ Управление партнёрами", callback_data="pt:manage")])
    rows.append([_BACK_BTN])
    return InlineKeyboardMarkup(rows)


def collab_kind_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏷 Брендинг", callback_data="ck:type1"),
         InlineKeyboardButton("🖼 Обложка", callback_data="ck:cover")],
        [_BACK_BTN],
    ])


def partner_tune_keyboard(d: dict):
    rows = [
        [InlineKeyboardButton("➖ Меньше", callback_data="tune:-"),
         InlineKeyboardButton("➕ Больше", callback_data="tune:+")],
        [InlineKeyboardButton("⬆️ Выше", callback_data="tune:up"),
         InlineKeyboardButton("⬇️ Ниже", callback_data="tune:down")],
    ]
    if d.get("contrast"):
        rows.append([InlineKeyboardButton(f"🔄 Маска: {MASK_NAMES[d.get('mask', 'sil')]}",
                                          callback_data="tune:mask")])
    rows.append([InlineKeyboardButton("✅ Сохранить", callback_data="tune:save")])
    rows.append([_BACK_BTN])
    return InlineKeyboardMarkup(rows)


def partner_card_keyboard(pid: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Переименовать", callback_data=f"pm:ren:{pid}"),
         InlineKeyboardButton("🖼 Заменить лого", callback_data=f"pm:logo:{pid}")],
        [InlineKeyboardButton("📏 Размер и сдвиг", callback_data=f"pm:size:{pid}"),
         InlineKeyboardButton("🗑 Удалить", callback_data=f"pm:del:{pid}")],
        [InlineKeyboardButton("⬅️ К списку", callback_data="pm:list")],
    ])


def collab_color_keyboard(cur: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(("• " if k == cur else "") + v, callback_data=f"ccol:{k}")
         for k, v in COLLAB_COLORS.items()],
        [InlineKeyboardButton("✅ Сгенерировать", callback_data="ccol:go")],
        [_BACK_BTN],
    ])


# ---- STORE: выбор цвета графики ----
def _store_gray_value(idx: int):
    """Позиция слайдера → серый (v,v,v). idx 0 = белый (255), max = чёрный (0)."""
    idx = max(0, min(STORE_GRAY_STEPS, idx))
    v = round(255 * (STORE_GRAY_STEPS - idx) / STORE_GRAY_STEPS)
    return (v, v, v)


def _store_gray_hex(idx: int) -> str:
    v = _store_gray_value(idx)[0]
    return "#{0:02X}{0:02X}{0:02X}".format(v)


def _store_gray_meter(idx: int) -> str:
    cells = "".join("🔘" if i == idx else "▬" for i in range(STORE_GRAY_STEPS + 1))
    return f"⚪ {cells} ⚫"


def store_color_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎯 Адаптивный", callback_data="scol:adaptive")],
        [InlineKeyboardButton("⬜ Светлый", callback_data="scol:light"),
         InlineKeyboardButton("⬛ Тёмный", callback_data="scol:dark")],
        [InlineKeyboardButton("🎚 Свой оттенок (ЧБ)", callback_data="scol:custom")],
        [_BACK_BTN],
    ])


def store_gray_keyboard(idx: int):
    """ЧБ-слайдер: ◀️ светлее / hex / темнее ▶️. Края гаснут."""
    left = InlineKeyboardButton(
        "◀️ светлее" if idx > 0 else "· · ·",
        callback_data="sgray:down" if idx > 0 else "sgray:noop")
    mid = InlineKeyboardButton(_store_gray_hex(idx), callback_data="sgray:noop")
    right = InlineKeyboardButton(
        "темнее ▶️" if idx < STORE_GRAY_STEPS else "· · ·",
        callback_data="sgray:up" if idx < STORE_GRAY_STEPS else "sgray:noop")
    return InlineKeyboardMarkup([
        [left, mid, right],
        [InlineKeyboardButton("✅ Применить ко всем", callback_data="sgray:apply")],
        [_BACK_BTN],
    ])


def process_store(img: Image.Image, text: str, color=None) -> Image.Image:
    """ÖMANKÖ STORE — витрина магазина.
    Холст всегда 2000×2500, фон-фото (cover-fit). Векторный Ö внизу слева;
    справа — подпись в 2 строки тем же цветом, шрифт Nunito Bold, межстрочный 90%.
    color=None → адаптивный цвет под фоном (как в брендинге);
    color=(r,g,b) → фиксированный цвет графики (Ö + текст)."""
    cw, ch = STORE_SIZE
    canvas = fit_image_to_canvas(img, cw, ch)

    logo_x = STORE_LOGO_LEFT
    logo_y = ch - STORE_LOGO_BOTTOM - STORE_LOGO_H
    logo_w, logo_h = STORE_LOGO_W, STORE_LOGO_H

    if color is None:
        # Адаптивный цвет под лого (как в брендинге) — общий для Ö и текста
        r, g, b = get_average_color(canvas, logo_x, logo_y, logo_w, logo_h)
        percent = BRIGHTNESS_OFFSET if brightness_of(r, g, b) < 128 else -BRIGHTNESS_OFFSET
        color = adjust_brightness(r, g, b, percent)
    color = (int(color[0]), int(color[1]), int(color[2]))

    canvas_rgba = canvas.convert("RGBA")
    draw_logo(canvas_rgba, logo_x, logo_y, logo_w, logo_h, color)

    # Подпись: до 2 строк справа от лого, тем же цветом
    lines = text.split("\n")[:2]
    font = load_bold(STORE_TEXT_SIZE)
    draw = ImageDraw.Draw(canvas_rgba)
    text_x = logo_x + logo_w + STORE_TEXT_GAP
    line_h = STORE_TEXT_SIZE * STORE_LINE_HEIGHT
    fill = (color[0], color[1], color[2], int(255 * ALPHA))
    _, descent = font.getmetrics()
    base_last = ch - STORE_TEXT_BOTTOM - descent  # базовая линия нижней строки
    n = len(lines)
    for i, line in enumerate(lines):
        baseline = base_last - (n - 1 - i) * line_h
        draw.text((text_x, baseline), line, font=font, fill=fill, anchor="ls")

    return canvas_rgba.convert("RGB")


# ============ Сессия, повтор, доставка ============
CONV_TIMEOUT_SEC = 30 * 60       # брошенная сессия закрывается через 30 мин тишины
UPLOAD_DEBOUNCE_SEC = 1.5        # пауза после последнего фото перед статусом «Загружено: N»
MEDIA_GROUP_MAX = 10             # лимит Telegram на альбом

_MODE_NAMES = {"type1": "🏷 Брендинг", "cover": "🖼 Обложка",
               "collab": "🤝 Коллаборация", "store": "🛍 STORE"}
# Что переносим в «Ещё раз с теми же настройками»
_LAST_KEYS = ("mode", "channel", "format", "hashtag", "title", "partner_id",
              "collab_kind", "collab_color", "store_text", "store_color", "dark_idx")


def reset_session(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Очищает текущую сессию (фото и т.д.), но сохраняет настройки прошлого
    поста — они нужны кнопке «Ещё раз с теми же настройками»."""
    last = context.user_data.get("_last")
    context.user_data.clear()
    if last:
        context.user_data["_last"] = last


def remember_last(context: ContextTypes.DEFAULT_TYPE, chat_id: int = None) -> None:
    ud = context.user_data
    ud["_last"] = {k: ud[k] for k in _LAST_KEYS if k in ud}
    if chat_id is not None:  # и на диск — чтобы 🔁 пережил передеплой
        allsets = load_json(LAST_FILE, {})
        allsets[str(chat_id)] = ud["_last"]
        save_json(LAST_FILE, allsets)


def load_last(chat_id: int):
    return load_json(LAST_FILE, {}).get(str(chat_id))


def is_collab(ud) -> bool:
    return ud.get("mode") == "collab"


def is_cover(ud) -> bool:
    """Обложка — обычная или коллаб-обложка."""
    return ud.get("mode") == "cover" or (is_collab(ud) and ud.get("collab_kind") == "cover")


def format_kb(ud):
    return cover_format_keyboard() if is_cover(ud) else format_keyboard()


def recent_tag(ud):
    """Хештег, с которым в прошлый раз постили выбранного партнёра."""
    if not is_collab(ud):
        return None
    return load_partners().get(ud.get("partner_id") or "", {}).get("hashtag")


def done_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 Ещё раз с теми же настройками", callback_data="again:repeat")],
        [InlineKeyboardButton("🆕 Новый пост", callback_data="again:new")],
    ])


async def finish(message, context: ContextTypes.DEFAULT_TYPE, ok: int, total: int):
    """Общий финал всех режимов: запоминаем настройки, чистим сессию, шлём итог
    с кнопками повтора."""
    remember_last(context, message.chat_id)
    reset_session(context)
    text = "✅ Готово!" if ok == total else f"⚠️ Готово частично: {ok} из {total}."
    await message.reply_text(text, reply_markup=done_keyboard())
    return ConversationHandler.END


def _short(text: str, n: int = 28) -> str:
    t = " / ".join(ln.strip() for ln in (text or "").split("\n") if ln.strip())
    return t if len(t) <= n else t[:n - 1] + "…"


def settings_summary(ud) -> str:
    """Короткое описание настроек для экрана повтора (обычный текст)."""
    mode = ud.get("mode", "type1")
    parts = [_MODE_NAMES.get(mode, mode)]
    if mode == "collab":
        p = load_partners().get(ud.get("partner_id") or "", {})
        parts.append("× " + p.get("name", "?"))
        parts.append("обложка" if is_cover(ud) else "брендинг")
    if mode in ("type1", "cover"):
        parts.append(CHANNELS.get(ud.get("channel", "base"), CHANNELS["base"])["title"])
    if mode != "store" and ud.get("format"):
        parts.append(cover_key(ud["format"]) if is_cover(ud) else ud["format"])
    if mode in ("type1", "cover", "collab"):
        tag = ud.get("hashtag")
        parts.append("без хештега" if (not tag or tag == NO_HASHTAG) else tag)
    if is_cover(ud):
        parts.append("затемнение: " + DARK_LEVEL_NAMES[ud.get("dark_idx", DARK_DEFAULT_IDX)])
    elif mode == "collab":
        parts.append("цвет: " + COLLAB_COLORS.get(ud.get("collab_color", "auto"), "")[2:].lower())
    if mode == "store":
        col = ud.get("store_color")
        if col is None:
            parts.append("цвет: адаптивный")
        else:
            parts.append("цвет: #{:02X}{:02X}{:02X}".format(*col))
    return " · ".join(parts)


async def _send_docs(message, files) -> None:
    """Отправка готовых файлов: один — документом, несколько — альбомом
    документов (до 10). Если альбом не ушёл — досылаем по одному."""
    if not files:
        return
    if len(files) == 1:
        data, name = files[0]
        await message.reply_document(document=data, filename=name, write_timeout=300)
        return
    media = [InputMediaDocument(media=data, filename=name) for data, name in files]
    try:
        await message.reply_media_group(media=media, write_timeout=300)
    except TimedOut:
        # Альбом мог уйти, просто ответ не дождались — повтор дал бы дубли
        logger.warning("Альбом: таймаут ответа Telegram, повторно не шлю")
    except RetryAfter as e:
        await asyncio.sleep(int(e.retry_after) + 1)
        await message.reply_media_group(media=media, write_timeout=300)
    except Exception as e:
        logger.warning(f"Альбом не отправился ({e}) — шлю по одному")
        for data, name in files:
            await message.reply_document(document=data, filename=name, write_timeout=300)


def human_error(e: Exception) -> str:
    """Ошибка рендера человеческими словами (технические детали — в лог)."""
    from PIL import UnidentifiedImageError
    if isinstance(e, UnidentifiedImageError):
        if not HEIF_OK:
            return "не смог открыть файл. Если это HEIC с iPhone — экспортируй в JPEG"
        return "не смог открыть файл — формат не похож на картинку"
    if isinstance(e, MemoryError):
        return "файл слишком большой для обработки"
    return f"{type(e).__name__}: {e}"


async def deliver(message, photos, render_one) -> int:
    """Рендер всех фото (в отдельном потоке) + отправка альбомами.
    render_one(photo_bytes, i) -> ([(jpeg_bytes, filename), ...], upscale) —
    синхронная; upscale — во сколько раз пришлось растянуть исходник.
    Файлы одного фото в разные альбомы не разрываются (важно для обложек:
    лента + IG + TG идут вместе). Возвращает число успешно обработанных фото."""
    ok = 0
    batch = []
    small = []
    for i, photo_bytes in enumerate(photos):
        try:
            await message.chat.send_action("upload_document")
        except Exception:
            pass
        try:
            files, factor = await run_render(render_one, photo_bytes, i)
        except Exception as e:
            logger.exception(f"Ошибка фото {i+1}")
            await message.reply_text(f"❌ Фото {i+1}: {human_error(e)}")
            continue
        if factor >= UPSCALE_WARN:
            small.append((i + 1, factor))
        if batch and len(batch) + len(files) > MEDIA_GROUP_MAX:
            await _send_docs(message, batch)
            batch = []
        batch.extend(files)
        ok += 1
    if batch:
        await _send_docs(message, batch)
    if small:
        nums = ", ".join(str(n) for n, _ in small)
        worst = max(f for _, f in small)
        await message.reply_text(
            f"⚠️ Фото {nums} — маленькое разрешение, пришлось растянуть до ×{worst:.1f}, "
            "может быть мыльно. Скорее всего, ушло сжатым — пришли оригинал файлом "
            "(скрепка → Файл).")
    return ok


# ---- Статус загрузки фото (одно сообщение на пачку, а не на каждое фото) ----
def upload_keyboard(n: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Готово ({n})", callback_data="photos:done"),
         InlineKeyboardButton("🗑 Сбросить", callback_data="photos:reset")],
        [_BACK_BTN],
    ])


def _upload_job_name(chat_id: int) -> str:
    return f"upl:{chat_id}"


def cancel_upload_status(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    jq = context.job_queue
    if not jq:
        return
    for job in jq.get_jobs_by_name(_upload_job_name(chat_id)):
        job.schedule_removal()


async def _post_upload_status(bot, chat_id: int, ud) -> None:
    photos = ud.get("photos") or []
    if not photos:
        return
    old = ud.pop("upl_status", None)
    if old:
        try:
            await bot.delete_message(chat_id, old)
        except Exception:
            pass
    n = len(photos)
    msg = await bot.send_message(
        chat_id, f"📥 Загружено: *{n}* фото.\nДокидывай ещё или жми «Готово».",
        parse_mode="Markdown", reply_markup=upload_keyboard(n))
    ud["upl_status"] = msg.message_id


async def _upload_status_job(context: ContextTypes.DEFAULT_TYPE):
    await _post_upload_status(context.bot, context.job.chat_id, context.user_data)


async def _drop_status_keyboard(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Убирает кнопки со статуса загрузки (после «Готово»/Назад), чтобы
    старые кнопки не висели в чате."""
    mid = context.user_data.pop("upl_status", None)
    if mid:
        try:
            await context.bot.edit_message_reply_markup(chat_id, mid, reply_markup=None)
        except Exception:
            pass


# ============ Хендлеры ============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_user(update.effective_chat.id)
    cancel_upload_status(context, update.effective_chat.id)
    reset_session(context)
    await update.effective_message.reply_text(
        "👋 Привет! Я Post Creator для ÖMANKÖ.\n\nЧто делаем?",
        reply_markup=type_keyboard()
    )
    return CHOOSING_TYPE


async def again(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопки после готового поста: 🔁 повтор с теми же настройками / 🆕 новый."""
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    chat_id = update.effective_chat.id
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    cancel_upload_status(context, chat_id)
    last = context.user_data.get("_last") or load_last(chat_id)
    reset_session(context)

    if action == "repeat" and not last:
        await query.message.reply_text(
            "Настроек прошлого поста не нашёл — начнём с нуля 👇",
            reply_markup=type_keyboard())
        return CHOOSING_TYPE
    if action != "repeat":
        add_user(chat_id)
        await query.message.reply_text("Что делаем?", reply_markup=type_keyboard())
        return CHOOSING_TYPE
    if last.get("mode") == "collab" and not partner_for_render(last.get("partner_id")):
        await query.message.reply_text(
            "Партнёра из прошлого поста уже нет в списке (его удалили) — выбери заново 👇",
            reply_markup=type_keyboard())
        return CHOOSING_TYPE

    context.user_data.update(last)
    context.user_data["repeat"] = True
    await query.message.reply_text(
        photos_prompt_text(context), parse_mode="Markdown", reply_markup=back_keyboard())
    return WAITING_PHOTOS


async def choose_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mode = query.data.split(":", 1)[1]
    context.user_data["mode"] = mode
    if mode == "collab":
        context.user_data["channel"] = "base"  # коллаб всегда на базовом вордмарке
        await query.edit_message_text(partner_list_text(), parse_mode="Markdown",
                                      reply_markup=partner_list_keyboard())
        return CHOOSING_PARTNER
    if mode == "store":
        context.user_data["channel"] = "base"  # Ö всегда базовый векторный
        await query.edit_message_text(
            photos_prompt_text(context),
            parse_mode="Markdown",
            reply_markup=back_keyboard(),
        )
        return WAITING_PHOTOS
    name = "Обложка" if mode == "cover" else "Брендинг"
    await query.edit_message_text(
        f"Режим: *{name}*\n\nТеперь выбери канал:",
        parse_mode="Markdown",
        reply_markup=channel_keyboard()
    )
    return CHOOSING_CHANNEL


_FILE_HINT = "📎 Отправляй фото как *файл* (скрепка → Файл), чтобы качество не сжалось.\n\n"


def photos_prompt_text(context) -> str:
    """Текст шага «пришли фото» — общий для прямого хода, повтора и «Назад»."""
    ud = context.user_data
    mode = ud.get("mode")
    n = len(ud.get("photos", []))
    have = f"📂 Уже загружено: *{n}*. " if n else ""
    tail = f"{have}Пришли фото, затем «Готово» или /done"
    if ud.get("repeat"):
        head = ("🔁 *Повтор:* " + escape_markdown(settings_summary(ud), version=1)
                + "\n\nВсё как в прошлый раз — нужны только новые фото.")
        if mode == "cover":
            head += " Заголовок спрошу после фото."
        elif mode == "store":
            head += " Подпись спрошу после фото."
        return head + "\n\n" + _FILE_HINT + tail
    if mode == "collab":
        p = load_partners().get(ud.get("partner_id") or "", {})
        kind = "обложка" if is_cover(ud) else "брендинг"
        return (f"🤝 *ÖMANKÖ × {escape_markdown(p.get('name', '?'), version=1)}* · {kind}\n\n"
                + _FILE_HINT + tail)
    if mode == "store":
        return "🛍 *ÖMANKÖ STORE*\n\n" + _FILE_HINT + tail
    channel = ud.get("channel", "base")
    return f"Канал: *{CHANNELS[channel]['title']}*\n\n" + _FILE_HINT + tail


async def choose_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    channel = query.data.split(":", 1)[1]
    if channel not in CHANNELS:
        channel = "base"
    context.user_data["channel"] = channel
    await query.edit_message_text(
        photos_prompt_text(context),
        parse_mode="Markdown",
        reply_markup=back_keyboard(),
    )
    return WAITING_PHOTOS


TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # Bot API не отдаёт ботам файлы больше 20 МБ


def too_big_text(doc) -> str:
    name = f"«{doc.file_name}» " if doc and doc.file_name else "Файл "
    size = f"({doc.file_size / 1024 / 1024:.0f} МБ) " if doc and doc.file_size else ""
    return (f"⚠️ {name}{size}больше 20 МБ — Telegram не даёт ботам скачивать такие. "
            "Экспортируй JPEG полегче или пришли этот кадр как обычное фото.")


async def download_media(msg):
    """Байты фото/картинки-документа. None + ответ человеку, если не вышло."""
    doc = msg.document
    if doc and doc.file_size and doc.file_size > TG_DOWNLOAD_LIMIT:
        await msg.reply_text(too_big_text(doc))
        return None
    target = msg.photo[-1] if msg.photo else doc
    if target is None:
        return None
    try:
        file = await target.get_file()
        return bytes(await file.download_as_bytearray())
    except BadRequest as e:
        if "too big" in str(e).lower():
            await msg.reply_text(too_big_text(doc))
            return None
        raise


async def receive_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    data = await download_media(msg)
    if data is None:
        return WAITING_PHOTOS
    photos = context.user_data.setdefault("photos", [])
    photos.append(data)

    # Не отвечаем на каждое фото: ждём паузу после последнего и шлём ОДИН
    # статус «Загружено: N» с кнопками. Альбом из 10 фото = 1 сообщение.
    chat_id = update.effective_chat.id
    cancel_upload_status(context, chat_id)
    if context.job_queue:
        context.job_queue.run_once(
            _upload_status_job, UPLOAD_DEBOUNCE_SEC, chat_id=chat_id,
            user_id=update.effective_user.id, name=_upload_job_name(chat_id))
    else:
        await _post_upload_status(context.bot, chat_id, context.user_data)
    return WAITING_PHOTOS


async def photos_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🗑 Сбросить: выкидываем все загруженные фото, остаёмся на том же шаге."""
    query = update.callback_query
    await query.answer("Сбросил")
    cancel_upload_status(context, update.effective_chat.id)
    context.user_data["photos"] = []
    context.user_data.pop("upl_status", None)
    try:
        await query.edit_message_text(
            "🗑 Все фото сброшены — присылай заново.", reply_markup=back_keyboard())
    except Exception:
        pass
    return WAITING_PHOTOS


TITLE_PROMPT = ("✍️ Пришли *текст заголовка*.\n"
                "Переносы строк ставь сам — как нужно на обложке.")

STORE_TEXT_PROMPT = ("✍️ Пришли *текст подписи* для STORE — 2 строки.\n"
                     "Перенос между строками ставь сам (Enter).")


def _reuse_keyboard(ud, key: str, cb: str):
    """Клавиатура шага ввода текста: при повторе — кнопка «тот же текст»."""
    rows = []
    if ud.get("repeat") and ud.get(key):
        rows.append([InlineKeyboardButton(f"↩️ Тот же: «{_short(ud[key])}»",
                                          callback_data=cb)])
    rows.append([_BACK_BTN])
    return InlineKeyboardMarkup(rows)


async def _proceed_after_photos(message, context: ContextTypes.DEFAULT_TYPE):
    """Общий шаг после загрузки фото (/done или кнопка «Готово»)."""
    ud = context.user_data
    photos = ud.get("photos", [])
    if not photos:
        await message.reply_text("Сначала отправь хотя бы одно фото!")
        return WAITING_PHOTOS
    mode = ud.get("mode")
    if is_cover(ud):
        await message.reply_text(TITLE_PROMPT, parse_mode="Markdown",
                                 reply_markup=_reuse_keyboard(ud, "title", "reuse:title"))
        return WAITING_TITLE
    if mode == "store":
        await message.reply_text(STORE_TEXT_PROMPT, parse_mode="Markdown",
                                 reply_markup=_reuse_keyboard(ud, "store_text", "reuse:store"))
        return WAITING_STORE_TEXT
    if ud.get("repeat"):
        await message.reply_text("⚙️ Готовлю превью..." if is_collab(ud)
                                 else f"⚙️ Обрабатываю {len(photos)} фото...")
        return await _render_with_hashtag(message, context, ud.get("hashtag", NO_HASHTAG))
    await message.reply_text(
        f"📐 Выбери формат ({len(photos)} фото):", reply_markup=format_kb(ud)
    )
    return CHOOSING_FORMAT


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    cancel_upload_status(context, chat_id)
    await _drop_status_keyboard(context, chat_id)
    return await _proceed_after_photos(update.message, context)


async def photos_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    cancel_upload_status(context, chat_id)
    await _drop_status_keyboard(context, chat_id)
    return await _proceed_after_photos(query.message, context)


async def _after_title(message, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("repeat"):
        await message.reply_text("⚙️ Готовлю превью...")
        return await _render_with_hashtag(message, context,
                                          context.user_data.get("hashtag", NO_HASHTAG))
    await message.reply_text(
        "📐 Выбери формат ленты (сторис IG и TG добавлю автоматически):",
        reply_markup=cover_format_keyboard()
    )
    return CHOOSING_FORMAT


async def receive_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = nfc(update.message.text).strip("\n")
    if not title.strip():
        await update.message.reply_text("Заголовок пустой — пришли текст ещё раз.")
        return WAITING_TITLE
    context.user_data["title"] = title
    return await _after_title(update.message, context)


async def reuse_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    return await _after_title(query.message, context)


async def choose_format(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ud = context.user_data
    ud["format"] = query.data.split(":", 1)[1]
    await query.edit_message_text(
        "Выбери хештег:", reply_markup=hashtag_keyboard(ud.get("channel", "base"), recent_tag(ud)))
    return CHOOSING_HASHTAG


def fixed_need(fmt_key: str):
    """Размер холста брендинга/коллаба для draft-декодирования (None — адаптивный)."""
    size = FORMATS.get(fmt_key)
    return [size] if size else None


async def generate_collab(message, context: ContextTypes.DEFAULT_TYPE):
    """Коллаб-брендинг: рендер всех фото со связкой «ÖMANKÖ × партнёр» и
    хештегом справа, в выбранном цвете (авто / светлый / тёмный)."""
    ud = context.user_data
    fmt = ud.get("format", "4:5")
    photos = ud.get("photos", [])
    hashtag = ud.get("hashtag", NO_HASHTAG)
    color = ud.get("collab_color", "auto")
    partner = partner_for_render(ud.get("partner_id"))
    if not partner:
        await message.reply_text("Партнёр не найден — похоже, его удалили. Начни заново: /start")
        reset_session(context)
        return ConversationHandler.END
    need = fixed_need(fmt)

    def render_one(photo_bytes, i):
        img = open_photo(photo_bytes, need)
        res = process_collab(img, fmt, partner, hashtag=hashtag, color_mode=color)
        return [(to_jpeg(res), f"collab_{i+1}.jpg")], upscale_factor(img, res.size)

    ok = await deliver(message, photos, render_one)
    record_post("base", "collab", ok, partner=partner["name"], kind="type1")
    if ok:
        touch_partner(partner["id"], hashtag)
    return await finish(message, context, ok, len(photos))


async def _after_store_text(message, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("repeat"):
        await message.reply_text("⚙️ Обрабатываю фото...")
        return await generate_store(message, context)
    await message.reply_text(
        "🎨 *Цвет графики* (логотип + текст):",
        parse_mode="Markdown", reply_markup=store_color_keyboard())
    return CHOOSING_STORE_COLOR


async def receive_store_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """STORE: приём подписи (до 2 строк) → выбор цвета (или сразу рендер при повторе)."""
    text = nfc(update.message.text).strip("\n")
    if not text.strip():
        await update.message.reply_text(
            "Текст пустой — пришли подпись ещё раз (2 строки).",
            reply_markup=back_keyboard())
        return WAITING_STORE_TEXT
    context.user_data["store_text"] = text
    return await _after_store_text(update.message, context)


async def reuse_store_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    return await _after_store_text(query.message, context)


def _render_store_preview(photo_bytes: bytes, text: str, idx: int) -> bytes:
    """Превью первого фото на текущем оттенке слайдера (уменьшенное, для скорости).
    Синхронная — вызывается через run_render."""
    res = process_store(open_photo(photo_bytes, [STORE_SIZE]), text, color=_store_gray_value(idx))
    res.thumbnail((1200, 1500), Image.LANCZOS)
    return to_jpeg(res, quality=85)


async def _store_preview(context: ContextTypes.DEFAULT_TYPE, idx: int) -> bytes:
    ud = context.user_data
    return await run_render(_render_store_preview, ud.get("photos", [])[0],
                            ud.get("store_text", ""), idx)


def _store_slider_caption(idx: int) -> str:
    return (f"🎚 Оттенок графики (ЧБ)\n{_store_gray_meter(idx)}\n"
            f"`{_store_gray_hex(idx)}`")


async def choose_store_color(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]

    if choice in ("adaptive", "light", "dark"):
        context.user_data["store_color"] = {
            "adaptive": None,
            "light": STORE_COLOR_LIGHT,
            "dark": STORE_COLOR_DARK,
        }[choice]
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text("⚙️ Обрабатываю фото...")
        return await generate_store(query.message, context)

    # custom → ЧБ-слайдер с живым превью
    idx = STORE_GRAY_DEFAULT_IDX
    context.user_data["store_gray_idx"] = idx
    preview = await _store_preview(context, idx)
    if query.message.photo:
        await query.edit_message_media(
            InputMediaPhoto(media=io.BytesIO(preview),
                            caption=_store_slider_caption(idx), parse_mode="Markdown"),
            reply_markup=store_gray_keyboard(idx))
    else:
        await query.message.reply_photo(
            photo=io.BytesIO(preview), caption=_store_slider_caption(idx),
            parse_mode="Markdown", reply_markup=store_gray_keyboard(idx))
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
    return STORE_COLOR_SLIDER


async def store_gray_slider(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    action = query.data.split(":", 1)[1]

    if action == "noop":
        await query.answer()
        return STORE_COLOR_SLIDER

    if action == "apply":
        idx = context.user_data.get("store_gray_idx", STORE_GRAY_DEFAULT_IDX)
        context.user_data["store_color"] = _store_gray_value(idx)
        await query.answer("Применяю…")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text("⚙️ Обрабатываю фото...")
        return await generate_store(query.message, context)

    idx = context.user_data.get("store_gray_idx", STORE_GRAY_DEFAULT_IDX)
    new_idx = max(0, min(STORE_GRAY_STEPS, idx + (1 if action == "up" else -1)))
    if new_idx == idx:
        await query.answer("Дальше некуда 🙂")
        return STORE_COLOR_SLIDER
    context.user_data["store_gray_idx"] = new_idx
    await query.answer()
    preview = await _store_preview(context, new_idx)
    try:
        await query.edit_message_media(
            InputMediaPhoto(media=io.BytesIO(preview),
                            caption=_store_slider_caption(new_idx), parse_mode="Markdown"),
            reply_markup=store_gray_keyboard(new_idx))
    except Exception as e:
        logger.error(f"STORE слайдер: не смог обновить превью: {e}")
    return STORE_COLOR_SLIDER


async def back_store_color_to_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.message.photo:
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.message.reply_text(
            STORE_TEXT_PROMPT, parse_mode="Markdown", reply_markup=back_keyboard())
    else:
        await query.edit_message_text(
            STORE_TEXT_PROMPT, parse_mode="Markdown", reply_markup=back_keyboard())
    return WAITING_STORE_TEXT


async def back_store_slider_to_color(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await query.edit_message_caption(
            caption="🎨 *Цвет графики* (логотип + текст):",
            parse_mode="Markdown", reply_markup=store_color_keyboard())
    except Exception:
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.message.reply_text(
            "🎨 *Цвет графики* (логотип + текст):",
            parse_mode="Markdown", reply_markup=store_color_keyboard())
    return CHOOSING_STORE_COLOR


async def generate_store(message, context: ContextTypes.DEFAULT_TYPE):
    """STORE: рендер всех фото в формат витрины 2000×2500 и отправка альбомом."""
    photos = context.user_data.get("photos", [])
    text = context.user_data.get("store_text", "")
    color = context.user_data.get("store_color")  # None=адаптивный или (r,g,b)
    context.user_data["store_color"] = color       # ключ нужен для повтора

    def render_one(photo_bytes, i):
        img = open_photo(photo_bytes, [STORE_SIZE])
        res = process_store(img, text, color=color)
        return [(to_jpeg(res), f"store_{i+1}.jpg")], upscale_factor(img, res.size)

    ok = await deliver(message, photos, render_one)
    record_post(context.user_data.get("channel", "base"), "store", ok)
    return await finish(message, context, ok, len(photos))


def cover_need(fmt: str, stories: bool = True):
    """Холсты обложки для draft-декодирования (None — адаптивная лента)."""
    spec = COVER_FORMATS.get(cover_key(fmt))
    if spec is None:
        return None
    return [spec["size"], STORY_SIZE] if stories else [spec["size"]]


async def _send_covers(message, photos, title, hashtag, fmt, channel, dark_idx,
                       partner=None) -> int:
    """Рендер + отправка обложек (feed/ig/tg) для всех фото на заданном уровне
    затемнения. Тройка одного фото всегда уходит в одном альбоме. partner —
    пресет коллаба: тогда вместо вордмарка/лого канала стоит связка.
    Возвращает число успешно обработанных фото."""
    level = DARK_LEVELS[dark_idx]
    need = cover_need(fmt)
    prefix = "collab_cover" if partner else "cover"

    def render_one(photo_bytes, i):
        img = open_photo(photo_bytes, need)
        feed = render_cover_feed(img, fmt, title, hashtag, dark_level=level, partner=partner)
        ig = render_cover_story(img, "ig", title, hashtag, channel=channel, dark_level=level,
                                partner=partner)
        tg = render_cover_story(img, "tg", title, hashtag, channel=channel, dark_level=level,
                                partner=partner)
        factor = max(upscale_factor(img, feed.size), upscale_factor(img, STORY_SIZE))
        return [(to_jpeg(r), f"{prefix}_{i+1}_{sfx}.jpg")
                for r, sfx in ((feed, "feed"), (ig, "ig"), (tg, "tg"))], factor

    return await deliver(message, photos, render_one)


def _render_cover_preview(photo_bytes, fmt, title, hashtag, idx, partner=None) -> bytes:
    """Превью обложки (лента, первое фото) на текущем уровне затемнения —
    уменьшенное, для скорости. Синхронная — вызывается через run_render."""
    res = render_cover_feed(open_photo(photo_bytes, cover_need(fmt, stories=False)), fmt,
                            title, hashtag, dark_level=DARK_LEVELS[idx], partner=partner)
    res.thumbnail((1200, 1500), Image.LANCZOS)
    return to_jpeg(res, quality=85)


async def _cover_preview(context: ContextTypes.DEFAULT_TYPE, idx: int) -> bytes:
    sess = context.user_data["cover_session"]
    return await run_render(_render_cover_preview, sess["photos"][0], sess["fmt"],
                            sess["title"], sess["hashtag"], idx,
                            partner_for_render(sess.get("partner_id")))


def _cover_slider_caption(idx: int) -> str:
    return ("🎚 Затемнение градиента — подбери под кадр.\n"
            "Превью на первом фото 👇\n\n"
            f"*Уровень:* {dark_meter(idx)}")


async def _render_with_hashtag(message, context: ContextTypes.DEFAULT_TYPE, hashtag: str):
    """Общий рендер для обоих путей выбора хештега (кнопка из списка / свой текст)
    и для повтора. Статус «Обрабатываю…» каждый путь шлёт сам."""
    ud = context.user_data
    ud["hashtag"] = hashtag
    fmt = ud.get("format", "4:5")
    photos = ud.get("photos", [])
    mode = ud.get("mode", "type1")
    channel = ud.get("channel", "base")

    if is_collab(ud) and not partner_for_render(ud.get("partner_id")):
        await message.reply_text("Партнёр не найден — похоже, его удалили. Начни заново: /start")
        reset_session(context)
        return ConversationHandler.END

    if is_cover(ud):
        title = ud.get("title", "")
        # При повторе слайдер стартует с прошлого уровня затемнения
        idx = ud.get("dark_idx", DARK_DEFAULT_IDX)
        # Не генерируем сразу: сначала слайдер затемнения с живым превью.
        ud["cover_session"] = {
            "photos": photos, "title": title, "hashtag": hashtag,
            "fmt": fmt, "channel": channel, "dark_idx": idx,
            "partner_id": ud.get("partner_id") if is_collab(ud) else None,
        }
        preview = await _cover_preview(context, idx)
        await message.reply_photo(
            photo=io.BytesIO(preview), caption=_cover_slider_caption(idx),
            parse_mode="Markdown", reply_markup=cover_dark_keyboard(idx))
        return COVER_DARK_SLIDER

    if is_collab(ud):
        # Коллаб-брендинг: сначала превью цвета связки на первом фото
        ud.setdefault("collab_color", "auto")
        preview = await _collab_preview(context)
        await message.reply_photo(
            photo=io.BytesIO(preview), caption=_collab_color_caption(ud["collab_color"]),
            parse_mode="Markdown", reply_markup=collab_color_keyboard(ud["collab_color"]))
        return COLLAB_COLOR

    # ---- Тип 1 (брендинг) ----
    need = fixed_need(fmt)

    def render_one(photo_bytes, i):
        img = open_photo(photo_bytes, need)
        res = process_image(img, fmt, hashtag, channel=channel)
        return [(to_jpeg(res), f"1_{i+1}.jpg")], upscale_factor(img, res.size)

    ok = await deliver(message, photos, render_one)
    record_post(channel, mode, ok)
    return await finish(message, context, ok, len(photos))


async def choose_hashtag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tag = query.data.split(":", 1)[1]

    # Свой хештег — уводим в ввод текста, рендер будет после ввода
    if tag == CUSTOM_HASHTAG_CB:
        await query.edit_message_text(
            "✍️ Кидай свой хештег одним словом 🔥\n"
            "Можно с # или без — решётку добавлю сам. Например: лето\n"
            f"До {CUSTOM_HASHTAG_MAX} символов.",
            reply_markup=back_keyboard()
        )
        return WAITING_CUSTOM_HASHTAG

    ud = context.user_data
    photos = ud.get("photos", [])
    await query.edit_message_text("⚙️ Готовлю превью..." if (is_cover(ud) or is_collab(ud))
                                  else f"⚙️ Обрабатываю {len(photos)} фото...")
    return await _render_with_hashtag(query.message, context, tag)


async def receive_custom_hashtag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = nfc(update.message.text).strip()
    token = raw.split()[0] if raw.split() else ""
    token = token.lstrip("#").strip()
    if not token:
        await update.message.reply_text("Пустой хештег — пришли ещё раз, например: лето")
        return WAITING_CUSTOM_HASHTAG
    # Только буквы (латиница/кириллица), цифры и _ — эмодзи и символов в шрифте
    # нет, на картинке вышли бы пустые квадраты.
    if any(not (c == "_" or (c.isalnum() and ord(c) <= 0x052F)) for c in token):
        await update.message.reply_text(
            "В хештеге можно только буквы, цифры и _ — эмодзи и значков в нашем шрифте "
            "нет, на картинке вышли бы пустые квадраты. Пришли ещё раз 🙂",
            reply_markup=back_keyboard())
        return WAITING_CUSTOM_HASHTAG
    if len(token) > CUSTOM_HASHTAG_MAX:
        await update.message.reply_text(
            f"Длинновато 😅 Максимум {CUSTOM_HASHTAG_MAX} символов, а тут {len(token)}. "
            "Пришли покороче.", reply_markup=back_keyboard())
        return WAITING_CUSTOM_HASHTAG
    hashtag = "#" + token

    ud = context.user_data
    photos = ud.get("photos", [])
    await update.message.reply_text("⚙️ Готовлю превью..." if (is_cover(ud) or is_collab(ud))
                                    else f"⚙️ Обрабатываю {len(photos)} фото с {hashtag}...")
    return await _render_with_hashtag(update.message, context, hashtag)


async def cover_dark_slider(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Слайдер затемнения обложки с превью. Двигает уровень и перерисовывает
    превью на первом фото; по ✅ Сгенерировать — финальный рендер всех кадров
    (лента + IG + TG) на выбранном уровне затемнения."""
    query = update.callback_query
    action = query.data.split(":", 1)[1]

    sess = context.user_data.get("cover_session")
    if not sess:
        await query.answer("Сессия устарела — сделай новый пост через /start",
                           show_alert=True)
        return COVER_DARK_SLIDER

    if action == "noop":
        await query.answer()
        return COVER_DARK_SLIDER

    if action == "apply":
        idx = sess["dark_idx"]
        await query.answer("Генерирую…")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        partner = partner_for_render(sess.get("partner_id"))
        if sess.get("partner_id") and not partner:
            await query.message.reply_text("Партнёр не найден — похоже, его удалили. /start")
            reset_session(context)
            return ConversationHandler.END
        await query.message.reply_text("⚙️ Рендерю обложки и сторис...")
        ok = await _send_covers(query.message, sess["photos"], sess["title"],
                                sess["hashtag"], sess["fmt"], sess["channel"], idx,
                                partner=partner)
        if partner:
            record_post("base", "collab", ok, partner=partner["name"], kind="cover")
            if ok:
                touch_partner(partner["id"], sess["hashtag"])
        else:
            record_post(sess["channel"], "cover", ok)
        context.user_data["dark_idx"] = idx
        context.user_data.pop("cover_session", None)
        return await finish(query.message, context, ok, len(sess["photos"]))

    idx = sess["dark_idx"]
    new_idx = max(0, min(len(DARK_LEVELS) - 1, idx + (1 if action == "up" else -1)))
    if new_idx == idx:
        await query.answer("Дальше некуда 🙂")
        return COVER_DARK_SLIDER
    sess["dark_idx"] = new_idx
    await query.answer()
    preview = await _cover_preview(context, new_idx)
    try:
        await query.edit_message_media(
            InputMediaPhoto(media=io.BytesIO(preview),
                            caption=_cover_slider_caption(new_idx), parse_mode="Markdown"),
            reply_markup=cover_dark_keyboard(new_idx))
    except Exception as e:
        logger.error(f"COVER слайдер: не смог обновить превью: {e}")
    return COVER_DARK_SLIDER


async def back_cover_slider(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Назад со слайдера затемнения → к выбору хештега."""
    query = update.callback_query
    await query.answer()
    ud = context.user_data
    try:
        await query.message.delete()
    except Exception:
        pass
    await query.message.reply_text(
        "Выбери хештег:", reply_markup=hashtag_keyboard(ud.get("channel", "base"), recent_tag(ud)))
    return CHOOSING_HASHTAG


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_upload_status(context, update.effective_chat.id)
    reset_session(context)
    await update.message.reply_text("Отменено. /start чтобы начать заново.")
    return ConversationHandler.END


async def on_timeout(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Сессия брошена дольше CONV_TIMEOUT_SEC: освобождаем память (фото, логотип
    партнёра) и коротко сообщаем. Настройки для повтора сохраняются."""
    chat = update.effective_chat if isinstance(update, Update) else None
    if chat:
        cancel_upload_status(context, chat.id)
    reset_session(context)
    if chat:
        try:
            await context.bot.send_message(
                chat.id,
                "⏳ Сессия закрыта — полчаса тишины. Загруженные фото удалил из памяти.\n"
                "/start — начать заново.")
        except Exception:
            pass


async def stale_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Нажата кнопка из старого/закрытого диалога — отвечаем, чтобы не крутился
    бесконечный «часик» на кнопке."""
    try:
        await update.callback_query.answer(
            "Эта кнопка уже неактуальна 🙂 /start — новый пост", show_alert=False)
    except Exception:
        pass


# ============ Коллаб: партнёры, загрузка лого, подстройка, управление ============
def png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def png_image(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGBA")


def _esc(text: str) -> str:
    return escape_markdown(text or "", version=1)


async def to_text(query, text: str, kb, markdown: bool = True):
    """Текстовый экран из колбэка. Если текущее сообщение — картинка (превью),
    его не отредактировать в текст: удаляем и шлём новое."""
    pm = "Markdown" if markdown else None
    if query.message.photo:
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.message.reply_text(text, parse_mode=pm, reply_markup=kb)
    else:
        await query.edit_message_text(text, parse_mode=pm, reply_markup=kb)


async def to_photo(query, photo: bytes, caption: str, kb):
    """Экран с картинкой из колбэка: меняем медиа или шлём новое сообщение."""
    if query.message.photo:
        await query.edit_message_media(
            InputMediaPhoto(media=io.BytesIO(photo), caption=caption, parse_mode="Markdown"),
            reply_markup=kb)
    else:
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.message.reply_photo(photo=io.BytesIO(photo), caption=caption,
                                        parse_mode="Markdown", reply_markup=kb)


def partner_list_text(manage: bool = False) -> str:
    warn = ("" if STORAGE_PERSISTENT else
            "\n\n⚠️ Хранилище временное — пресеты пропадут при деплое. Подключи Volume в Railway.")
    if manage:
        return "⚙️ *Партнёры* — кого правим?" + warn
    if not load_partners():
        return ("🤝 *Коллаборация*\n\nПартнёров пока нет — добавь первого 👇\n"
                "Логотип загружается один раз и дальше хранится в боте для всей команды." + warn)
    return "🤝 *Коллаборация*\n\nС кем коллаб?" + warn


def kind_text(p: dict) -> str:
    return f"🤝 *ÖMANKÖ × {_esc(p['name'])}*\n\nЧто делаем?"


def logo_prompt_text() -> str:
    kinds = ("PNG без фона, SVG или JPG на однотонном фоне" if SVG_OK
             else "PNG без фона или JPG на однотонном фоне")
    return ("🖼 Пришли *логотип партнёра* — лучше файлом (скрепка → Файл).\n\n"
            f"Подойдёт {kinds}: фон уберу, поля обрежу и перекрашу под связку сам.")


def name_keyboard(suggest=None):
    rows = []
    if suggest:
        rows.append([InlineKeyboardButton(f"✅ «{suggest}»", callback_data="pname:file")])
    rows.append([_BACK_BTN])
    return InlineKeyboardMarkup(rows)


def _tune_caption(d: dict) -> str:
    return (f"🤝 ÖMANKÖ × *{_esc(d.get('name', '?'))}*\n"
            f"Размер: *{round(d['scale'] * 100)}%* · Сдвиг: *{int(d['dy']):+d} px*\n\n"
            "Сверху — как в обложке, снизу — как в брендинге на светлом фото. "
            "Подгони, чтобы логотип партнёра весил наравне с ÖMANKÖ.")


async def _tune_preview(d: dict) -> bytes:
    return await run_render(render_partner_preview, png_image(d["sil"]), d["scale"], d["dy"])


def _card_caption(p: dict) -> str:
    last = ""
    if p.get("last_used"):
        try:
            dt = datetime.fromisoformat(p["last_used"]).astimezone(MSK)
            last = f" · последний {_ru_date(dt)}"
        except Exception:
            pass
    return (f"🤝 *{_esc(p['name'])}*\n"
            f"Размер {round(float(p.get('scale', 1.0)) * 100)}% · сдвиг {int(p.get('dy', 0)):+d} px\n"
            f"Коллабов: {int(p.get('uses', 0))}{last}")


async def show_card(query, pid: str):
    p = load_partners().get(pid)
    pr = partner_for_render(pid)
    if not p or not pr:
        await to_text(query, partner_list_text(manage=True), partner_list_keyboard(manage=True))
        return
    preview = await run_render(render_partner_preview, pr["img"], pr["scale"], pr["dy"])
    await to_photo(query, preview, _card_caption(p), partner_card_keyboard(pid))


async def send_card(message, pid: str):
    p = load_partners().get(pid)
    pr = partner_for_render(pid)
    if not p or not pr:
        await message.reply_text(partner_list_text(manage=True), parse_mode="Markdown",
                                 reply_markup=partner_list_keyboard(manage=True))
        return
    preview = await run_render(render_partner_preview, pr["img"], pr["scale"], pr["dy"])
    await message.reply_photo(photo=io.BytesIO(preview), caption=_card_caption(p),
                              parse_mode="Markdown", reply_markup=partner_card_keyboard(pid))


async def partner_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список партнёров: выбрать / ➕ новый / ⚙️ управление / листание."""
    query = update.callback_query
    ud = context.user_data
    data = query.data.split(":", 1)[1]
    if data == "noop":
        await query.answer()
        return CHOOSING_PARTNER
    if data == "new":
        await query.answer()
        ud["logo_target"] = "new"
        await to_text(query, logo_prompt_text(), back_keyboard())
        return WAITING_PARTNER_LOGO
    if data == "manage":
        await query.answer()
        await to_text(query, partner_list_text(manage=True), partner_list_keyboard(manage=True))
        return PARTNER_MANAGE
    if data.startswith("page:"):
        await query.answer()
        await query.edit_message_reply_markup(
            reply_markup=partner_list_keyboard(int(data.split(":")[1])))
        return CHOOSING_PARTNER
    p = load_partners().get(data)
    if not p:
        await query.answer("Этого партнёра уже удалили", show_alert=True)
        await to_text(query, partner_list_text(), partner_list_keyboard())
        return CHOOSING_PARTNER
    await query.answer()
    ud["partner_id"] = data
    await to_text(query, kind_text(p), collab_kind_keyboard())
    return CHOOSING_COLLAB_KIND


async def choose_collab_kind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ud = context.user_data
    if not load_partners().get(ud.get("partner_id") or ""):
        await to_text(query, partner_list_text(), partner_list_keyboard())
        return CHOOSING_PARTNER
    ud["collab_kind"] = "cover" if query.data == "ck:cover" else "type1"
    await to_text(query, photos_prompt_text(context), back_keyboard())
    return WAITING_PHOTOS


async def receive_partner_logo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Логотип партнёра в любом виде → белый силуэт без полей → имя / подстройка."""
    msg = update.message
    ud = context.user_data
    doc = msg.document
    fname = (doc.file_name or "") if doc else ""
    if doc and not ((doc.mime_type or "").startswith("image/") or fname.lower().endswith(".svg")):
        await msg.reply_text("Это не картинка 🙃 Пришли логотип: PNG, SVG или JPG.",
                             reply_markup=back_keyboard())
        return WAITING_PARTNER_LOGO
    data = await download_media(msg)
    if data is None:
        return WAITING_PARTNER_LOGO
    try:
        src = await run_render(rasterize_logo, data, fname)
        sil = await run_render(logo_silhouette, src, "sil")
    except ValueError as e:
        text = {
            "svg_unsupported": "SVG сервер пока не открывает — пришли, пожалуйста, PNG или JPG.",
            "empty_logo": "Не нашёл на картинке логотипа — она пустая или однотонная. Пришли другой файл.",
        }.get(str(e), "Не смог открыть логотип — пришли PNG, SVG или JPG ещё раз.")
        await msg.reply_text(text, reply_markup=back_keyboard())
        return WAITING_PARTNER_LOGO
    except Exception:
        logger.exception("Логотип партнёра не открылся")
        await msg.reply_text("Не смог открыть логотип — пришли PNG, SVG или JPG ещё раз.",
                             reply_markup=back_keyboard())
        return WAITING_PARTNER_LOGO

    draft = {"src": png_bytes(src), "sil": png_bytes(sil), "mask": "sil",
             "contrast": logo_has_inner_contrast(src), "scale": 1.0, "dy": 0.0}
    target = ud.pop("logo_target", "new")  # метка использована — дальше не нужна
    if target.startswith("replace:"):
        pid = target.split(":", 1)[1]
        p = load_partners().get(pid)
        if not p:
            await msg.reply_text(partner_list_text(), parse_mode="Markdown",
                                 reply_markup=partner_list_keyboard())
            return CHOOSING_PARTNER
        draft.update(pid=pid, name=p["name"], scale=float(p.get("scale", 1.0)),
                     dy=float(p.get("dy", 0)))
        ud["draft"] = draft
        await msg.reply_photo(photo=io.BytesIO(await _tune_preview(draft)),
                              caption=_tune_caption(draft), parse_mode="Markdown",
                              reply_markup=partner_tune_keyboard(draft))
        return PARTNER_TUNE
    draft["suggest"] = name_from_filename(fname)
    ud["draft"] = draft
    ud["name_target"] = "new"
    await msg.reply_text("✍️ Как назовём партнёра? Так он будет подписан на кнопке.",
                         reply_markup=name_keyboard(draft["suggest"]))
    return WAITING_PARTNER_NAME


async def logo_wrong_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Жду картинку логотипа — файлом или фото 🙂",
                                    reply_markup=back_keyboard())
    return WAITING_PARTNER_LOGO


async def _apply_partner_name(message, context, name: str):
    ud = context.user_data
    target = ud.get("name_target", "new")
    name = " ".join(nfc(name).split())
    if not name:
        await message.reply_text("Название пустое — напиши ещё раз.", reply_markup=back_keyboard())
        return WAITING_PARTNER_NAME
    if len(name) > PARTNER_NAME_MAX:
        await message.reply_text(f"Длинновато — до {PARTNER_NAME_MAX} символов, а тут {len(name)}.",
                                 reply_markup=back_keyboard())
        return WAITING_PARTNER_NAME
    except_pid = target.split(":", 1)[1] if target.startswith("rename:") else None
    if partner_name_taken(name, except_pid):
        await message.reply_text(f"«{name}» уже есть в списке — выбери его там или назови иначе.",
                                 reply_markup=back_keyboard())
        return WAITING_PARTNER_NAME
    if except_pid:
        ps = load_partners()
        ud.pop("name_target", None)
        if except_pid in ps:
            ps[except_pid]["name"] = name
            save_partners(ps)
        await send_card(message, except_pid)
        return PARTNER_MANAGE
    d = ud.get("draft")
    if not d:
        await message.reply_text("Сессия устарела — начни заново: /start")
        return ConversationHandler.END
    d["name"] = name
    await message.reply_photo(photo=io.BytesIO(await _tune_preview(d)), caption=_tune_caption(d),
                              parse_mode="Markdown", reply_markup=partner_tune_keyboard(d))
    return PARTNER_TUNE


async def receive_partner_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _apply_partner_name(update.message, context, update.message.text or "")


async def partner_name_from_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    suggest = (context.user_data.get("draft") or {}).get("suggest") or ""
    return await _apply_partner_name(query.message, context, suggest)


async def partner_tune(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """➖/➕ размер, ⬆️/⬇️ сдвиг, 🔄 маска, ✅ сохранить — с живым превью."""
    query = update.callback_query
    ud = context.user_data
    d = ud.get("draft")
    if not d:
        await query.answer("Сессия устарела — начни заново: /start", show_alert=True)
        return PARTNER_TUNE
    action = query.data.split(":", 1)[1]
    if action == "save":
        await query.answer("Сохраняю…")
        return await _save_draft(query, context)
    if action in ("+", "-"):
        new = round(min(PARTNER_SCALE_MAX, max(PARTNER_SCALE_MIN,
                    d["scale"] + (PARTNER_SCALE_STEP if action == "+" else -PARTNER_SCALE_STEP))), 2)
        if new == d["scale"]:
            await query.answer("Дальше некуда 🙂")
            return PARTNER_TUNE
        d["scale"] = new
    elif action in ("up", "down"):
        new = max(-PARTNER_DY_MAX, min(PARTNER_DY_MAX,
                  d["dy"] + (-PARTNER_DY_STEP if action == "up" else PARTNER_DY_STEP)))
        if new == d["dy"]:
            await query.answer("Дальше некуда 🙂")
            return PARTNER_TUNE
        d["dy"] = new
    elif action == "mask" and d.get("src"):
        order = list(MASK_NAMES)
        mask = order[(order.index(d.get("mask", "sil")) + 1) % len(order)]
        try:
            sil = await run_render(logo_silhouette, png_image(d["src"]), mask)
        except ValueError:
            await query.answer("С этой маской логотип пустой — пропускаю")
            return PARTNER_TUNE
        d["mask"], d["sil"] = mask, png_bytes(sil)
    else:
        await query.answer()
        return PARTNER_TUNE
    await query.answer()
    try:
        await query.edit_message_media(
            InputMediaPhoto(media=io.BytesIO(await _tune_preview(d)), caption=_tune_caption(d),
                            parse_mode="Markdown"),
            reply_markup=partner_tune_keyboard(d))
    except Exception as e:
        logger.error(f"Превью пресета не обновилось: {e}")
    return PARTNER_TUNE


async def _save_draft(query, context):
    ud = context.user_data
    d = ud.pop("draft")
    sil = png_image(d["sil"])
    src = png_image(d["src"]) if d.get("src") else None
    ps = load_partners()
    now = datetime.now(timezone.utc).isoformat()
    if d.get("pid"):  # правка существующего
        pid = d["pid"]
        if pid not in ps:
            await to_text(query, partner_list_text(manage=True), partner_list_keyboard(manage=True))
            return PARTNER_MANAGE
        ps[pid].update(scale=d["scale"], dy=d["dy"], mask=d.get("mask", "sil"))
        save_partners(ps)
        save_partner_images(pid, sil, src)
        await show_card(query, pid)
        return PARTNER_MANAGE
    pid = secrets.token_hex(4)
    while pid in ps:
        pid = secrets.token_hex(4)
    ps[pid] = {"name": d["name"], "scale": d["scale"], "dy": d["dy"], "mask": d.get("mask", "sil"),
               "hashtag": None, "uses": 0, "created_at": now,
               "created_by": query.from_user.id if query.from_user else None}
    save_partners(ps)
    save_partner_images(pid, sil, src)
    ud["partner_id"] = pid
    try:
        await query.edit_message_caption(
            caption=f"✅ Сохранил *ÖMANKÖ × {_esc(d['name'])}* — теперь он в списке у всей команды.",
            parse_mode="Markdown", reply_markup=None)
    except Exception:
        pass
    await query.message.reply_text(kind_text(ps[pid]), parse_mode="Markdown",
                                   reply_markup=collab_kind_keyboard())
    return CHOOSING_COLLAB_KIND


async def partner_manage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """⚙️ Управление: карточка партнёра, переименовать, заменить лого, размер, удалить."""
    query = update.callback_query
    ud = context.user_data
    data = query.data.split(":", 1)[1]
    if data == "noop":
        await query.answer()
        return PARTNER_MANAGE
    if data == "list":
        await query.answer()
        await to_text(query, partner_list_text(manage=True), partner_list_keyboard(manage=True))
        return PARTNER_MANAGE
    if data.startswith("page:"):
        await query.answer()
        await query.edit_message_reply_markup(
            reply_markup=partner_list_keyboard(int(data.split(":")[1]), manage=True))
        return PARTNER_MANAGE
    action, pid = data.split(":", 1) if ":" in data else ("open", data)
    p = load_partners().get(pid)
    if not p:
        await query.answer("Этого партнёра уже удалили", show_alert=True)
        await to_text(query, partner_list_text(manage=True), partner_list_keyboard(manage=True))
        return PARTNER_MANAGE
    await query.answer("Удалил" if action == "delok" else None)
    if action == "open":
        await show_card(query, pid)
        return PARTNER_MANAGE
    if action == "ren":
        ud["name_target"] = f"rename:{pid}"
        await to_text(query, f"✍️ Новое название для *{_esc(p['name'])}*:", back_keyboard())
        return WAITING_PARTNER_NAME
    if action == "logo":
        ud["logo_target"] = f"replace:{pid}"
        await to_text(query, logo_prompt_text(), back_keyboard())
        return WAITING_PARTNER_LOGO
    if action == "size":
        sil = load_partner_image(pid)
        src = load_partner_image(pid, src=True)
        if sil is None:
            await to_text(query, partner_list_text(manage=True), partner_list_keyboard(manage=True))
            return PARTNER_MANAGE
        d = {"pid": pid, "name": p["name"], "sil": png_bytes(sil),
             "src": png_bytes(src) if src else None, "mask": p.get("mask", "sil"),
             "contrast": bool(src) and logo_has_inner_contrast(src),
             "scale": float(p.get("scale", 1.0)), "dy": float(p.get("dy", 0))}
        ud["draft"] = d
        await to_photo(query, await _tune_preview(d), _tune_caption(d), partner_tune_keyboard(d))
        return PARTNER_TUNE
    if action == "del":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Да, удалить", callback_data=f"pm:delok:{pid}"),
                                    InlineKeyboardButton("Отмена", callback_data=f"pm:{pid}")]])
        text = (f"Удалить *{_esc(p['name'])}* для всей команды? "
                "Логотип потом придётся загружать заново.")
        if query.message.photo:
            await query.edit_message_caption(caption=text, parse_mode="Markdown", reply_markup=kb)
        else:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
        return PARTNER_MANAGE
    if action == "delok":
        delete_partner(pid)
        await to_text(query, partner_list_text(manage=True), partner_list_keyboard(manage=True))
        return PARTNER_MANAGE
    return PARTNER_MANAGE


# ---- Коллаб-брендинг: цвет связки с превью ----
def _collab_color_caption(cur: str) -> str:
    return ("🎨 *Цвет связки* — превью на первом фото 👇\n"
            "Авто выбирает светлый или тёмный по фону, как в брендинге.\n\n"
            f"*Сейчас:* {COLLAB_COLORS.get(cur, COLLAB_COLORS['auto'])}")


def _render_collab_preview(photo_bytes, fmt, partner, hashtag, color) -> bytes:
    res = process_collab(open_photo(photo_bytes, fixed_need(fmt)), fmt, partner,
                         hashtag=hashtag, color_mode=color)
    res.thumbnail((1200, 1500), Image.LANCZOS)
    return to_jpeg(res, quality=85)


async def _collab_preview(context) -> bytes:
    ud = context.user_data
    return await run_render(_render_collab_preview, ud["photos"][0], ud.get("format", "4:5"),
                            partner_for_render(ud.get("partner_id")),
                            ud.get("hashtag", NO_HASHTAG), ud.get("collab_color", "auto"))


async def collab_color_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    ud = context.user_data
    action = query.data.split(":", 1)[1]
    if not ud.get("photos") or not partner_for_render(ud.get("partner_id")):
        await query.answer("Сессия устарела — начни заново: /start", show_alert=True)
        return COLLAB_COLOR
    if action == "go":
        await query.answer("Генерирую…")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text(f"⚙️ Обрабатываю {len(ud['photos'])} фото...")
        return await generate_collab(query.message, context)
    if action not in COLLAB_COLORS:
        await query.answer()
        return COLLAB_COLOR
    if action == ud.get("collab_color", "auto"):
        await query.answer("Уже выбрано 🙂")
        return COLLAB_COLOR
    ud["collab_color"] = action
    await query.answer()
    try:
        await query.edit_message_media(
            InputMediaPhoto(media=io.BytesIO(await _collab_preview(context)),
                            caption=_collab_color_caption(action), parse_mode="Markdown"),
            reply_markup=collab_color_keyboard(action))
    except Exception as e:
        logger.error(f"Превью цвета коллаба не обновилось: {e}")
    return COLLAB_COLOR


# ---- «Назад» в коллабе ----
async def back_collab_to_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Назад из «Что делаем?» — к списку партнёров."""
    query = update.callback_query
    await query.answer()
    await to_text(query, partner_list_text(), partner_list_keyboard())
    return CHOOSING_PARTNER


async def back_from_logo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Назад из загрузки лого: новый партнёр → список, замена лого → карточка."""
    query = update.callback_query
    await query.answer()
    target = context.user_data.pop("logo_target", "new")
    pid = target.split(":", 1)[1] if target.startswith("replace:") else None
    if pid and load_partners().get(pid):
        await show_card(query, pid)
        return PARTNER_MANAGE
    await to_text(query, partner_list_text(), partner_list_keyboard())
    return CHOOSING_PARTNER


async def back_from_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ud = context.user_data
    target = ud.pop("name_target", "new")
    if target.startswith("rename:"):
        await show_card(query, target.split(":", 1)[1])
        return PARTNER_MANAGE
    ud.pop("draft", None)
    ud["logo_target"] = "new"
    await to_text(query, logo_prompt_text(), back_keyboard(), markdown=True)
    return WAITING_PARTNER_LOGO


async def back_from_tune(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ud = context.user_data
    d = ud.get("draft") or {}
    if d.get("pid"):  # правка — выходим без сохранения
        ud.pop("draft", None)
        await show_card(query, d["pid"])
        return PARTNER_MANAGE
    ud["name_target"] = "new"
    await to_text(query, "✍️ Как назовём партнёра? Так он будет подписан на кнопке.",
                  name_keyboard(d.get("suggest")), markdown=False)
    return WAITING_PARTNER_NAME


async def back_manage_to_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await to_text(query, partner_list_text(), partner_list_keyboard())
    return CHOOSING_PARTNER


async def back_collab_color(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ud = context.user_data
    await to_text(query, "Выбери хештег:",
                  hashtag_keyboard(ud.get("channel", "base"), recent_tag(ud)), markdown=False)
    return CHOOSING_HASHTAG


async def back_to_hashtags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Назад из ввода своего хештега — к списку хештегов (а не к форматам)."""
    query = update.callback_query
    await query.answer()
    ud = context.user_data
    await query.edit_message_text(
        "Выбери хештег:", reply_markup=hashtag_keyboard(ud.get("channel", "base"), recent_tag(ud)))
    return CHOOSING_HASHTAG


# ============ Тишины больше нет: ошибки и «лишние» сообщения ============
async def photos_wrong_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """На шаге фото пришло не фото — подсказываем, а не молчим."""
    m = update.message
    mime = (m.document.mime_type or "") if m.document else ""
    if m.video or m.animation or mime.startswith("video/"):
        text = "🎬 Видео пока не умею — только картинки."
    elif m.document:
        text = "📄 Это не картинка. Пришли JPG, PNG, HEIC или WebP."
    else:
        text = "Жду фото 🙂 Когда закончишь — жми «Готово» или /done."
    await m.reply_text(text)
    return WAITING_PHOTOS


async def orphan_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Фото прислали вне шага загрузки (например, без /start) — раньше бот
    молчал. Отвечаем один раз на альбом."""
    m = update.effective_message
    if m.media_group_id and context.chat_data.get("orphan_mg") == m.media_group_id:
        return
    context.chat_data["orphan_mg"] = m.media_group_id
    if context.user_data.get("mode"):
        text = "Сейчас я жду не фото 🙂 Посмотри последнее сообщение — или /start, чтобы начать заново."
    else:
        text = "📸 Вижу фото! Чтобы сделать пост, нажми /start — фото пришлёшь на следующем шаге."
    await m.reply_text(text)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Любая необработанная ошибка: в лог с трейсом, человеку — короткое сообщение
    (раньше он просто не получал ответа). Сетевые подвисания не дёргаем."""
    err = context.error
    transient = isinstance(err, (TimedOut, RetryAfter)) or type(err) is NetworkError
    if transient:
        logger.warning(f"Сетевая ошибка Telegram: {err}")
        return
    logger.error("Необработанная ошибка", exc_info=err)
    chat = update.effective_chat if isinstance(update, Update) else None
    if chat:
        try:
            await context.bot.send_message(
                chat.id, "😵 Что-то пошло не так — ошибку я записал. Попробуй ещё раз, "
                         "а если повторится — /start.")
        except Exception:
            pass


# ============ Навигация «Назад» ============
async def back_to_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Что делаем?", reply_markup=type_keyboard())
    return CHOOSING_TYPE


async def back_to_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ud = context.user_data
    cancel_upload_status(context, update.effective_chat.id)
    if ud.get("upl_status") == query.message.message_id:
        ud.pop("upl_status", None)  # это сообщение сейчас станет меню — не удалять
    ud.pop("repeat", None)  # ушли назад — дальше обычный путь со всеми шагами
    mode = ud.get("mode", "type1")
    if mode == "collab":
        p = load_partners().get(ud.get("partner_id") or "")
        if not p:
            await query.edit_message_text(partner_list_text(), parse_mode="Markdown",
                                          reply_markup=partner_list_keyboard())
            return CHOOSING_PARTNER
        await query.edit_message_text(kind_text(p), parse_mode="Markdown",
                                      reply_markup=collab_kind_keyboard())
        return CHOOSING_COLLAB_KIND
    if mode == "store":
        await query.edit_message_text("Что делаем?", reply_markup=type_keyboard())
        return CHOOSING_TYPE
    name = "Обложка" if mode == "cover" else "Брендинг"
    await query.edit_message_text(
        f"Режим: *{name}*\n\nТеперь выбери канал:",
        parse_mode="Markdown", reply_markup=channel_keyboard())
    return CHOOSING_CHANNEL


async def back_to_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        photos_prompt_text(context), parse_mode="Markdown", reply_markup=back_keyboard())
    return WAITING_PHOTOS


async def back_from_format(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Из выбора формата назад: в обложке → к заголовку, в Тип 1 → к фото."""
    query = update.callback_query
    await query.answer()
    if is_cover(context.user_data):
        await query.edit_message_text(
            TITLE_PROMPT, parse_mode="Markdown", reply_markup=back_keyboard())
        return WAITING_TITLE
    await query.edit_message_text(
        photos_prompt_text(context), parse_mode="Markdown", reply_markup=back_keyboard())
    return WAITING_PHOTOS


async def back_to_format(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    photos = context.user_data.get("photos", [])
    await query.edit_message_text(
        f"📐 Выбери формат ({len(photos)} фото):", reply_markup=format_kb(context.user_data))
    return CHOOSING_FORMAT


# ============ Рассылка ============
async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    # Пока ADMIN_ID не задан — отвечаем всем (разовая настройка: так ты узнаёшь
    # свой ID). Как только ADMIN_ID прописан — команда отвечает только тебе,
    # для остальных её как будто не существует.
    if ADMIN_ID != 0 and uid != ADMIN_ID:
        return
    await update.message.reply_text(
        f"Твой Telegram ID: `{uid}`\n\n"
        "Чтобы включить рассылку, добавь его в Railway: "
        "Variables → ADMIN_ID → этот номер, затем передеплой.",
        parse_mode="Markdown"
    )


async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    # Любой, кроме админа, — молча игнорируем, чтобы для остальных
    # пользователей ничего не менялось. (Пока ADMIN_ID == 0, не совпадёт
    # ни с кем: сначала задай ADMIN_ID, потом пользуйся рассылкой.)
    if uid != ADMIN_ID:
        return ConversationHandler.END
    n = len(load_users())
    await update.message.reply_text(
        f"📣 Рассылка по {n} пользователям.\n\n"
        "Пришли сообщение, которое разослать (текст, фото, что угодно — "
        "уйдёт как есть). /cancel — отмена."
    )
    return BROADCAST_MSG


async def broadcast_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["bc_chat"] = update.effective_chat.id
    context.user_data["bc_msg"] = update.message.message_id
    n = len(load_users())
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ Отправить ({n})", callback_data="bc:go"),
        InlineKeyboardButton("❌ Отмена", callback_data="bc:no"),
    ]])
    await update.message.reply_text(
        f"Сообщение выше уйдёт {n} пользователям. Отправляем?",
        reply_markup=kb
    )
    return BROADCAST_CONFIRM


async def broadcast_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "bc:no":
        context.user_data.pop("bc_chat", None)
        context.user_data.pop("bc_msg", None)
        await query.edit_message_text("Рассылка отменена.")
        return ConversationHandler.END

    src_chat = context.user_data.get("bc_chat")
    src_msg = context.user_data.get("bc_msg")
    users = load_users()
    await query.edit_message_text(f"📤 Рассылаю {len(users)} пользователям...")

    sent = failed = 0
    dead = []
    for target in list(users):
        try:
            await context.bot.copy_message(chat_id=target, from_chat_id=src_chat, message_id=src_msg)
            sent += 1
        except RetryAfter as e:
            await asyncio.sleep(int(e.retry_after) + 1)
            try:
                await context.bot.copy_message(chat_id=target, from_chat_id=src_chat, message_id=src_msg)
                sent += 1
            except Exception:
                failed += 1
        except Forbidden:
            # пользователь заблокировал бота — убираем из базы
            failed += 1
            dead.append(target)
        except Exception as e:
            failed += 1
            logger.error(f"Рассылка для {target}: {e}")
        await asyncio.sleep(0.05)  # бережём лимиты Telegram (~30/сек)

    if dead:
        remove_users(dead)

    context.user_data.pop("bc_chat", None)
    context.user_data.pop("bc_msg", None)
    report = f"✅ Готово.\nДоставлено: {sent}\nНе доставлено: {failed}"
    if dead:
        report += f"\nУбрал заблокировавших: {len(dead)}"
    await query.message.reply_text(report)
    return ConversationHandler.END


async def weekly_stats_job(context: ContextTypes.DEFAULT_TYPE):
    """Раз в день срабатывает в REPORT_HOUR_MSK:00 МСК; шлём отчёт только по
    пятницам — админу и всем подписчикам. Заблокировавших бот убираем из
    подписки (админа не трогаем)."""
    now = datetime.now(MSK)
    if now.weekday() != 4:  # 4 = пятница (Пн=0 … Вс=6)
        return
    recipients = load_subscribers()
    if ADMIN_ID != 0:
        recipients.add(ADMIN_ID)
    if not recipients:
        logger.info("Еженедельный отчёт: ни подписчиков, ни ADMIN_ID — пропускаю.")
        return

    all_events = load_stats()
    report = build_weekly_report(all_events, until=now)
    card = render_stats_card(all_events, until=now)  # PNG-байты или None
    dead = []
    for target in list(recipients):
        try:
            await context.bot.send_message(chat_id=target, text=report, parse_mode="Markdown")
        except Forbidden:
            dead.append(target)
            continue  # заблокировал бот — карточку даже не пытаемся слать
        except RetryAfter as e:
            await asyncio.sleep(int(e.retry_after) + 1)
            try:
                await context.bot.send_message(chat_id=target, text=report, parse_mode="Markdown")
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Еженедельный отчёт для {target}: {e}")
        # Карточка отправляется отдельным сообщением после текста (свежий
        # BytesIO на каждого получателя — Telegram «вычитывает» поток).
        if card:
            try:
                await context.bot.send_photo(chat_id=target, photo=io.BytesIO(card))
            except Forbidden:
                if target not in dead:
                    dead.append(target)
            except RetryAfter as e:
                await asyncio.sleep(int(e.retry_after) + 1)
            except Exception as e:
                logger.error(f"Карточка статистики для {target}: {e}")
        await asyncio.sleep(0.05)  # бережём лимиты Telegram

    if dead:
        subs = load_subscribers()
        subs -= set(dead)
        save_subscribers(subs)
        logger.info(f"Еженедельный отчёт: убрал заблокировавших из подписки: {len(dead)}.")


def _stats_allowed(uid: int, chat_id: int) -> bool:
    """Кому доступна статистика: пока ADMIN_ID не задан — всем (для настройки),
    затем — админу и подписчикам скрытой команды."""
    if ADMIN_ID == 0:
        return True
    if uid == ADMIN_ID:
        return True
    return chat_id in load_subscribers()


async def stats_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Скрытая команда подписки. Кто её знает — тот подписывается на пятничный
    отчёт и получает доступ к /stats."""
    chat_id = update.effective_chat.id
    subs = load_subscribers()
    if chat_id in subs:
        await update.message.reply_text(
            f"📊 Ты уже в деле — сводка прилетает по пятницам в "
            f"{REPORT_HOUR_MSK}:00 МСК. И /stats тоже твоя 😎"
        )
        return
    subs.add(chat_id)
    save_subscribers(subs)
    await update.message.reply_text(
        "📊 *Подписка оформлена!*\n\n"
        f"Каждую пятницу в *{REPORT_HOUR_MSK}:00 МСК* тебе будет прилетать "
        "сводка по ÖMANKÖ — сколько постов и фото сделано за неделю.\n\n"
        "Бонусом открыл доступ к /stats — зови в любой момент 🔥\n\n"
        f"Передумаешь — /{UNSUBSCRIBE_CMD}",
        parse_mode="Markdown"
    )


async def stats_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отписка от еженедельного отчёта (и от доступа к /stats)."""
    chat_id = update.effective_chat.id
    subs = load_subscribers()
    if chat_id not in subs:
        return  # тихо — команда скрытая, незнакомцам реагировать незачем
    subs.discard(chat_id)
    save_subscribers(subs)
    await update.message.reply_text(
        f"Отписал от еженедельной сводки. Захочешь обратно — /{SUBSCRIBE_CMD} 👋"
    )


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика по запросу (за последние 7 дней) + состояние хранилища.
    Доступна админу и подписчикам; для остальных команды как будто нет."""
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    if not _stats_allowed(uid, chat_id):
        return
    all_events = load_stats()
    report = build_weekly_report(all_events)
    storage = ("🟢 постоянное (Railway Volume) — переживёт деплой"
               if STORAGE_PERSISTENT else
               "🔴 ВРЕМЕННОЕ — данные обнулятся при следующем деплое. "
               "Подключи Volume в Railway (mount path любой, бот подхватит сам).")
    await update.message.reply_text(
        f"{report}\n\n_Хранилище: {storage}_",
        parse_mode="Markdown"
    )
    # Визуальная карточка отдельным сообщением (если есть что показывать).
    card = render_stats_card(all_events)
    if card:
        try:
            await update.message.reply_photo(photo=io.BytesIO(card))
        except Exception as e:
            logger.error(f"Не смог отправить карточку статистики: {e}")


class PerUserUpdateProcessor(BaseUpdateProcessor):
    """Параллельная обработка апдейтов РАЗНЫХ людей, но строго по очереди для
    одного и того же чата. Так один человек, рендерящий 10 обложек, не
    замораживает бота для остальных, а у каждого диалог идёт в правильном
    порядке (фото альбома — по порядку, двойной клик по слайдеру — без гонок)."""

    def __init__(self, max_concurrent_updates: int = 64):
        super().__init__(max_concurrent_updates)
        self._locks = {}

    async def do_process_update(self, update, coroutine):
        key = None
        if isinstance(update, Update):
            if update.effective_chat:
                key = update.effective_chat.id
            elif update.effective_user:
                key = update.effective_user.id
        if key is None:
            await coroutine
            return
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            await coroutine

    async def initialize(self):
        pass

    async def shutdown(self):
        pass


def main():
    app = (
        Application.builder()
        .token(TOKEN)
        .read_timeout(120).write_timeout(120).connect_timeout(30)
        .concurrent_updates(PerUserUpdateProcessor(64))
        .build()
    )
    again_handler = CallbackQueryHandler(again, pattern="^again:")
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start), again_handler],
        states={
            CHOOSING_TYPE: [CallbackQueryHandler(choose_type, pattern="^type:")],
            CHOOSING_CHANNEL: [
                CallbackQueryHandler(choose_channel, pattern="^channel:"),
                CallbackQueryHandler(back_to_type, pattern="^nav:back$"),
            ],
            # --- коллаб: партнёры ---
            CHOOSING_PARTNER: [
                CallbackQueryHandler(partner_pick, pattern="^pt:"),
                CallbackQueryHandler(back_to_type, pattern="^nav:back$"),
            ],
            WAITING_PARTNER_LOGO: [
                MessageHandler(filters.PHOTO | filters.Document.ALL, receive_partner_logo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, logo_wrong_input),
                CallbackQueryHandler(back_from_logo, pattern="^nav:back$"),
            ],
            WAITING_PARTNER_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_partner_name),
                CallbackQueryHandler(partner_name_from_file, pattern="^pname:file$"),
                CallbackQueryHandler(back_from_name, pattern="^nav:back$"),
            ],
            PARTNER_TUNE: [
                CallbackQueryHandler(partner_tune, pattern="^tune:"),
                CallbackQueryHandler(back_from_tune, pattern="^nav:back$"),
            ],
            PARTNER_MANAGE: [
                CallbackQueryHandler(partner_manage, pattern="^pm:"),
                CallbackQueryHandler(back_manage_to_list, pattern="^nav:back$"),
            ],
            CHOOSING_COLLAB_KIND: [
                CallbackQueryHandler(choose_collab_kind, pattern="^ck:"),
                CallbackQueryHandler(back_collab_to_list, pattern="^nav:back$"),
            ],
            COLLAB_COLOR: [
                CallbackQueryHandler(collab_color_step, pattern="^ccol:"),
                CallbackQueryHandler(back_collab_color, pattern="^nav:back$"),
            ],
            WAITING_PHOTOS: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, receive_photos),
                CommandHandler("done", done),
                CallbackQueryHandler(photos_done, pattern="^photos:done$"),
                CallbackQueryHandler(photos_reset, pattern="^photos:reset$"),
                CallbackQueryHandler(back_to_channel, pattern="^nav:back$"),
                # всё остальное на этом шаге — подсказка вместо тишины
                MessageHandler(filters.UpdateType.MESSAGE & ~filters.COMMAND, photos_wrong_input),
            ],
            WAITING_TITLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_title),
                CallbackQueryHandler(reuse_title, pattern="^reuse:title$"),
                CallbackQueryHandler(back_to_photos, pattern="^nav:back$"),
            ],
            CHOOSING_FORMAT: [
                CallbackQueryHandler(choose_format, pattern="^fmt:"),
                CallbackQueryHandler(back_from_format, pattern="^nav:back$"),
            ],
            CHOOSING_HASHTAG: [
                CallbackQueryHandler(choose_hashtag, pattern="^tag:"),
                CallbackQueryHandler(back_to_format, pattern="^nav:back$"),
            ],
            WAITING_CUSTOM_HASHTAG: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_custom_hashtag),
                CallbackQueryHandler(back_to_hashtags, pattern="^nav:back$"),
            ],
            WAITING_STORE_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_store_text),
                CallbackQueryHandler(reuse_store_text, pattern="^reuse:store$"),
                CallbackQueryHandler(back_to_photos, pattern="^nav:back$"),
            ],
            CHOOSING_STORE_COLOR: [
                CallbackQueryHandler(choose_store_color, pattern="^scol:"),
                CallbackQueryHandler(back_store_color_to_text, pattern="^nav:back$"),
            ],
            STORE_COLOR_SLIDER: [
                CallbackQueryHandler(store_gray_slider, pattern="^sgray:"),
                CallbackQueryHandler(back_store_slider_to_color, pattern="^nav:back$"),
            ],
            COVER_DARK_SLIDER: [
                CallbackQueryHandler(cover_dark_slider, pattern="^cdark:"),
                CallbackQueryHandler(back_cover_slider, pattern="^nav:back$"),
            ],
            # Брошенная сессия: чистим фото из памяти и сообщаем
            ConversationHandler.TIMEOUT: [TypeHandler(Update, on_timeout)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start),
                   again_handler],
        conversation_timeout=CONV_TIMEOUT_SEC,
    )
    bc_conv = ConversationHandler(
        entry_points=[CommandHandler("broadcast", broadcast_start)],
        states={
            BROADCAST_MSG: [MessageHandler(~filters.COMMAND, broadcast_receive)],
            BROADCAST_CONFIRM: [CallbackQueryHandler(broadcast_confirm, pattern="^bc:")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=CONV_TIMEOUT_SEC,
    )
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler(SUBSCRIBE_CMD, stats_subscribe))
    app.add_handler(CommandHandler(UNSUBSCRIBE_CMD, stats_unsubscribe))
    app.add_handler(bc_conv)
    app.add_handler(conv)
    # Фото вне диалога (без /start или не на том шаге) — короткая подсказка
    app.add_handler(MessageHandler(
        filters.UpdateType.MESSAGE & (filters.PHOTO | filters.Document.IMAGE), orphan_media))
    # Последним: кнопки из закрытых/старых диалогов — просто гасим «часики»
    app.add_handler(CallbackQueryHandler(stale_callback))
    app.add_error_handler(on_error)

    logger.info(
        f"Хранилище: {DATA_DIR} "
        f"({'постоянное (Volume)' if STORAGE_PERSISTENT else 'ВРЕМЕННОЕ — нужен Volume!'})"
    )
    logger.info(f"HEIC/HEIF: {'включён' if HEIF_OK else 'нет pillow-heif — HEIC не откроется'}")
    logger.info("Текст: " + ("RAQM — кернинг включён" if features.check("raqm")
                             else "BASIC — без кернинга (нет libfribidi0 в Dockerfile)"))
    logger.info(f"SVG-логотипы: {'включены' if SVG_OK else 'выключены (нет cairosvg/libcairo2)'}")
    n_partners = len(load_partners())
    logger.info(f"Партнёров в пресетах: {n_partners}")
    if app.job_queue:
        app.job_queue.run_daily(
            weekly_stats_job,
            time=dtime(hour=REPORT_HOUR_MSK, minute=0, tzinfo=MSK),
        )
        logger.info(f"Еженедельный отчёт: запланирован на пятницу {REPORT_HOUR_MSK}:00 МСК.")
    else:
        logger.warning(
            "JobQueue недоступна — еженедельный отчёт не запустится. "
            "Нужно: python-telegram-bot[job-queue] в requirements.txt."
        )

    app.run_polling()


if __name__ == "__main__":
    main()
