import os
import io
import json
import asyncio
import logging
from datetime import datetime, time as dtime, timezone, timedelta
from PIL import Image, ImageDraw, ImageFont
import pillow_avif  # noqa: F401 — регистрирует AVIF-декодер в Pillow
import numpy as np
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, ConversationHandler
)
from telegram.error import Forbidden, RetryAfter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from omanko.paths import (
    BASE, DATA_DIR, STORAGE_PERSISTENT, USERS_FILE, STATS_FILE, SUBS_FILE,
)
from omanko.settings import TOKEN, ADMIN_ID, SUBSCRIBE_CMD, UNSUBSCRIBE_CMD
from omanko.states import (
    CHOOSING_TYPE, WAITING_PHOTOS, CHOOSING_FORMAT, CHOOSING_HASHTAG,
    WAITING_TITLE, CHOOSING_CHANNEL, WAITING_PARTNER_LOGO,
    WAITING_CUSTOM_HASHTAG, WAITING_STORE_TEXT, CHOOSING_STORE_COLOR,
    STORE_COLOR_SLIDER, COVER_DARK_SLIDER, BROADCAST_MSG, BROADCAST_CONFIRM,
)
from omanko.config import (
    MSK, REPORT_HOUR_MSK, _STATS_CAP, _RU_MONTHS,
    FORMATS, NO_HASHTAG, CUSTOM_HASHTAG_CB, COMMON_HASHTAGS, CHANNEL_HASHTAGS,
    LOGO_W, LOGO_H, LOGO_LEFT, LOGO_BOTTOM, HASHTAG_RIGHT, HASHTAG_BOTTOM,
    HASHTAG_SIZE, BRIGHTNESS_OFFSET, ALPHA,
    COLLAB_WORDMARK_W, COLLAB_WORDMARK_H, COLLAB_GAP, COLLAB_X_GLYPH,
    COLLAB_X_SIZE, COLLAB_PARTNER_H, COLLAB_BOTTOM,
    COLLAB_WORDMARK_CENTER_DROP, COLLAB_ADAPTIVE,
    COVER_TITLE_SIZE_FEED, COVER_TITLE_LS_FEED, COVER_TITLE_SIZE_STORY,
    COVER_TITLE_LS_STORY, COVER_TITLE_BOTTOM_IG, COVER_TITLE_BOTTOM_TG,
    COVER_LINE_SPACING, WORDMARK_W_FEED, WORDMARK_BOTTOM_FEED,
    WORDMARK_W_STORY, WORDMARK_TOP_STORY, BUBBLE_TEXT_SIZE, FEED_BUBBLE_PAD_X,
    IG_BUBBLE_BOTTOM, IG_BUBBLE_W, IG_BUBBLE_H, IG_BUBBLE_RADIUS,
    TG_BUBBLE_BOTTOM, TG_BUBBLE_W, TG_BUBBLE_H, TG_BUBBLE_RADIUS,
    FEED_BUBBLE_ALPHA, FEED_BUBBLE_FILL, STORY_BUBBLE_ALPHA,
    GRAD_ALPHA_DARK, GRAD_ALPHA_LIGHT, GRAD_ALPHA_CEIL, GRAD_RISE_STORY,
    DARK_LEVELS, DARK_DEFAULT_IDX, DARK_LEVEL_NAMES, STORY_SIZE,
    COVER_FORMATS, COVER_DEFAULT,
    STORE_SIZE, STORE_LOGO_W, STORE_LOGO_H, STORE_LOGO_LEFT,
    STORE_LOGO_BOTTOM, STORE_TEXT_GAP, STORE_TEXT_SIZE, STORE_TEXT_BOTTOM,
    STORE_LINE_HEIGHT, STORE_COLOR_LIGHT, STORE_COLOR_DARK,
    STORE_GRAY_STEPS, STORE_GRAY_DEFAULT_IDX,
    CHANNELS, BASE_WORDMARK_FILE, BASE_O_LOGO_FILE,
    _store_gray_value,
)
from omanko.storage import (
    load_users, add_user, remove_users,
    load_subscribers, save_subscribers, load_stats, record_post,
)
from omanko.imaging import (
    get_average_color, brightness_of, adjust_brightness,
    fit_image_to_canvas, load_semibold, load_black, load_bold, get_wordmark,
    _tint_white_logo, paste_type1_channel_logo, paste_story_channel_logo,
    draw_logo,
)
from omanko.render import (
    process_image, process_collab, render_cover_feed, render_cover_story,
    process_store,
)


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
    store_photos = 0

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
        elif e.get("mode") == "store":
            store_photos += ph
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


