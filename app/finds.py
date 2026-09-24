"""Находки и Are.na в боте: кнопка «🔍 Находки» на пульте (fa:info), команда /arena — список каналов Are.na.
Сами нишевые источники — app/niche.py; слот находки — slots.find_slot, выбор поста — pipeline.pick_next."""
import html
import logging

import httpx
from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app import config, db, niche, pipeline, screen, slots

log = logging.getLogger(__name__)
router = Router()
router.message.filter(F.from_user.id == config.ADMIN_ID)
router.callback_query.filter(F.from_user.id == config.ADMIN_ID)


async def home_label() -> str:
    """Надпись кнопки на пульте: сколько находок в запасе и какой слот за ними."""
    n = len(await pipeline.ready_finds())
    fs = slots.find_slot()
    every = f" · раз в {config.FIND_EVERY_DAYS} дн." if config.FIND_EVERY_DAYS > 1 else ""
    return f"🔍 Находки · {n}" + (f" · слот {fs[0]:02d}:{fs[1]:02d}{every}" if fs else "")


async def _arena_text(note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    chans = await niche.arena_channels()
    lines = ["<b>Are.na — каналы для находок</b>"]
    if note:
        lines.append(f"<b>{html.escape(note)}</b>")
    lines.append("Бот каждый сбор заглядывает в три случайных канала, на случайную страницу. "
                 "Добавить: <code>/arena add ссылка-или-имя</code>\n")
    lines += [f'{i + 1}. <a href="https://www.are.na/channel/{c}">{html.escape(c)}</a>' for i, c in enumerate(chans)]
    btns = [screen.btn(f"✕ {i + 1}", f"fa:del:{i}") for i in range(len(chans))]
    rows = [btns[i:i + 6] for i in range(0, len(btns), 6)] + [[screen.btn("Закрыть", "fa:close")]]
    return "\n".join(lines)[:4000], InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("arena"))
async def cmd_arena(msg: Message, command: CommandObject, bot: Bot):
    args = (command.args or "").split()
    note = ""
    if len(args) >= 2 and args[0].lower() in ("add", "+"):
        slug = args[1].rstrip("/").rsplit("/", 1)[-1].split("?")[0].lower()
        try:
            async with httpx.AsyncClient(headers={"User-Agent": niche.API_UA}, timeout=30) as c:
                r = await c.get(f"https://api.are.na/v2/channels/{slug}/thumb")
                ok = r.status_code == 200 and (r.json().get("status") or "public") != "private"
        except Exception:
            ok = False
        chans = await niche.arena_channels()
        if not ok:
            note = f"Канал «{slug}» не открылся — проверь ссылку (закрытые каналы не читаются)"
        elif slug in chans:
            note = "Этот канал уже в списке"
        else:
            await db.set_setting("arena_channels", chans + [slug])
            note = f"Добавил: {slug}"
    text, kb = await _arena_text(note)
    await bot.send_message(config.ADMIN_ID, text, reply_markup=kb, disable_web_page_preview=True)
    try:
        await bot.delete_message(config.ADMIN_ID, msg.message_id)
    except Exception:
        pass


@router.callback_query(F.data.startswith("fa:"))
async def on_cb(cb: CallbackQuery, bot: Bot):
    p = cb.data.split(":")
    a = p[1]
    if a == "info":
        stock: dict[str, int] = {}
        for post in await pipeline.ready_finds():
            stock[post["source"]] = stock.get(post["source"], 0) + 1
        fs = slots.find_slot()
        text = ("Находки в запасе: " + (", ".join(f"{niche.LABELS.get(k, k)} {v}" for k, v in
                                                   sorted(stock.items(), key=lambda x: -x[1])) or "пока нет")
                + (f". Слот находки: {fs[0]:02d}:{fs[1]:02d}." if fs else ". Слот находки выключен (FIND_SLOT=off).")
                + " Каналы Are.na — /arena")
        return await cb.answer(text[:200], show_alert=True)
    if a == "close":
        await cb.answer()
        try:
            return await cb.message.delete()
        except Exception:
            return
    if a == "del":
        chans = await niche.arena_channels()
        i = int(p[2])
        if i < len(chans):
            gone = chans.pop(i)
            await db.set_setting("arena_channels", chans)
            note = f"Убрал: {gone}"
        else:
            note = "Список уже изменился"
        await cb.answer()
        text, kb = await _arena_text(note)
        try:
            await cb.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
        except Exception:
            pass
        return
    await cb.answer()
