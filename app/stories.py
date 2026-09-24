"""Instagram Stories. Три режима — кнопка «📖 Сторис» на экране «📸 Instagram»:

  фон  (по умолчанию) — для достойных постов (оценка от STORY_MIN_SCORE, по умолчанию 9) бот присылает тебе
        фон 1080×1920: кадр поста на весь экран, сильно размытый и затемнённый, знак сверху, заголовок снизу,
        середина свободна. Ты делаешь в Instagram «Добавить в историю» под постом и кладёшь этот фон —
        получается живой репост, на который можно нажать. Фон к любому вышедшему посту — кнопка на его карточке.
  авто — после большого поста бот сам публикует сторис-открытку (описание ниже). Нажать на неё нельзя:
        репост через API Instagram не делает.
  выкл.

Открытка (режим «авто»):

Картинка 1080×1920 в духе экрана бота: тёплый серый фон, обложка поста с полями, тонкая линия,
заголовок на английском (как в подписи Instagram), логотип в левом нижнем углу.
Ссылку-стикер на пост Instagram через API ставить не даёт, поэтому сторис работает как напоминание.

Включается кнопкой «📖 Stories» на экране «📸 Instagram» (по умолчанию включено) или переменной IG_STORIES=0.
Только большие посты: мини и подборки в сторис не идут."""
import asyncio
import html
import json
import logging
import os
import secrets
import shutil
from pathlib import Path

import httpx
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from app import brand, cards, config, db, formatter

log = logging.getLogger(__name__)

W, H = 1080, 1920
BG, INK, GREY = (236, 233, 226), (28, 28, 28), (110, 106, 100)
MARGIN = 96
FONTS = Path(__file__).resolve().parent.parent / "data" / "fonts"

SCHEMA = """
CREATE TABLE IF NOT EXISTS ig_stories (
    post_id INTEGER PRIMARY KEY,
    status TEXT NOT NULL,       -- done | failed
    media_id TEXT,
    error TEXT,
    updated_at TEXT NOT NULL
);
"""


async def init() -> None:
    async with db.connect() as c:
        await c.executescript(SCHEMA)
        await c.commit()


MODES = {"bg": "фон мне", "auto": "авто", "off": "выкл"}
NEXT = {"bg": "auto", "auto": "off", "off": "bg"}
MIN_SCORE = int(os.getenv("STORY_MIN_SCORE", "9"))


async def mode() -> str:
    if os.getenv("IG_STORIES", "1").strip().lower() in ("0", "off", "false", "no"):
        return "off"
    m = await db.get_setting("ig_story_mode", "bg")
    return m if m in MODES else "bg"


async def enabled() -> bool:
    return await mode() == "auto"


async def after_post(bot, pid: int) -> None:
    """Пост вышел в ленте Instagram: в режиме «фон» — фон достойному посту, в режиме «авто» — сторис."""
    m = await mode()
    post = await db.get_post(pid)
    if not post:
        return
    if m == "auto":
        return await publish(bot, pid)
    if m == "bg" and (post["score"] or 0) >= MIN_SCORE and post["format"] != "notes":
        try:
            await send_background(bot, pid)
        except Exception:
            log.warning("Фон для сторис к посту %s не собрался", pid, exc_info=True)