def hashtag_keyboard(channel: str = "base"):
    tags = CHANNEL_HASHTAGS.get(channel, COMMON_HASHTAGS)
    items = [NO_HASHTAG] + tags
    rows, row = [], []
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
    if mode == "collab":
        context.user_data["channel"] = "base"  # коллаб всегда на базовом вордмарке
        await query.edit_message_text(
            "Режим: *Коллаборация* 🤝\n\n"
            "Сначала пришли *логотип партнёра*:\n"
            "PNG, белый, без фона. Отправляй как *файл* (скрепка → Файл), "
            "чтобы не потерять прозрачность.",
            parse_mode="Markdown",
            reply_markup=back_keyboard(),
        )
        return WAITING_PARTNER_LOGO
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


def photos_prompt_text(context) -> str:
    """Текст шага «пришли фото» — общий для прямого хода и для возврата «Назад»."""
    mode = context.user_data.get("mode")
    n = len(context.user_data.get("photos", []))
    have = f"📂 Уже загружено: *{n}*. " if n else ""
    if mode == "collab":
        return (
            "🤝 *Коллаборация* — логотип партнёра принят.\n\n"
            "📎 Отправляй фото как *файл* (скрепка → Файл), чтобы качество не сжалось.\n\n"
            f"{have}Пришли фото, затем /done"
        )
    if mode == "store":
        return (
            "🛍 *ÖMANKÖ STORE*\n\n"
            "📎 Отправляй фото как *файл* (скрепка → Файл), чтобы качество не сжалось.\n\n"
            f"{have}Пришли фото, затем /done"
        )
    channel = context.user_data.get("channel", "base")
    note = ""
    if mode != "cover":
        note = "_(в Тип 1 логотип Ö общий для всех каналов)_\n\n"
    return (
        f"Канал: *{CHANNELS[channel]['title']}*\n\n"
        f"{note}"
        "📎 Отправляй фото как *файл* (скрепка → Файл), чтобы качество не сжалось.\n\n"
        f"{have}Пришли фото, затем /done"
    )


async def receive_partner_logo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Коллаборация: приём белого PNG-логотипа партнёра (как файл или фото)."""
    data = None
    doc = update.message.document
    if doc and (doc.mime_type or "").startswith("image/"):
        file = await doc.get_file()
        data = bytes(await file.download_as_bytearray())
    elif update.message.photo:
        file = await update.message.photo[-1].get_file()
        data = bytes(await file.download_as_bytearray())

    if not data:
        await update.message.reply_text(
            "Это не похоже на картинку 🙃 Пришли *PNG* логотипа партнёра "
            "(лучше как файл, чтобы сохранить прозрачность).",
            parse_mode="Markdown", reply_markup=back_keyboard())
        return WAITING_PARTNER_LOGO
    try:
        Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:
        await update.message.reply_text(
            "Не смог открыть это как изображение. Пришли PNG ещё раз.",
            reply_markup=back_keyboard())
        return WAITING_PARTNER_LOGO

    context.user_data["partner_logo"] = data
    await update.message.reply_text(
        photos_prompt_text(context),
        parse_mode="Markdown", reply_markup=back_keyboard())
    return WAITING_PHOTOS


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

STORE_TEXT_PROMPT = ("✍️ Пришли *текст подписи* для STORE — 2 строки.\n"
                     "Перенос между строками ставь сам (Enter).")


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
    if context.user_data.get("mode") == "store":
        await update.message.reply_text(
            STORE_TEXT_PROMPT, parse_mode="Markdown", reply_markup=back_keyboard()
        )
        return WAITING_STORE_TEXT
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
    if context.user_data.get("mode") == "collab":
        return await generate_collab(update, context)
    channel = context.user_data.get("channel", "base")
    await query.edit_message_text("Выбери хештег:", reply_markup=hashtag_keyboard(channel))
    return CHOOSING_HASHTAG


async def generate_collab(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Коллаборация: рендер всех фото с нижней строкой «ÖMANKÖ × партнёр».
    Хештегов нет — генерим сразу после выбора формата."""
    query = update.callback_query
    fmt = context.user_data.get("format", "4:5")
    photos = context.user_data.get("photos", [])
    partner = context.user_data.get("partner_logo")

    await query.edit_message_text(f"⚙️ Обрабатываю {len(photos)} фото...")

    if not partner:
        await query.message.reply_text(
            "Потерялся логотип партнёра 😅 Начни заново через /start.")
        context.user_data.clear()
        return ConversationHandler.END

    ok = 0
    for i, photo_bytes in enumerate(photos):
        try:
            img = Image.open(io.BytesIO(photo_bytes)).convert("RGB")
            result = process_collab(img, fmt, partner)
            buf = io.BytesIO()
            result.save(buf, format="JPEG", quality=92)
            buf.seek(0)
            await query.message.reply_document(document=buf, filename=f"collab_{i+1}.jpg")
            ok += 1
        except Exception as e:
            logger.error(f"Ошибка коллаб-фото {i+1}: {e}")
            await query.message.reply_text(f"❌ Ошибка с фото {i+1}: {e}")

    record_post(context.user_data.get("channel", "base"), "collab", ok)
    context.user_data.clear()
    await query.message.reply_text("✅ Готово! /start чтобы начать заново.")
    return ConversationHandler.END


