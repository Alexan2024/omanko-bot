"""🗂 Очередь публикаций: по сообщению на день — сетка обложек и список постов в одном сообщении.

Кнопка на пульте «🗂 Очередь». В сетке по одной фотографии от каждого поста с номером, в подписи —
время, название и рубрика; свободные слоты — строкой ⚪️. Под каждым днём — кнопки с номерами: пост
открывается на экране со всеми обычными действиями (слот, текст, фото, отклонить…).
«🔄 Обновить» перерисовывает очередь, «← Назад» удаляет все её сообщения.

Кнопки qv:… — свой роутер, он подключён в bot.py; кнопка на пульте — в screen._home."""
import asyncio
import html
import logging
import math
import shutil
import time
from datetime import timedelta
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardMarkup
from PIL import Image, ImageDraw, ImageFont, ImageOps

from app import cards, config, db, screen, slots

log = logging.getLogger(__name__)
router = Router()
router.callback_query.filter(F.from_user.id == config.ADMIN_ID)

HORIZON_DAYS = 14
CANVAS_W = 1080
RANK = {"approved": 3, "announced": 2, "sent": 1}
MARK = {"approved": "", "announced": "🤖 ", "sent": "📥 "}
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
_lock = asyncio.Lock()


def btn(text: str, data: str):
    return screen.btn(text, data)


# ======================= данные =======================

async def queue_days() -> list[dict]:
    """[{date, rows: [{key, fmt, post|None}]}] — дни, где есть хоть один пост (и сегодня, если там есть свободные)."""
    now = slots._now()
    ups = [(dt, f) for dt, f in slots.upcoming(None, len(config.SLOTS) * (HORIZON_DAYS + 1))
           if dt.date() <= (now + timedelta(days=HORIZON_DAYS)).date()]
    keys = [slots.key_of(dt) for dt, _ in ups]
    rows = await db.posts_in_slots(keys)
    sk = await slots.skipped()
    days: dict = {}
    for dt, f in ups:
        key = slots.key_of(dt)
        here = [r for r in rows if r["slot_key"] == key and r["status"] in RANK]
        post = max(here, key=lambda r: RANK[r["status"]]) if here else None
        if not post and key in sk:
            continue
        days.setdefault(dt.date(), []).append({"key": key, "fmt": f, "dt": dt, "post": post})
    out = [{"date": d, "rows": r} for d, r in sorted(days.items()) if any(x["post"] for x in r)]
    return out


def day_title(d) -> str:
    today = slots._now().date()
    word = {0: "Сегодня", 1: "Завтра"}.get((d - today).days)
    base = f"{WEEKDAYS[d.weekday()]} {d:%d.%m}"
    return f"{word} · {base}" if word else base.capitalize()


def _short(post, limit: int = 38) -> str:
    return slots.headline(post, limit)


def caption(day: dict, limit: int = 38) -> str:
    posts = [r for r in day["rows"] if r["post"]]
    n = len(posts)
    word = ("пост" if n % 10 == 1 and n % 100 != 11 else
            "поста" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "постов")
    lines = [f"<b>{day_title(day['date'])}</b> — {n} {word}", ""]
    num = 0
    for r in day["rows"]:
        t = f"{r['dt']:%H:%M}"
        p = r["post"]
        if not p:
            lines.append(f"⚪️ {t} · свободно · {cards.SHORT.get(r['fmt'], r['fmt'])}")
            continue
        num += 1
        fmt = cards.SHORT.get(p["format"], "замет" if p["format"] == "notes" else p["format"])
        lines.append(f"<b>{num}.</b> {t} · {MARK[p['status']]}{html.escape(_short(p, limit))} · "
                     f"<i>{html.escape(cards.cat_label(p['category']))}</i> · {fmt}")
    used = {r["post"]["status"] for r in day["rows"] if r["post"]}
    legend = [t for st, t in (("announced", "🤖 выйдет сам"), ("sent", "📥 ждёт твоего решения")) if st in used]
    if legend:
        lines += ["", " · ".join(legend)]
    text = "\n".join(lines)
    if len(text) > 1000 and limit > 16:      # подпись к фото — до 1024 знаков: укорачиваем названия
        return caption(day, limit - 8)
    if len(text) > 1000:                      # крайний случай — без свободных слотов
        text = "\n".join(l for l in lines if not l.startswith("⚪️"))
    return text


# ======================= сетка =======================

