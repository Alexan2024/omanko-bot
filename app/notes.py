"""#ahmagnotes по запросу: темы → сбор материала → план на утверждение → текст → входящие."""
import html
import json
import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app import config, curator, db, pipeline, screen, slots

log = logging.getLogger(__name__)
router = Router()
router.message.filter(F.from_user.id == config.ADMIN_ID)
router.callback_query.filter(F.from_user.id == config.ADMIN_ID)


class Notes(StatesGroup):
    topic = State()
    plan = State()


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


async def propose(msg: Message) -> None:
    wait = await msg.answer("Думаю над темами…")
    try:
        avoid = await db.note_topics() + [t["title"] for t in await db.get_setting("notes_topics", [])]
        topics = await curator.notes_topics(avoid)
    except Exception as exc:
        log.exception("notes_topics")
        return await wait.edit_text(curator.explain(exc))
    if not topics:
        return await wait.edit_text("Темы не придумались. Попробуйте ещё раз или задайте свою.")
    await db.set_setting("notes_topics", topics)
    text = "<b>#ahmagnotes — темы на выбор</b>\n\n" + "\n\n".join(
        f"<b>{i + 1}. {html.escape(t['title'])}</b>\n{html.escape(t.get('angle', ''))}" for i, t in enumerate(topics))
    rows = [[_btn(f"{i + 1}", f"nt:{i}") for i in range(len(topics))],
            [_btn("🔄 Другие темы", "nt:more"), _btn("✍️ Своя тема", "nt:own")]]
    await wait.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


def _plan_view(nid: int, brief: dict) -> tuple[str, InlineKeyboardMarkup]:
    plan = "\n".join(f"{i + 1}. {html.escape(str(p))}" for i, p in enumerate(brief.get("plan", [])))
    srcs = "\n".join(
        f'• <a href="{html.escape(s.get("url") or "")}">{html.escape((s.get("title") or s.get("url") or "?")[:70])}</a>'
        for s in (brief.get("sources") or [])[:8] if s.get("url")) or "—"
    text = (f"<b>{html.escape(brief.get('title') or brief.get('topic', ''))}</b>\n"
            f"<i>{html.escape(brief.get('thesis', ''))}</i>\n\n<b>План</b>\n{plan}\n\n"
            f"<b>Источники</b> (фактов: {len(brief.get('facts', []))})\n{srcs}")
    if brief.get("_no_search"):
        text += "\n\n⚠️ Веб-поиск не сработал — материал собран по памяти модели, факты нужно проверить."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("✅ Писать", f"np:write:{nid}")],
        [_btn("✏️ Поправить план", f"np:edit:{nid}"), _btn("❌ Отмена", f"np:cancel:{nid}")]])
    return text[:4000], kb


async def research(msg: Message, topic: str, angle: str = "") -> None:
    wait = await msg.answer(f"Собираю материал: <b>{html.escape(topic)}</b>\nЭто займёт пару минут.")
    try:
        brief = await curator.notes_research(topic, angle)
        nid = await db.add_note(topic, brief, status="plan")
        text, kb = _plan_view(nid, brief)
        await wait.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception as exc:
        log.exception("notes_research")
        await wait.edit_text(f"Не получилось собрать материал. {curator.explain(exc)}")


@router.callback_query(F.data.startswith("nt:"))
async def on_topic(cb: CallbackQuery, state: FSMContext):
    arg = cb.data.split(":")[1]
    await cb.answer()
    if arg == "more":
        return await propose(cb.message)
    if arg == "own":
        await state.set_state(Notes.topic)
        return await cb.message.answer("Напишите тему заметки — одной-двумя фразами. /cancel — отмена.")
    topics = await db.get_setting("notes_topics", [])
    if int(arg) >= len(topics):
        return await cb.message.answer("Список тем устарел — откройте #ahmagnotes заново.")
    t = topics[int(arg)]
    await research(cb.message, t["title"], t.get("angle", ""))


@router.message(Notes.topic, F.text, ~F.text.startswith("/"))
async def on_own_topic(msg: Message, state: FSMContext):
    await state.clear()
    await research(msg, msg.text.strip()[:300])


@router.callback_query(F.data.startswith("np:"))
async def on_plan(cb: CallbackQuery, state: FSMContext, bot: Bot):
    _, action, nid = cb.data.split(":")
    nid = int(nid)
    note = await db.get_note(nid)
    await cb.answer()
    if not note or note["status"] != "plan":
        return await cb.message.answer("Этот план уже не активен.")
    if action == "cancel":
        await db.update_note(nid, status="cancelled")
        return await cb.message.edit_reply_markup(reply_markup=None)
    if action == "edit":
        await state.set_state(Notes.plan)
        await state.update_data(nid=nid, msg_id=cb.message.message_id)
        return await cb.message.answer("Что поменять в плане или в главной мысли? /cancel — отмена.")
    await cb.message.edit_reply_markup(reply_markup=None)
    wait = await cb.message.answer("Подбираю фото и пишу заметку…")
    try:
        pid = await pipeline.build_notes_post(nid)
        await slots.propose(pid, None)
        await wait.delete()
        await screen.move_down(bot, "list", mode="inbox", pid=pid)
    except Exception as exc:
        log.exception("notes write")
        await wait.edit_text(f"Не получилось. {curator.explain(exc)}")


@router.message(Notes.plan, F.text, ~F.text.startswith("/"))
async def on_plan_comment(msg: Message, state: FSMContext):
    nid = (await state.get_data())["nid"]
    await state.clear()
    wait = await msg.answer("Правлю план…")
    try:
        note = await db.get_note(nid)
        brief = await curator.notes_replan(json.loads(note["brief"]), msg.text)
        await db.update_note(nid, brief=brief)
        text, kb = _plan_view(nid, brief)
        await wait.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception as exc:
        log.exception("notes_replan")
        await wait.edit_text(f"Не получилось. {curator.explain(exc)}")