async def receive_store_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """STORE: приём подписи (до 2 строк) → переход к выбору цвета графики."""
    text = (update.message.text or "").strip("\n")
    if not text.strip():
        await update.message.reply_text(
            "Текст пустой — пришли подпись ещё раз (2 строки).",
            reply_markup=back_keyboard())
        return WAITING_STORE_TEXT
    context.user_data["store_text"] = text
    await update.message.reply_text(
        "🎨 *Цвет графики* (логотип + текст):",
        parse_mode="Markdown", reply_markup=store_color_keyboard())
    return CHOOSING_STORE_COLOR


def _render_store_preview(context: ContextTypes.DEFAULT_TYPE, idx: int) -> bytes:
    """Превью первого фото на текущем оттенке слайдера (уменьшенное, для скорости)."""
    photos = context.user_data.get("photos", [])
    text = context.user_data.get("store_text", "")
    img = Image.open(io.BytesIO(photos[0])).convert("RGB")
    res = process_store(img, text, color=_store_gray_value(idx))
    res.thumbnail((1200, 1500), Image.LANCZOS)
    buf = io.BytesIO()
    res.save(buf, format="JPEG", quality=85)
    buf.seek(0)
    return buf.getvalue()


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
    preview = _render_store_preview(context, idx)
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
    preview = _render_store_preview(context, new_idx)
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
    """STORE: рендер всех фото в формат витрины 2000×2500 и отправка файлами."""
    photos = context.user_data.get("photos", [])
    text = context.user_data.get("store_text", "")
    color = context.user_data.get("store_color")  # None=адаптивный или (r,g,b)
    ok = 0
    for i, photo_bytes in enumerate(photos):
        try:
            img = Image.open(io.BytesIO(photo_bytes)).convert("RGB")
            result = process_store(img, text, color=color)
            buf = io.BytesIO()
            result.save(buf, format="JPEG", quality=92)
            buf.seek(0)
            await message.reply_document(document=buf, filename=f"store_{i+1}.jpg")
            ok += 1
        except Exception as e:
            logger.error(f"Ошибка стор-фото {i+1}: {e}")
            await message.reply_text(f"❌ Ошибка с фото {i+1}: {e}")

    record_post(context.user_data.get("channel", "base"), "store", ok)
    context.user_data.clear()
    await message.reply_text("✅ Готово! /start чтобы начать заново.")
    return ConversationHandler.END


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


def _render_cover_preview(context: ContextTypes.DEFAULT_TYPE, idx: int) -> bytes:
    """Превью обложки (лента, первое фото) на текущем уровне затемнения —
    уменьшенное, для скорости. По нему пользователь подбирает плотность."""
    sess = context.user_data["cover_session"]
    img = Image.open(io.BytesIO(sess["photos"][0])).convert("RGB")
    res = render_cover_feed(img, sess["fmt"], sess["title"], sess["hashtag"],
                            dark_level=DARK_LEVELS[idx])
    res.thumbnail((1200, 1500), Image.LANCZOS)
    buf = io.BytesIO()
    res.save(buf, format="JPEG", quality=85)
    buf.seek(0)
    return buf.getvalue()


def _cover_slider_caption(idx: int) -> str:
    return ("🎚 Затемнение градиента — подбери под кадр.\n"
            "Превью на первом фото 👇\n\n"
            f"*Уровень:* {dark_meter(idx)}")


