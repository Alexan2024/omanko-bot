"""Фото поста, публикация в канал, альбом для просмотра и пакет для Instagram."""
import html
import json
import logging
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile, InputMediaDocument, InputMediaPhoto

from app import brand, config, db, formatter

log = logging.getLogger(__name__)

FORMAT_LABEL = {"std": "большой", "mini": "мини", "notes": "#ahmagnotes"}
SHORT = {"std": "бол", "mini": "мини"}


def cat_label(cat: str | None) -> str:
    cat = config.CATEGORY_ALIASES.get(cat or "", cat or "")
    return {"notes": "заметка"}.get(cat, config.CATEGORIES.get(cat, cat or "—")).lower()


# ---------- фото ----------

def photo_plan(post) -> list[int]:
    """Индексы фото, которые уйдут в канал: без исключённых, обложка первой, с лимитом формата."""
    data = json.loads(post["data"])
    n = len(json.loads(post["images"]))
    excluded = set(data.get("_excluded") or [])
    idx = [i for i in range(n) if i not in excluded] or list(range(min(n, 1)))
    cover = data.get("_cover")
    if cover in idx:
        idx.remove(cover)
        idx.insert(0, cover)
    return idx[: config.MINI_MAX_PHOTOS if post["format"] == "mini" else config.MAX_PHOTOS]


def cover_path(post) -> str | None:
    images = json.loads(post["images"] or "[]")
    plan = photo_plan(post)
    if plan and Path(images[plan[0]]).exists():
        return images[plan[0]]
    return next((p for p in images if Path(p).exists()), None)


def _fids(post) -> dict[str, str]:
    """{индекс фото: file_id}. В прошлой версии хранился список в порядке фото — понимаем оба вида."""
    raw = json.loads(post["file_ids"] or "{}") if post["file_ids"] else {}
    if isinstance(raw, list):
        return {str(i): f for i, f in enumerate(raw) if f}
    return raw


async def remember_fids(pid: int, pairs: dict[int, str]) -> None:
    post = await db.get_post(pid)
    fids = _fids(post)
    fids.update({str(i): f for i, f in pairs.items() if f})
    await db.update_post(pid, file_ids=fids)


def _media(post, i: int):
    fid = _fids(post).get(str(i))
    return fid or FSInputFile(json.loads(post["images"])[i])


def build_album(files: list, captions: list | None = None) -> list[InputMediaPhoto]:
    captions = captions or [None] * len(files)
    return [InputMediaPhoto(media=f, caption=c, parse_mode="HTML") if c else InputMediaPhoto(media=f)
            for f, c in zip(files, captions)]


async def _send_photos(bot: Bot, chat_id, files: list, caption: str | None) -> list:
    """Фото одним сообщением или альбомом. В канал — со знаком AHMAG, в личку — как есть."""
    if not files:
        return []
    if str(chat_id) == str(config.CHANNEL_ID) and brand.enabled():
        return await brand.with_logo(bot, files, lambda out: _send_raw(bot, chat_id, out, caption))
    return await _send_raw(bot, chat_id, files, caption)


async def _send_raw(bot: Bot, chat_id, files: list, caption: str | None) -> list:
    if len(files) == 1:   # альбом в Telegram — от двух фото
        return [await bot.send_photo(chat_id, files[0], caption=caption)]
    return await bot.send_media_group(chat_id, build_album(files, [caption] + [None] * (len(files) - 1)))


async def send_album(bot: Bot, post) -> list[int]:
    """Все фото поста с номерами — чтобы выбрать, какие оставить. → id сообщений (временные)."""
    images = json.loads(post["images"])
    idx = [i for i, p in enumerate(images) if Path(p).exists()][:10]
    if not idx:
        return []
    files = [_media(post, i) for i in idx]
    if len(files) == 1:
        msgs = [await bot.send_photo(config.ADMIN_ID, files[0], caption="1")]
    else:
        msgs = await bot.send_media_group(config.ADMIN_ID, build_album(files, [str(i + 1) for i in idx]))
    await remember_fids(post["id"], {i: m.photo[-1].file_id for i, m in zip(idx, msgs) if m.photo})
    return [m.message_id for m in msgs]


