import os
import io
import json
import asyncio
import logging
from datetime import datetime, time as dtime, timezone, timedelta
from PIL import Image, ImageDraw, ImageFont
import pillow_avif  # noqa: F401 — регистрирует AVIF-декодер в Pillow
import numpy as np
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, ConversationHandler
)
from telegram.error import Forbidden, RetryAfter

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


def record_post(channel: str, mode: str, n_photos: int) -> None:
    if n_photos <= 0:
        return
    events = load_stats()
    events.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "channel": channel,
        "mode": mode if mode in ("type1", "cover") else "type1",
        "photos": int(n_photos),
    })
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
        if e.get("mode") == "cover":
            cover_photos += ph
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
        f"🏷 Тип 1 — {type1_photos}",
        f"🖼 Обложка — {cover_photos}",
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
    "beauty": (255, 110, 196),
    "music":  (255, 176, 60),
    "agency": (60, 214, 180),
    "gastro": (255, 96, 96),
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

HASHTAGS = [
    "— Без хештега —",
    "#architecture", "#cars", "#cinema", "#archives",
    "#art", "#community", "#item", "#music",
    "#paper", "#space", "#style", "#beauty", "#fashion", "#agency"
]

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

# ============ Обложка — параметры ============
# Заголовок (Nunito Sans Black)
COVER_TITLE_SIZE_FEED = 135      # абсолютный размер на всех соотношениях ленты
COVER_TITLE_LS_FEED = -0.03      # letter-spacing -3%
COVER_TITLE_SIZE_STORY = 77      # на канвас 1080x1920
COVER_TITLE_LS_STORY = -0.06     # letter-spacing -6%
COVER_TITLE_BOTTOM_IG = 452      # 424 + 28 (поднят выше)
COVER_TITLE_BOTTOM_TG = 382      # 354 + 28 (поднят выше)
COVER_LINE_SPACING = 1.08        # межстрочный множитель

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
    "2:3": dict(size=(1920, 2560), bubble_h=126, bubble_top=68, title_bottom=385),
    "1:1": dict(size=(2400, 2400), bubble_h=158, bubble_top=85, title_bottom=411),
    "3:2": dict(size=(3600, 2400), bubble_h=126, bubble_top=68, title_bottom=440),
}
# Адаптивный режим обложки использует параметры 4:5
COVER_DEFAULT = dict(bubble_h=126, bubble_top=68, title_bottom=365)

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
    "beauty": {"title": "Ö BEAUTY",
               "type1_logo": "logo_type1_beauty.png", "type1_box": (72, 107, 91, 52),
               "story_logo": "logo_cover_beauty.png",  "story_box": (196, 81)},
    "music":  {"title": "Ö MUSIC",
               "type1_logo": "logo_type1_music.png",  "type1_box": (89, 107, 91, 52),
               "story_logo": "logo_cover_music.png",   "story_box": (196, 81)},
    "agency": {"title": "Ö AGENCY",  # спека пока не задана — базовое поведение
               "type1_logo": None, "type1_box": None,
               "story_logo": None, "story_box": None},
    "gastro": {"title": "Ö GASTRO",
               "type1_logo": "logo_type1_gastro.png", "type1_box": (75, 119, 91, 40),
               "story_logo": "logo_cover_gastro.png",  "story_box": (196, 81)},
}

BASE_WORDMARK_FILE = "wordmark_white.png"  # широкий ÖMANKÖ: лента + база сторис

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