async def _render_with_hashtag(message, context: ContextTypes.DEFAULT_TYPE, hashtag: str):
    """Общий рендер для обоих путей выбора хештега (кнопка из списка / свой текст).
    Сообщение со статусом «Обрабатываю…» каждый путь шлёт сам — здесь только рендер."""
    fmt = context.user_data.get("format", "4:5")
    photos = context.user_data.get("photos", [])
    mode = context.user_data.get("mode", "type1")
    channel = context.user_data.get("channel", "base")

    if mode == "cover":
        title = context.user_data.get("title", "")
        idx = DARK_DEFAULT_IDX
        # Не генерируем сразу: сначала слайдер затемнения с живым превью.
        # Параметры держим в сессии — по ним рисуем превью и финальный рендер.
        context.user_data["cover_session"] = {
            "photos": photos, "title": title, "hashtag": hashtag,
            "fmt": fmt, "channel": channel, "dark_idx": idx,
        }
        preview = _render_cover_preview(context, idx)
        await message.reply_photo(
            photo=io.BytesIO(preview), caption=_cover_slider_caption(idx),
            parse_mode="Markdown", reply_markup=cover_dark_keyboard(idx))
        return COVER_DARK_SLIDER

    # ---- Тип 1 (брендинг) ----
    ok = 0
    for i, photo_bytes in enumerate(photos):
        try:
            img = Image.open(io.BytesIO(photo_bytes)).convert("RGB")
            result = process_image(img, fmt, hashtag, channel=channel)
            buf = io.BytesIO()
            result.save(buf, format="JPEG", quality=92)
            buf.seek(0)
            await message.reply_document(document=buf, filename=f"1_{i+1}.jpg")
            ok += 1
        except Exception as e:
            logger.error(f"Ошибка фото {i+1}: {e}")
            await message.reply_text(f"❌ Ошибка с фото {i+1}: {e}")

    record_post(channel, mode, ok)
    context.user_data.clear()
    await message.reply_text("✅ Готово! /start чтобы начать заново.")
    return ConversationHandler.END


async def choose_hashtag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tag = query.data.split(":", 1)[1]

    # Свой хештег — уводим в ввод текста, рендер будет после ввода
    if tag == CUSTOM_HASHTAG_CB:
        await query.edit_message_text(
            "✍️ Кидай свой хештег одним словом 🔥\n"
            "Можно с # или без — решётку добавлю сам. Например: лето"
        )
        return WAITING_CUSTOM_HASHTAG

    photos = context.user_data.get("photos", [])
    await query.edit_message_text(f"⚙️ Обрабатываю {len(photos)} фото...")
    return await _render_with_hashtag(query.message, context, tag)


async def receive_custom_hashtag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = (update.message.text or "").strip()
    token = raw.split()[0] if raw.split() else ""
    token = token.lstrip("#").strip()
    if not token:
        await update.message.reply_text("Пустой хештег — пришли ещё раз, например: лето")
        return WAITING_CUSTOM_HASHTAG
    hashtag = "#" + token

    photos = context.user_data.get("photos", [])
    await update.message.reply_text(f"⚙️ Обрабатываю {len(photos)} фото с {hashtag}...")
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
        await query.message.reply_text("⚙️ Рендерю обложки и сторис...")
        ok = await _send_covers(query.message, sess["photos"], sess["title"],
                                sess["hashtag"], sess["fmt"], sess["channel"], idx)
        record_post(sess["channel"], "cover", ok)
        context.user_data.clear()
        await query.message.reply_text("✅ Готово! /start чтобы начать заново.")
        return ConversationHandler.END

    idx = sess["dark_idx"]
    new_idx = max(0, min(len(DARK_LEVELS) - 1, idx + (1 if action == "up" else -1)))
    if new_idx == idx:
        await query.answer("Дальше некуда 🙂")
        return COVER_DARK_SLIDER
    sess["dark_idx"] = new_idx
    await query.answer()
    preview = _render_cover_preview(context, new_idx)
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
    channel = context.user_data.get("channel", "base")
    try:
        await query.message.delete()
    except Exception:
        pass
    await query.message.reply_text(
        "Выбери хештег:", reply_markup=hashtag_keyboard(channel))
    return CHOOSING_HASHTAG


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
    if mode == "collab":
        await query.edit_message_text(
            "Режим: *Коллаборация* 🤝\n\n"
            "Пришли *логотип партнёра*: PNG, белый, без фона "
            "(лучше как файл).",
            parse_mode="Markdown", reply_markup=back_keyboard())
        return WAITING_PARTNER_LOGO
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
            WAITING_PARTNER_LOGO: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, receive_partner_logo),
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
            WAITING_CUSTOM_HASHTAG: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_custom_hashtag),
                CallbackQueryHandler(back_to_format, pattern="^nav:back$"),
            ],
            WAITING_STORE_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_store_text),
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
