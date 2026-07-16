"""Inline-клавиатуры Telegram."""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from omanko.config import (
    CHANNEL_HASHTAGS, CHANNELS, COMMON_HASHTAGS, CUSTOM_HASHTAG_CB,
    DARK_LEVEL_NAMES, DARK_LEVELS, FORMATS, NO_HASHTAG, STORE_GRAY_STEPS,
    _store_gray_value,
)


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