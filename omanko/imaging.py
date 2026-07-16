"""Примитивы работы с изображением: цвет, яркость, шрифты, логотипы.

Модуль ничего не знает про Telegram и про состояние диалога. Импортирует
только paths и config — поэтому render.py, стоящий на нём, работает в
тестах без переменных окружения.
"""
import logging
import os

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from omanko.config import ALPHA, BASE_O_LOGO_FILE, BASE_WORDMARK_FILE
from omanko.paths import BASE

logger = logging.getLogger(__name__)


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