def _font(size: int):
    for name in ("DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "Arial Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            pass
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def _badge(tile: Image.Image, text: str) -> None:
    d = ImageDraw.Draw(tile)
    s = max(28, tile.width // 7)
    font = _font(int(s * 0.62))
    box = d.textbbox((0, 0), text, font=font)
    w = max(s, box[2] - box[0] + s // 2)
    m = s // 4
    d.rectangle([m, m, m + w, m + s], fill=(17, 17, 17))
    d.text((m + w / 2, m + s / 2), text, font=font, fill=(255, 255, 255), anchor="mm")


def grid(covers: list[Path | None], dest: Path) -> Path:
    n = max(1, len(covers))
    cols = {1: 1, 2: 2, 4: 2}.get(n, 3)
    tile = CANVAS_W // cols
    rows = math.ceil(n / cols)
    gap = 6
    canvas = Image.new("RGB", (CANVAS_W, rows * tile), (255, 255, 255))
    for i, path in enumerate(covers):
        try:
            with Image.open(path) as im:
                t = ImageOps.fit(ImageOps.exif_transpose(im).convert("RGB"), (tile - gap, tile - gap), Image.LANCZOS)
        except Exception:
            t = Image.new("RGB", (tile - gap, tile - gap), (200, 200, 200))
        _badge(t, str(i + 1))
        canvas.paste(t, ((i % cols) * tile + gap // 2, (i // cols) * tile + gap // 2))
    canvas.save(dest, "JPEG", quality=88)
    return dest


async def _cover(bot: Bot, post, dest: Path) -> Path | None:
    p = cards.cover_path(post)
    if p:
        return Path(p)
    plan = cards.photo_plan(post)
    fid = cards._fids(post).get(str(plan[0])) if plan else None
    if fid:
        try:
            await bot.download(fid, destination=dest)
            return dest
        except Exception:
            log.warning("Очередь: обложка поста %s не скачалась", post["id"], exc_info=True)
    return None


# ======================= сообщения =======================

async def _ids() -> list[int]:
    return list(await db.get_setting("queue_view_msgs", []))


async def clear(bot: Bot) -> None:
    for mid in await _ids():
        try:
            await bot.delete_message(config.ADMIN_ID, mid)
        except Exception:
            pass
    await db.set_setting("queue_view_msgs", [])


async def show(bot: Bot) -> None:
    async with _lock:
        await clear(bot)
        days = await queue_days()
        sent: list[int] = []
        tail = [btn("🔄 Обновить", "qv:r"), btn("← Назад", "qv:b")]
        if not days:
            m = await bot.send_message(config.ADMIN_ID, "<b>🗂 Очередь пуста</b> — в слотах ничего не стоит.",
                                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[tail]))
            return await db.set_setting("queue_view_msgs", [m.message_id])
        tmp = config.DATA_DIR / "queue_view" / str(int(time.time() * 1000))
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            for di, day in enumerate(days):
                posts = [r["post"] for r in day["rows"] if r["post"]]
                covers = [await _cover(bot, p, tmp / f"c{di}_{k}.jpg") for k, p in enumerate(posts)]
                img = await asyncio.to_thread(grid, covers, tmp / f"day{di}.jpg")
                nums = [btn(str(k + 1), f"qv:o:{p['id']}") for k, p in enumerate(posts)]
                rows = [nums[i:i + 6] for i in range(0, len(nums), 6)]
                if di == len(days) - 1:
                    rows.append(tail)
                m = await bot.send_photo(config.ADMIN_ID, FSInputFile(img), caption=caption(day),
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
                sent.append(m.message_id)
                await db.set_setting("queue_view_msgs", sent)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


@router.callback_query(F.data.startswith("qv:"))
async def on_cb(cb: CallbackQuery, bot: Bot):
    p = cb.data.split(":")
    a = p[1]
    if a in ("show", "r"):
        await cb.answer("Собираю очередь…" if a == "show" else "Обновляю")
        return asyncio.create_task(_safe_show(bot))
    if a == "b":
        await cb.answer()
        return await clear(bot)
    if a == "o":
        pid = int(p[2])
        post = await db.get_post(pid)
        if not post:
            return await cb.answer("Пост не найден — обнови очередь", show_alert=True)
        await cb.answer()
        st = post["status"]
        mode = "sched" if st == "approved" else ("inbox" if st in ("sent", "announced") else "one")
        return await screen.move_down(bot, "list", mode=mode, pid=pid)
    await cb.answer()


async def _safe_show(bot: Bot) -> None:
    try:
        await show(bot)
    except Exception as exc:
        log.exception("очередь")
        from app import curator
        await bot.send_message(config.ADMIN_ID, f"Очередь не показалась: {curator.explain(exc)}"[:500])