def post_link(post) -> str | None:
    """Ссылка на вышедший пост в канале."""
    if not post["channel_msg_id"]:
        return None
    ch = str(config.CHANNEL_ID)
    if ch.startswith("@"):
        return f"https://t.me/{ch[1:]}/{post['channel_msg_id']}"
    if ch.startswith("-100"):
        return f"https://t.me/c/{ch[4:]}/{post['channel_msg_id']}"
    return None


# ---------- публикация ----------

async def publish_post(bot: Bot, pid: int, how: str = "", slot_key: str | None = None) -> bool:
    """Отправляет пост в канал. Большому посту без текста текст дописывается перед выходом;
    если это не удалось, пост выходит мини. slot_key остаётся на посте — расписание помнит, чем слот был занят."""
    from app import pipeline  # здесь, чтобы не было кругового импорта
    post = await db.get_post(pid)
    if not post or post["status"] == "published":
        return False
    if post["format"] == "std" and not formatter.has_body(json.loads(post["data"])):
        try:
            post = await pipeline.ensure_text(pid)
        except Exception:
            log.exception("Текст перед публикацией %s", pid)
            post = await pipeline.set_format(pid, "mini", write=False)
    plan = photo_plan(post)
    files = [_media(post, i) for i in plan if Path(json.loads(post["images"])[i]).exists() or _fids(post).get(str(i))]
    caption = post["caption"]
    inline = formatter.visible_len(caption) <= config.CAPTION_LIMIT and bool(files)

    msgs = await _send_photos(bot, config.CHANNEL_ID, files, caption if inline else None)
    first_id = msgs[0].message_id if msgs else None
    if not inline:
        for chunk in formatter.split_blocks(caption, config.MESSAGE_LIMIT - 100):
            m = await bot.send_message(config.CHANNEL_ID, chunk, disable_web_page_preview=True)
            first_id = first_id or m.message_id
    await db.update_post(pid, status="published", decided_at=db.now(), slot_key=slot_key or post["slot_key"],
                         channel_msg_id=first_id)
    log.info("Опубликован пост %s %s", pid, how)
    from app import instagram, repeats  # здесь: оба модуля сами пользуются cards
    try:
        await repeats.remember(await db.get_post(pid))   # отпечаток на год — пока фото ещё на диске
    except Exception:
        log.warning("Отпечаток поста %s не записался", pid, exc_info=True)
    try:
        await instagram.mirror(bot, pid)
    except Exception:
        log.exception("Instagram: пост %s не встал в очередь", pid)
    return True


# ---------- Instagram ----------

async def instagram_pack(bot: Bot, pid: int) -> list[int]:
    """Оригиналы фото файлами (без сжатия Telegram) и подпись простым текстом. → id сообщений."""
    post = await db.get_post(pid)
    images = json.loads(post["images"])
    plan = photo_plan(post)
    paths = [Path(images[i]) for i in plan]
    out = []
    if paths and all(p.exists() for p in paths):
        docs = [FSInputFile(p, filename=f"ahmag_{pid}_{n + 1:02d}.jpg") for n, p in enumerate(paths)]
        if len(docs) == 1:
            out.append((await bot.send_document(config.ADMIN_ID, docs[0])).message_id)
        else:
            msgs = await bot.send_media_group(config.ADMIN_ID, [InputMediaDocument(media=d) for d in docs])
            out += [m.message_id for m in msgs]
    else:
        fids = _fids(post)
        files = [fids[str(i)] for i in plan if str(i) in fids]
        if files:
            out += [m.message_id for m in await _send_photos(bot, config.ADMIN_ID, files, None)]
        out.append((await bot.send_message(
            config.ADMIN_ID, "Оригиналы уже удалены с диска — прислал фото в сжатии Telegram.")).message_id)
    text = formatter.plain_text(post["caption"])
    out.append((await bot.send_message(config.ADMIN_ID, f"<code>{html.escape(text[:3900])}</code>")).message_id)
    return out
