"""Логотип AHMAG на фото, которые уходят в канал и в Instagram.

Размеры заданы для кадра шириной 1080 px и масштабируются по ширине фото:
знак 39×35, отступ 38 от левого и 38 от нижнего края, непрозрачность 65%.
Цвет знака выбирается по фону под ним: на светлом — чёрный, на тёмном — белый.

Знак ставят cards._send_photos (фото в канал) и instagram._fit (фото для Instagram).
Всё, что бот присылает тебе в личку, — без знака. Выключить — переменная Railway BRAND=0."""
import asyncio
import logging
import os
import shutil
import uuid
from pathlib import Path

from PIL import Image, ImageOps, ImageStat

log = logging.getLogger(__name__)

BASE_W = 1080
LOGO_W, LOGO_H = 39, 35
MARGIN_LEFT, MARGIN_BOTTOM = 38, 38
OPACITY = float(os.getenv("BRAND_OPACITY", "0.65"))
LIGHT_BG = 140          # средняя яркость фона под знаком (0–255), выше — знак чёрный

ASSETS = Path(__file__).resolve().parent.parent / "data" / "brand"
LOGOS = {"black": ASSETS / "logo_black.png", "white": ASSETS / "logo_white.png"}

_cache: dict[tuple[str, int, int], Image.Image] = {}


def enabled() -> bool:
    return os.getenv("BRAND", "1").strip().lower() not in ("0", "false", "no", "off")


def _logo(color: str, w: int, h: int) -> Image.Image:
    """Логотип нужного цвета и размера (RGBA), обрезанный по видимым границам знака."""
    key = (color, w, h)
    if key not in _cache:
        with Image.open(LOGOS[color]) as src:
            src = src.convert("RGBA")
            src = src.crop(src.getchannel("A").getbbox())
            _cache[key] = src.resize((w, h), Image.LANCZOS)
    return _cache[key]


def stamp(im: Image.Image, box: tuple[int, int, int, int] | None = None) -> Image.Image:
    """Фото с логотипом в левом нижнем углу. → новое RGB-изображение.
    box — где внутри кадра лежит сам снимок (x, y, ширина, высота), если вокруг него поля:
    знак встаёт в угол снимка, а не полей. Размер знака всегда считается от ширины всего кадра."""
    im = im.convert("RGB")
    W, H = im.size
    bx, by, bw, bh = box or (0, 0, W, H)
    s = W / BASE_W
    w, h = max(1, round(LOGO_W * s)), max(1, round(LOGO_H * s))
    x, y = bx + round(MARGIN_LEFT * s), by + bh - round(MARGIN_BOTTOM * s) - h
    if y < by or x + w > bx + bw:
        return im
    color = "black" if ImageStat.Stat(im.crop((x, y, x + w, y + h)).convert("L")).mean[0] > LIGHT_BG else "white"
    logo = _logo(color, w, h)
    alpha = logo.getchannel("A").point(lambda a: round(a * OPACITY))
    out = im.copy()
    out.paste(logo.convert("RGB"), (x, y), alpha)
    return out


def stamp_file(src: Path, dst: Path) -> Path:
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        stamp(im).save(dst, "JPEG", quality=95)
    return dst


# ======================= фото в канал =======================

def _tmp_dir() -> Path:
    from app import config
    d = config.DATA_DIR / "brand_tmp" / uuid.uuid4().hex
    d.mkdir(parents=True, exist_ok=True)
    return d


async def with_logo(bot, files: list, send):
    """Фото для канала со знаком: копии во временной папке → send(новый список) → папка удаляется.
    Фото, уже лежащие в Telegram (file_id), скачиваются. Не вышло со знаком — фото уходит как есть."""
    from aiogram.types import FSInputFile
    tmp = _tmp_dir()
    try:
        out = []
        for n, f in enumerate(files):
            try:
                if isinstance(f, FSInputFile):
                    src = Path(f.path)
                else:
                    src = tmp / f"src{n:02d}"
                    await bot.download(f, destination=src)
                dst = await asyncio.to_thread(stamp_file, src, tmp / f"{n:02d}.jpg")
                out.append(FSInputFile(dst))
            except Exception:
                log.warning("Логотип: фото %s ушло без знака", n + 1, exc_info=True)
                out.append(f)
        return await send(out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