# ============ Тип 1: логотип Ö (без изменений) ============
def draw_logo(canvas: Image.Image, x: int, y: int, w: int, h: int, color: tuple):
    logo = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(logo)
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
    canvas.paste(logo, (x, y), logo)


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

    hashtag_right = int(HASHTAG_RIGHT * scale)
    hashtag_bottom = int(HASHTAG_BOTTOM * scale)
    hashtag_size = int(HASHTAG_SIZE * scale)

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

    # ХЕШТЕГ
    if hashtag and hashtag != "— Без хештега —":
        sample_x = max(0, canvas_w - hashtag_right - int(200 * scale))
        sample_y = max(0, canvas_h - hashtag_bottom - hashtag_size)
        hr, hg, hb = get_average_color(canvas, sample_x, sample_y, int(200 * scale), hashtag_size + 20)
        h_percent = BRIGHTNESS_OFFSET if brightness_of(hr, hg, hb) < 128 else -BRIGHTNESS_OFFSET
        hcr, hcg, hcb = adjust_brightness(hr, hg, hb, h_percent)

        overlay = canvas.convert("RGBA")
        draw = ImageDraw.Draw(overlay)
        font = load_semibold(hashtag_size)
        fill = (hcr, hcg, hcb, int(255 * ALPHA))
        spacing = int(hashtag_size * (-0.007))
        total_w = 0
        char_widths = []
        for ch in hashtag:
            bbox = draw.textbbox((0, 0), ch, font=font)
            cw = bbox[2] - bbox[0]
            char_widths.append(cw)
            total_w += cw + spacing
        total_w -= spacing
        tx = canvas_w - hashtag_right - total_w
        ty = canvas_h - hashtag_bottom - hashtag_size
        cx = tx
        for ch, cw in zip(hashtag, char_widths):
            draw.text((cx, ty), ch, font=font, fill=fill)
            cx += cw + spacing
        canvas = overlay.convert("RGB")

    return canvas


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
    font = load_black(size)
    draw = ImageDraw.Draw(canvas_rgba)
    ls_px = round(size * ls_ratio)
    lines = [ln for ln in text.split("\n")]
    if not lines:
        return
    ascent, descent = font.getmetrics()
    line_adv = int(size * COVER_LINE_SPACING)
    line_visual = ascent + descent
    n = len(lines)
    last_top = (ch - bottom_offset) - line_visual
    first_top = last_top - (n - 1) * line_adv
    cx = cw / 2
    fill = (255, 255, 255, 255)
    for i, line in enumerate(lines):
        # ширина строки с трекингом
        widths = [draw.textlength(c, font=font) for c in line]
        total = sum(widths) + ls_px * (len(line) - 1 if len(line) > 1 else 0)
        x = cx - total / 2
        y = first_top + i * line_adv
        for c, w in zip(line, widths):
            draw.text((x, y), c, font=font, fill=fill)
            x += w + ls_px


def draw_bubble(canvas_rgba: Image.Image, center_x: int, center_y: int,
                bubble_w, bubble_h: int, radius: int, bg_img: Image.Image,
                label=None, color_mode="dark", alpha=0.85):
    """Бабл. label=None -> пустой бабл (сторис).
    color_mode: 'dark' (фикс. тёмный, лента) | 'adaptive_invert' (инверт к фону, сторис).
    bubble_w=None -> авто-ширина под текст (лента)."""
    font = load_semibold(BUBBLE_TEXT_SIZE) if label else None
    d = ImageDraw.Draw(canvas_rgba)
    tw = d.textlength(label, font=font) if label else 0
    if bubble_w is None:
        bubble_w = int(tw + 2 * FEED_BUBBLE_PAD_X)
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
                      dark_level: float = 1.0) -> Image.Image:
    spec = COVER_FORMATS.get(format_key)
    if spec is None:
        # Адаптивный: канвас по картинке (мин. ширина 1920), параметры — дефолтные
        if img.width < 1920:
            sc = 1920 / img.width
            canvas_w, canvas_h = 1920, int(img.height * sc)
        else:
            canvas_w, canvas_h = img.size
        bubble_h = COVER_DEFAULT["bubble_h"]
        bubble_top = COVER_DEFAULT["bubble_top"]
        title_bottom = COVER_DEFAULT["title_bottom"]
    else:
        canvas_w, canvas_h = spec["size"]
        bubble_h = spec["bubble_h"]
        bubble_top = spec["bubble_top"]
        title_bottom = spec["title_bottom"]

    # Размеры элементов абсолютные (не масштабируются от ширины)
    title_size = COVER_TITLE_SIZE_FEED
    wm_w = WORDMARK_W_FEED
    wm_bottom = WORDMARK_BOTTOM_FEED
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
    if hashtag and hashtag != "— Без хештега —":
        cy = bubble_top + bubble_h // 2
        bg_for_bubble = canvas.convert("RGB")
        label = "# " + hashtag.lstrip("#")
        draw_bubble(canvas, canvas_w // 2, cy, None, bubble_h, radius, bg_for_bubble,
                    label=label, color_mode="dark", alpha=FEED_BUBBLE_ALPHA)

    # вордмарк снизу по центру — в ленте ВСЕГДА базовый ÖMANKÖ (по каналу не меняется)
    ratio = get_wordmark().height / get_wordmark().width
    wm_h = round(wm_w * ratio)
    wm_y = canvas_h - wm_bottom - wm_h
    paste_wordmark(canvas, wm_w, canvas_w // 2, wm_y)

    return canvas.convert("RGB")


def render_cover_story(img: Image.Image, variant: str, title: str, hashtag: str,
                       channel: str = "base", dark_level: float = 1.0) -> Image.Image:
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
    if chan["story_logo"] and chan["story_box"]:
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
        [InlineKeyboardButton("🏷 Брендинг (Тип 1)", callback_data="type:type1")],
        [InlineKeyboardButton("🖼 Обложка с текстом", callback_data="type:cover")],
    ])