def _font(size: int, medium: bool = False):
    try:
        return ImageFont.truetype(str(FONTS / ("IBMPlexSans-Medium.ttf" if medium else "IBMPlexSans-Regular.ttf")), size)
    except Exception:
        return ImageFont.load_default(size=size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int, max_lines: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        test = f"{cur} {word}".strip()
        if draw.textlength(test, font=font) <= width or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(".,;:") + "…"
    return lines


def render(cover: Path, title: str, sub: str, dest: Path) -> Path:
    """Сторис: обложка с полями, линия, заголовок, логотип."""
    canvas = Image.new("RGB", (W, H), BG)
    with Image.open(cover) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        box_w, box_h = W - 2 * MARGIN, 1040
        k = min(box_w / im.width, box_h / im.height)
        im = im.resize((round(im.width * k), round(im.height * k)), Image.LANCZOS)
    top = 200 + (box_h - im.height) // 2
    canvas.paste(im, ((W - im.width) // 2, top))
    d = ImageDraw.Draw(canvas)
    y = top + im.height + 72
    rule = y
    d.line([(MARGIN, y), (W - MARGIN, y)], fill=INK, width=2)
    y += 40
    f1, f2 = _font(52, medium=True), _font(34)
    for line in _wrap(d, title, f1, W - 2 * MARGIN, 3):
        d.text((MARGIN, y), line, font=f1, fill=INK)
        y += 66
    y += 10
    for line in _wrap(d, sub, f2, W - 2 * MARGIN, 2):
        d.text((MARGIN, y), line, font=f2, fill=GREY)
        y += 46
    # знак — справа над линией, как подпись; внизу сторис его закрыло бы поле ответа
    out = brand.stamp(canvas, (W - MARGIN - 77, 0, 77, rule + 20)) if brand.enabled() else canvas
    out.save(dest, "JPEG", quality=92)
    return dest


def render_background(cover: Path, title: str, sub: str, dest: Path) -> Path:
    """Фон под репост: кадр на весь экран, сильно размыт и затемнён; знак сверху, заголовок снизу.
    Середина (примерно от 440 до 1440 px) свободна — туда встанет карточка поста."""
    with Image.open(cover) as im:
        im = ImageOps.fit(ImageOps.exif_transpose(im).convert("RGB"), (W, H), Image.LANCZOS)
    im = im.filter(ImageFilter.GaussianBlur(48))
    im = ImageEnhance.Brightness(im).enhance(0.45)
    d = ImageDraw.Draw(im)
    logo = brand._logo("white", 78, 70)
    im.paste(logo.convert("RGB"), (MARGIN, 250), logo.getchannel("A").point(lambda a: round(a * 0.9)))
    f1, f2 = _font(50, medium=True), _font(32)
    t = _wrap(d, title, f1, W - 2 * MARGIN, 2)
    s = _wrap(d, sub, f2, W - 2 * MARGIN, 2)
    y = 1500
    d.line([(MARGIN, y), (MARGIN + 120, y)], fill=(255, 255, 255), width=2)
    y += 34
    for line in t:
        d.text((MARGIN, y), line, font=f1, fill=(255, 255, 255))
        y += 62
    y += 8
    for line in s:
        d.text((MARGIN, y), line, font=f2, fill=(205, 200, 192))
        y += 44
    im.save(dest, "JPEG", quality=94)
    return dest


async def _cover(bot, post, dest: Path) -> Path:
    images = json.loads(post["images"] or "[]")
    plan = cards.photo_plan(post)
    i = plan[0] if plan else 0
    if i < len(images) and Path(images[i]).exists():
        shutil.copyfile(images[i], dest)
    elif cards._fids(post).get(str(i)):
        await bot.download(cards._fids(post)[str(i)], destination=dest)
    else:
        raise RuntimeError("нет обложки ни на диске, ни в Telegram")
    return dest


async def send_background(bot, pid: int) -> None:
    """Фон для сторис — тебе в личку файлом без сжатия, с кнопкой на пост в Instagram."""
    post = await db.get_post(pid)
    async with db.connect() as c:
        ig = await (await c.execute("SELECT caption, permalink FROM ig_posts WHERE post_id=?", (pid,))).fetchone()
    tmp = config.DATA_DIR / "story_tmp" / secrets.token_hex(8)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        cover = await _cover(bot, post, tmp / "cover")
        title, sub = _heading(post, ig["caption"] if ig else None)
        out = await asyncio.to_thread(render_background, cover, title, sub, tmp / "bg.jpg")
        kb = None
        if ig and ig["permalink"]:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📸 Пост в Instagram",
                                                                             url=ig["permalink"])]])
        await bot.send_document(
            config.ADMIN_ID, BufferedInputFile(out.read_bytes(), filename=f"story_{pid}.jpg"),
            caption=f"📖 Фон для сторис: {html.escape(title[:80])}\nОткрой пост в Instagram → «Добавить в историю» "
                    "→ положи этот фон.", reply_markup=kb)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _heading(post, ig_caption: str | None) -> tuple[str, str]:
    """Заголовок сторис — первая строка английской подписи Instagram («Name // Author // Place»)."""
    head = (ig_caption or "").split("\n", 1)[0].strip()
    parts = [p.strip() for p in head.split("//") if p.strip()] or formatter.headline_parts(json.loads(post["data"]))
    return (parts[0] if parts else "AHMAG"), " · ".join(parts[1:])


async def publish(bot, pid: int) -> None:
    """Сторис к посту, который уже вышел в ленте Instagram. Ошибка пишется в базу, пост это не задевает."""
    from app import instagram
    post = await db.get_post(pid)
    if not post or post["format"] != "std" or not await enabled():
        return
    async with db.connect() as c:
        row = await (await c.execute("SELECT status FROM ig_stories WHERE post_id=?", (pid,))).fetchone()
        ig = await (await c.execute("SELECT caption FROM ig_posts WHERE post_id=?", (pid,))).fetchone()
    if row and row["status"] == "done":
        return
    token = secrets.token_urlsafe(18)
    folder = instagram.PUBLIC_DIR / token
    folder.mkdir(parents=True, exist_ok=True)
    try:
        src = await _cover(bot, post, folder / "cover")
        title, sub = _heading(post, ig["caption"] if ig else None)
        await asyncio.to_thread(render, src, title, sub, folder / "story.jpg")
        url = f"{instagram.public_url()}/ig/{token}/story.jpg"
        async with httpx.AsyncClient() as client:
            cid = (await instagram._post(client, f"{instagram.ENV_USER}/media", media_type="STORIES", image_url=url))["id"]
            await instagram._wait(client, cid)
            mid = (await instagram._post(client, f"{instagram.ENV_USER}/media_publish", creation_id=cid))["id"]
        await _save(pid, "done", mid, None)
        log.info("Instagram: сторис к посту %s опубликована", pid)
    except Exception as exc:
        log.warning("Instagram: сторис к посту %s не вышла: %s", pid, exc)
        await _save(pid, "failed", None, str(exc)[:300])
    finally:
        shutil.rmtree(folder, ignore_errors=True)


async def _save(pid: int, status: str, mid: str | None, error: str | None) -> None:
    async with db.connect() as c:
        await c.execute("INSERT INTO ig_stories(post_id, status, media_id, error, updated_at) VALUES (?,?,?,?,?) "
                        "ON CONFLICT(post_id) DO UPDATE SET status=excluded.status, media_id=excluded.media_id, "
                        "error=excluded.error, updated_at=excluded.updated_at", (pid, status, mid, error, db.now()))
        await c.commit()


async def last_line() -> str:
    """Строка для экрана Instagram: последняя сторис."""
    async with db.connect() as c:
        r = await (await c.execute("SELECT * FROM ig_stories ORDER BY updated_at DESC LIMIT 1")).fetchone()
    if not r:
        return ""
    return ("Последняя сторис: вышла" if r["status"] == "done"
            else f"Последняя сторис не вышла: {(r['error'] or '')[:90]}")