def channel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(CHANNELS["base"]["title"], callback_data="channel:base")],
        [InlineKeyboardButton(CHANNELS["news"]["title"], callback_data="channel:news"),
         InlineKeyboardButton(CHANNELS["beauty"]["title"], callback_data="channel:beauty")],
        [InlineKeyboardButton(CHANNELS["music"]["title"], callback_data="channel:music"),
         InlineKeyboardButton(CHANNELS["agency"]["title"], callback_data="channel:agency")],
        [InlineKeyboardButton(CHANNELS["gastro"]["title"], callback_data="channel:gastro")],
        [_BACK_BTN],
    ])


def format_keyboard():
    keys = list(FORMATS.keys())
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(k, callback_data=f"fmt:{k}") for k in keys[:4]],
        [InlineKeyboardButton(keys[4], callback_data=f"fmt:{keys[4]}")],
        [_BACK_BTN],
    ])


def hashtag_keyboard():
    rows, row = [], []
    for tag in HASHTAGS:
        row.append(InlineKeyboardButton(tag, callback_data=f"tag:{tag}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([_BACK_BTN])
    return InlineKeyboardMarkup(rows)


def dark_meter(idx: int) -> str:
    """Текстовый индикатор уровня затемнения: ●●●○○ + подпись ступени."""
    n = len(DARK_LEVELS)
    filled = "●" * (idx + 1) + "○" * (n - idx - 1)
    return f"{filled} ({DARK_LEVEL_NAMES[idx]})"


def dark_keyboard(idx: int):
    """Регулятор затемнения под готовыми обложками. Кнопки гаснут на краях."""
    left = InlineKeyboardButton(
        "☀️ Светлее" if idx > 0 else "· · ·",
        callback_data="dark:down" if idx > 0 else "dark:noop")
    right = InlineKeyboardButton(
        "🌑 Темнее" if idx < len(DARK_LEVELS) - 1 else "· · ·",
        callback_data="dark:up" if idx < len(DARK_LEVELS) - 1 else "dark:noop")
    done = InlineKeyboardButton("✅ Завершить", callback_data="dark:done")
    return InlineKeyboardMarkup([[left, right], [done]])


# ============ Хендлеры ============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_user(update.effective_chat.id)
    context.user_data.clear()
    await update.message.reply_text(
        "👋 Привет! Я Post Creator для ÖMANKÖ.\n\nЧто делаем?",
        reply_markup=type_keyboard()
    )
    return CHOOSING_TYPE


async def choose_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mode = query.data.split(":", 1)[1]
    context.user_data["mode"] = mode
    name = "Обложка с текстом" if mode == "cover" else "Брендинг (Тип 1)"
    await query.edit_message_text(
        f"Режим: *{name}*\n\nТеперь выбери канал:",
        parse_mode="Markdown",
        reply_markup=channel_keyboard()
    )
    return CHOOSING_CHANNEL


def photos_prompt_text(context) -> str:
    """Текст шага «пришли фото» — общий для прямого хода и для возврата «Назад»."""
    channel = context.user_data.get("channel", "base")
    note = ""
    if context.user_data.get("mode") != "cover":
        note = "_(в Тип 1 логотип Ö общий для всех каналов)_\n\n"
    n = len(context.user_data.get("photos", []))
    have = f"📂 Уже загружено: *{n}*. " if n else ""
    return (
        f"Канал: *{CHANNELS[channel]['title']}*\n\n"
        f"{note}"
        "📎 Отправляй фото как *файл* (скрепка → Файл), чтобы качество не сжалось.\n\n"
        f"{have}Пришли фото, затем /done"
    )


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


async def receive_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photos = context.user_data.setdefault("photos", [])
    if update.message.photo:
        file = await update.message.photo[-1].get_file()
        photos.append(bytes(await file.download_as_bytearray()))
    elif update.message.document and update.message.document.mime_type.startswith("image/"):
        file = await update.message.document.get_file()
        photos.append(bytes(await file.download_as_bytearray()))
    await update.message.reply_text(f"✅ {len(photos)} фото. Ещё или /done")
    return WAITING_PHOTOS


TITLE_PROMPT = ("✍️ Пришли *текст заголовка*.\n"
                "Переносы строк ставь сам — как нужно на обложке.")


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photos = context.user_data.get("photos", [])
    if not photos:
        await update.message.reply_text("Сначала отправь хотя бы одно фото!")
        return WAITING_PHOTOS
    if context.user_data.get("mode") == "cover":
        await update.message.reply_text(
            TITLE_PROMPT, parse_mode="Markdown", reply_markup=back_keyboard()
        )
        return WAITING_TITLE
    await update.message.reply_text(
        f"📐 Выбери формат ({len(photos)} фото):", reply_markup=format_keyboard()
    )
    return CHOOSING_FORMAT


async def receive_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = (update.message.text or "").strip("\n")
    if not title.strip():
        await update.message.reply_text("Заголовок пустой — пришли текст ещё раз.")
        return WAITING_TITLE
    context.user_data["title"] = title
    await update.message.reply_text(
        "📐 Выбери формат ленты (сторис IG и TG добавлю автоматически):",
        reply_markup=format_keyboard()
    )
    return CHOOSING_FORMAT


async def choose_format(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["format"] = query.data.split(":", 1)[1]
    await query.edit_message_text("Выбери хештег:", reply_markup=hashtag_keyboard())
    return CHOOSING_HASHTAG


async def _send_covers(message, photos, title, hashtag, fmt, channel, dark_idx) -> int:
    """Рендер + отправка обложек (feed/ig/tg) для всех фото на заданном уровне
    затемнения. Используется и при первой генерации, и при ручной регулировке.
    Возвращает число успешно обработанных фото."""
    level = DARK_LEVELS[dark_idx]
    ok = 0
    for i, photo_bytes in enumerate(photos):
        try:
            img = Image.open(io.BytesIO(photo_bytes)).convert("RGB")
            feed = render_cover_feed(img, fmt, title, hashtag, dark_level=level)
            ig = render_cover_story(img, "ig", title, hashtag, channel=channel, dark_level=level)
            tg = render_cover_story(img, "tg", title, hashtag, channel=channel, dark_level=level)
            for result, suffix in ((feed, "feed"), (ig, "ig"), (tg, "tg")):
                buf = io.BytesIO()
                result.save(buf, format="JPEG", quality=92)
                buf.seek(0)
                await message.reply_document(document=buf, filename=f"cover_{i+1}_{suffix}.jpg")
            ok += 1
        except Exception as e:
            logger.error(f"Ошибка обложки {i+1}: {e}")
            await message.reply_text(f"❌ Ошибка с фото {i+1}: {e}")
    return ok


def _dark_control_text(idx: int) -> str:
    return ("✅ Готово! Текст плохо читается или фон перетемнён?\n"
            "Подстрой затемнение — перерисую те же кадры 👇\n\n"
            f"*Затемнение:* {dark_meter(idx)}")


async def choose_hashtag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    hashtag = query.data.split(":", 1)[1]
    fmt = context.user_data.get("format", "4:5")
    photos = context.user_data.get("photos", [])
    mode = context.user_data.get("mode", "type1")
    channel = context.user_data.get("channel", "base")

    await query.edit_message_text(f"⚙️ Обрабатываю {len(photos)} фото...")

    if mode == "cover":
        title = context.user_data.get("title", "")
        dark_idx = DARK_DEFAULT_IDX
        ok = await _send_covers(query.message, photos, title, hashtag, fmt, channel, dark_idx)
        record_post(channel, mode, ok)
        if ok:
            # Держим параметры в user_data, чтобы регулятор затемнения мог
            # перерисовать те же кадры уже после завершения диалога.
            context.user_data.clear()
            context.user_data["cover_session"] = {
                "photos": photos, "title": title, "hashtag": hashtag,
                "fmt": fmt, "channel": channel, "dark_idx": dark_idx,
            }
            await query.message.reply_text(
                _dark_control_text(dark_idx), parse_mode="Markdown",
                reply_markup=dark_keyboard(dark_idx),
            )
        else:
            context.user_data.clear()
            await query.message.reply_text("✅ Готово! /start чтобы начать заново.")
        return ConversationHandler.END

    # ---- Тип 1 (брендинг) ----
    ok = 0
    for i, photo_bytes in enumerate(photos):
        try:
            img = Image.open(io.BytesIO(photo_bytes)).convert("RGB")
            result = process_image(img, fmt, hashtag, channel=channel)
            buf = io.BytesIO()
            result.save(buf, format="JPEG", quality=92)
            buf.seek(0)
            await query.message.reply_document(document=buf, filename=f"1_{i+1}.jpg")
            ok += 1
        except Exception as e:
            logger.error(f"Ошибка фото {i+1}: {e}")
            await query.message.reply_text(f"❌ Ошибка с фото {i+1}: {e}")

    record_post(channel, mode, ok)
    context.user_data.clear()
    await query.message.reply_text("✅ Готово! /start чтобы начать заново.")
    return ConversationHandler.END


async def dark_adjust(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Топ-левел хендлер регулятора затемнения (работает после конца диалога)."""
    query = update.callback_query
    action = query.data.split(":", 1)[1]

    if action == "noop":
        await query.answer()
        return

    if action == "done":
        # Завершаем цикл обложки: гасим кнопки и шлём финальное сообщение.
        await query.answer()
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        context.user_data.pop("cover_session", None)
        await query.message.reply_text("✅ Готово! /start чтобы начать заново.")
        return

    sess = context.user_data.get("cover_session")
    if not sess:
        await query.answer("Сессия устарела — сделай новый пост через /start",
                           show_alert=True)
        return
    idx = sess["dark_idx"]
    step = 1 if action == "up" else -1
    new_idx = max(0, min(len(DARK_LEVELS) - 1, idx + step))
    if new_idx == idx:
        await query.answer("Дальше некуда 🙂")
        return
    sess["dark_idx"] = new_idx
    await query.answer("Перерисовываю…")
    # Со старого регулятора снимаем кнопки, чтобы активный регулятор всегда
    # сидел под самой свежей пачкой кадров, а не висел над ними.
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass  # сообщение устарело — не критично
    await _send_covers(query.message, sess["photos"], sess["title"], sess["hashtag"],
                       sess["fmt"], sess["channel"], new_idx)
    # Новый регулятор под новыми обложками: снова светлее / темнее / завершить.
    await query.message.reply_text(
        _dark_control_text(new_idx), parse_mode="Markdown",
        reply_markup=dark_keyboard(new_idx),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Отменено. /start чтобы начать заново.")
    return ConversationHandler.END


# ============ Навигация «Назад» ============
async def back_to_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Что делаем?", reply_markup=type_keyboard())
    return CHOOSING_TYPE


async def back_to_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mode = context.user_data.get("mode", "type1")
    name = "Обложка с текстом" if mode == "cover" else "Брендинг (Тип 1)"
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
    if context.user_data.get("mode") == "cover":
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
        f"📐 Выбери формат ({len(photos)} фото):", reply_markup=format_keyboard())
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
        context.user_data.clear()
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

    context.user_data.clear()
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


def main():
    app = (
        Application.builder()
        .token(TOKEN)
        .read_timeout(120).write_timeout(120).connect_timeout(30)
        .build()
    )
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSING_TYPE: [CallbackQueryHandler(choose_type, pattern="^type:")],
            CHOOSING_CHANNEL: [
                CallbackQueryHandler(choose_channel, pattern="^channel:"),
                CallbackQueryHandler(back_to_type, pattern="^nav:back$"),
            ],
            WAITING_PHOTOS: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, receive_photos),
                CommandHandler("done", done),
                CallbackQueryHandler(back_to_channel, pattern="^nav:back$"),
            ],
            WAITING_TITLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_title),
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
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start)],
    )
    bc_conv = ConversationHandler(
        entry_points=[CommandHandler("broadcast", broadcast_start)],
        states={
            BROADCAST_MSG: [MessageHandler(~filters.COMMAND, broadcast_receive)],
            BROADCAST_CONFIRM: [CallbackQueryHandler(broadcast_confirm, pattern="^bc:")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler(SUBSCRIBE_CMD, stats_subscribe))
    app.add_handler(CommandHandler(UNSUBSCRIBE_CMD, stats_unsubscribe))
    app.add_handler(bc_conv)
    # Регулятор затемнения обложек: топ-левел, до conv, чтобы ловить тапы
    # уже после завершения диалога. Паттерн не пересекается с conv.
    app.add_handler(CallbackQueryHandler(dark_adjust, pattern="^dark:"))
    app.add_handler(conv)

    logger.info(
        f"Хранилище: {DATA_DIR} "
        f"({'постоянное (Volume)' if STORAGE_PERSISTENT else 'ВРЕМЕННОЕ — нужен Volume!'})"
    )
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
