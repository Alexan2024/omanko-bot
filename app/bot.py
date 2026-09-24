"""Кнопки и команды. Всё управление — на одном экране (app/screen.py):
h:… — пульт и его разделы, v:… — действия с постом, n:… — кнопки уведомлений."""
import asyncio
import json
import logging
import re
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app import (cards, config, curator, dates, db, finds, formatter, notes, pipeline, queue_view, reels, request, screen,
                 slots, sources, taste, ui, voice)

log = logging.getLogger(__name__)
router = Router()
router.message.filter(F.from_user.id == config.ADMIN_ID)
router.callback_query.filter(F.from_user.id == config.ADMIN_ID)

URL_RE = re.compile(r"https?://\S+")
NOT_COMMAND = ~F.text.startswith("/")


class Edit(StatesGroup):
    text = State()
    rewrite = State()
    ban = State()


class Src(StatesGroup):
    add = State()


class VoiceAdd(StatesGroup):
    phrase = State()


def _parts(cb: CallbackQuery) -> list[str]:
    return cb.data.split(":")


def _background(bot: Bot, coro, what: str) -> None:
    """Долгое дело — в фоне, ошибка приходит сообщением."""
    async def run():
        try:
            await coro
        except Exception as exc:
            log.exception(what)
            await screen.notify(bot, f"⚠️ {what}: {curator.explain(exc)}"[:900])
    asyncio.create_task(run())


_drop = ui.drop
_list = ui.show_post


# ======================= команды =======================

@router.message(Command("start", "help", "menu"))
async def cmd_start(msg: Message, state: FSMContext, bot: Bot):
    await state.clear()
    await _drop(bot, msg.message_id)
    await screen.move_down(bot, "home")


@router.message(Command("next"))
async def cmd_next(msg: Message, bot: Bot):
    await _drop(bot, msg.message_id)
    await screen.move_down(bot, "next", fmt="mini")


@router.message(Command("stats"))
async def cmd_stats(msg: Message, bot: Bot):
    await _drop(bot, msg.message_id)
    await screen.move_down(bot, "stats")


@router.message(Command("collect"))
async def cmd_collect(msg: Message, bot: Bot):
    await _drop(bot, msg.message_id)
    _background(bot, _collect(bot), "Сбор")


@router.message(Command("cancel"))
async def cmd_cancel(msg: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    await _drop(bot, msg.message_id, data.get("prompt"))


@router.message(Command("diag"))
async def cmd_diag(msg: Message):
    """Короткая самопроверка: диск, база, доступ к Claude, пакеты, расход."""
    wait = await msg.answer("Проверяю…")
    lines = []
    try:
        config.IMG_DIR.mkdir(parents=True, exist_ok=True)
        probe = config.DATA_DIR / ".probe"
        probe.write_text("ok")
        probe.unlink()
        size = config.DB_PATH.stat().st_size // 1024 if config.DB_PATH.exists() else 0
        folders = len(list(config.IMG_DIR.glob("*"))) if config.IMG_DIR.exists() else 0
        lines.append(f"💾 Диск {config.DATA_DIR}: доступен · база {size} КБ · папок с фото {folders}")
    except Exception as exc:
        lines.append(f"💾 Диск {config.DATA_DIR}: ❌ {exc!r}\nПроверьте volume в настройках Railway.")
    missing = sum(1 for p in await db.ready_posts()
                  if json.loads(p["images"] or "[]") and not all(Path(x).exists() for x in json.loads(p["images"])))
    lines.append(f"🖼 Постов в запасе с пропавшими фото: {missing}")
    try:
        await curator.ping()
        lines.append("🤖 Claude: отвечает")
    except Exception as exc:
        lines.append(f"🤖 Claude: ❌ {curator.explain(exc)}")
    batches = await db.open_batches()
    lines.append(f"📦 Пакетов на оценке: {len(batches)}" + (f" (старейший с {batches[0]['created_at'][5:16]})" if batches else ""))
    lines.append(f"💵 Сегодня ${await db.cost_today():.2f}, из них фоном ${await db.cost_today(True):.2f} "
                 f"из ${config.DAILY_BUDGET_USD:.2f} · вызовов {await db.calls_today()}")
    lines.append(f"🧠 Модели: оценка {config.CLAUDE_MODEL} · фильтр {config.TRIAGE_MODEL} · тексты {config.WRITER_MODEL}")
    fs = slots.find_slot()
    async with db.connect() as c:
        prints = (await (await c.execute("SELECT COUNT(*) n FROM post_prints")).fetchone())["n"]
    lines.append(f"⚙️ v{config.VERSION} · страховка полуавтомата {'вкл' if config.SEMI_FALLBACK else 'выкл'} · "
                 f"слот находки {'%02d:%02d' % fs if fs else 'выкл'} · повторы: {config.REPEAT_DAYS} дн., "
                 f"отпечатков {prints}")
    from app import reelrender
    ok = await asyncio.to_thread(reelrender.ffmpeg_ok)
    lines.append(f"🎬 Рилсы: {'ffmpeg есть' if ok else '❌ нет ffmpeg — видео не соберётся'} · "
                 f"{', '.join(reels.DOW)} в {reels.TIME[0]:02d}:{reels.TIME[1]:02d}"
                 + ("" if reels.ENABLED else " · выключены (REELS=0)"))
    await wait.edit_text("\n\n".join(lines))


@router.message(Command("purge"))
async def cmd_purge(msg: Message, command: CommandObject, bot: Bot):
    source = (command.args or "").strip().lower()
    if not source:
        return await msg.answer("Укажите источник: /purge met")
    n = await db.purge_ready(source)
    await msg.answer(f"Убрано из запаса: {n} ({source})")
    screen.refresh_soon(bot)


# ======================= пульт и разделы =======================

async def _collect(bot: Bot) -> None:
    s = await pipeline.run_collection(manual=True)
    request.after_collection()
    stock = sum(sum(v.values()) for v in (await db.stock_counts()).values())
    lines = [f"🔎 Сбор: новых {s['added']}, отсеяно по заголовку и дублям {s['dropped'] + s['dups']}, "
             f"фильтр пропустил {s['yes']} из {s['yes'] + s['no']}."]
    if s["queued"]:
        lines.append(f"⏳ На оценке: {s['queued']}. Готовые посты появятся в запасе обычно в течение часа, "
                     "иногда дольше (пакетная оценка вдвое дешевле).")
    if s["made"]:
        lines.append(f"📦 Сразу в запас: +{s['made']}.")
    if s["note"]:
        lines.append(f"ℹ️ {s['note']}")
    lines.append(f"📦 В запасе сейчас: {stock}")
    await screen.notify(bot, "\n".join(lines), [("🏠 Экран", "n:home")])
    screen.refresh_soon(bot)


@router.callback_query(F.data.startswith("h:"))
async def on_home(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await screen.adopt(cb.message)
    p = _parts(cb)
    action = p[1]

    if action == "home":
        await cb.answer()
        return await screen.show(bot, "home", day=0)
    if action == "day":
        await cb.answer("Обновлено")
        return await screen.show(bot, "home", day=int(p[2]))
    if action == "mode":
        await db.set_setting("mode", p[2])
        hints = {
            "manual": f"Ручной: в {', '.join(map(str, config.DELIVERY_HOURS))} ч кладу посты во входящие, решаешь ты",
            "semi": f"Полуавтомат: в {config.PLAN_TIME[0]:02d}:{config.PLAN_TIME[1]:02d} собираю план на завтра — "
                    "по посту на слот, ты одобряешь. Сейчас соберу план на остаток дня",
            "auto": f"Автомат: анонс за {config.SLOT_LEAD_MIN} мин, публикую сам от {config.AUTO_MIN_SCORE}/10",
        }
        await cb.answer(hints[p[2]], show_alert=True)
        await screen.show(bot, "home", day=0)

        async def plan_now():
            text = await slots.on_mode_change(p[2])
            if text:
                await screen.notify(bot, text, [("📥 Разобрать", "n:inbox")])
            screen.refresh_soon(bot)
        return _background(bot, plan_now(), "План")
    if action == "pause":
        now_paused = not await slots.paused()
        await db.set_setting("paused", now_paused)
        if not now_paused:
            await slots.reschedule()
        await cb.answer("Пауза: ничего не публикую" if now_paused else "Работаю", show_alert=now_paused)
        return await screen.show(bot, "home", day=0)
    if action == "inbox":
        await cb.answer()
        return await screen.show(bot, "list", mode="inbox", idx=0)
    if action == "stock":
        await cb.answer()
        return await screen.show(bot, "stockmenu")
    if action == "stk":
        await cb.answer()
        return await screen.show(bot, "list", mode="stock", cat=p[2], idx=0)
    if action == "next":
        await cb.answer()
        return await screen.show(bot, "next", fmt=p[2])
    if action == "nx":
        return await _next_post(cb, bot, p[2], None if p[3] == "any" else p[3])
    if action == "plan" and len(p) == 2:
        await cb.answer()
        return await screen.show(bot, "plan")
    if action == "plan":
        await cb.answer("Собираю план — большим постам пишу текст, это до минуты…")
        made, missing = await slots.build_plan(int(p[2]))
        summary = slots.plan_summary(made, missing, "сегодня" if p[2] == "0" else "завтра")
        return await screen.show(bot, "list", mode="inbox", idx=0, note=summary.splitlines()[0])
    if action == "collect":
        await cb.answer("Собираю. Пришлю сводку", show_alert=False)
        return _background(bot, _collect(bot), "Сбор")
    if action == "perfr":
        await cb.answer("Обновляю цифры…")
        from app import stats
        await stats.refresh(bot)
        return await screen.show(bot, "perf")
    if action in ("stats", "digest", "src", "voice", "perf", "taste"):
        await cb.answer()
        return await screen.show(bot, action)
    if action == "srct":
        name = p[2]
        off = await sources.disabled()
        if name in off:
            off.discard(name)
            await cb.answer(f"{name} включён")
        else:
            off.add(name)
            purged = await db.purge_ready(name)
            await cb.answer(f"{name} выключен" + (f", из запаса убрано {purged}" if purged else ""), show_alert=bool(purged))
        await db.set_setting("disabled_sources", sorted(off))
        return await screen.show(bot, "src")
    if action == "srcadd":
        await cb.answer()
        return await _ask(bot, state, Src.add, "Пришлите строкой: <code>имя https://адрес-rss</code>\n"
                                               "Имя — латиницей, до 20 знаков. /cancel — отмена.")
    if action == "vdel":
        gone = await voice.remove_banned(int(p[2]))
        await cb.answer(f"Снял запрет: {gone}" if gone else "Уже нет")
        return await screen.show(bot, "voice")
    if action == "vadd":
        await cb.answer()
        return await _ask(bot, state, VoiceAdd.phrase,
                          "Какие слова или обороты боту больше не писать? Можно несколько через «;». /cancel — отмена.")
    if action == "notes":
        await cb.answer()
        return await notes.propose(cb.message)
    if action == "slot":
        await cb.answer()
        return await screen.show(bot, "slot", key=slots.dec(p[2]))
    if action == "sk":
        key = slots.dec(p[2])
        now_skipped = await slots.toggle_skip(key)
        await cb.answer("Слот пропускается" if now_skipped else "Слот снова в работе")
        return await screen.show(bot, "slot", key=key)
    if action == "sf":
        pid, key = int(p[2]), slots.dec(p[3])
        post = await db.get_post(pid)
        if post and post["status"] in ("approved", "announced"):
            await db.update_post(pid, status="sent", slot_key=None, sent_at=db.now())
        await cb.answer("Слот свободен, пост — во входящих")
        return await screen.show(bot, "slot", key=key)
    if action == "so":
        return await _offer_for_slot(cb, bot, slots.dec(p[2]), None if p[3] == "any" else p[3])
    if action == "open":
        await cb.answer()
        return await screen.show(bot, "list", mode=p[3], pid=int(p[2]), kb=None)
    await cb.answer()


async def _with_text(bot: Bot, pid: int, **arg) -> None:
    """Большому посту без текста — пишем текст, показывая это на экране."""
    post = await db.get_post(pid)
    if slots.has_text(post):
        return
    await _list(bot, pid=pid, note="✍️ Пишу текст…", **arg)
    await pipeline.ensure_text(pid)


async def _next_post(cb: CallbackQuery, bot: Bot, fmt: str, cat: str | None):
    post = await pipeline.pick_next(fmt, category=cat)
    if not post:
        stock = await db.stock_counts()
        where = " · ".join(f"{config.CATEGORIES[c]} {sum(v.values())}" for c, v in stock.items()
                           if c in config.CATEGORIES and sum(v.values()))
        label = config.CATEGORIES.get(cat, "запасе") if cat else "запасе"
        return await cb.answer(f"В «{label}» пусто." + (f" Есть: {where}" if where else " Запас пуст — нажми «Собрать»."),
                               show_alert=True)
    await cb.answer("Кладу во входящие")
    await slots.propose(post["id"], None)
    try:
        await _with_text(bot, post["id"], mode="inbox")
    except Exception as exc:
        return await _list(bot, mode="inbox", pid=post["id"], note=curator.explain(exc)[:200])
    await _list(bot, mode="inbox", pid=post["id"])


async def _offer_for_slot(cb: CallbackQuery, bot: Bot, key: str, cat: str | None):
    fmt = slots.slot_fmt(key)
    if not fmt or not await slots.is_free(key):
        return await cb.answer("Слот уже занят или прошёл", show_alert=True)
    post = await pipeline.pick_next(fmt, category=cat, exclude={p["id"] for p in await db.inbox_posts()})
    if not post:
        return await cb.answer("В этой рубрике пусто — выбери другую." if cat else
                               "Запас этого формата пуст — нажми «🔎 Собрать сейчас» на пульте.", show_alert=True)
    await cb.answer("Предлагаю")
    await slots.propose(post["id"], key)
    try:
        await _with_text(bot, post["id"], mode="inbox")
    except Exception as exc:
        return await _list(bot, mode="inbox", pid=post["id"], note=curator.explain(exc)[:200])
    await _list(bot, mode="inbox", pid=post["id"])


# ======================= действия с постом =======================

async def _approve(bot: Bot, pid: int, key: str) -> str:
    await _with_text(bot, pid)
    await db.update_post(pid, status="approved", decided_at=db.now(), slot_key=key)
    return f"Выйдет {slots.human_key(key)}" + (" (сейчас пауза)" if await slots.paused() else "")


@router.callback_query(F.data.startswith("v:"))
async def on_post(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await screen.adopt(cb.message)
    p = _parts(cb)
    action = p[1]
    if action == "noop":
        return await cb.answer()
    pid = int(p[2])
    post = await db.get_post(pid)
    if not post:
        await cb.answer("Пост не найден")
        return await screen.show(bot, "home")
    st = post["status"]
    active = ("sent", "approved", "announced", "ready")

    if action == "nav":
        view, arg = await screen.current()
        ids = await screen._list_ids(arg.get("mode", "inbox"), arg.get("cat"), pid)
        if not ids:
            await cb.answer()
            return await _list(bot)
        i = ids.index(pid) if pid in ids else 0
        nxt = ids[(i + int(p[3])) % len(ids)]
        await cb.answer()
        return await _list(bot, pid=nxt)

    if action in ("back", "pick", "reject", "now", "rej"):
        await cb.answer()
        sub = {"back": None, "pick": "pick", "now": "confirm", "rej": "reject"}.get(action)
        return await _list(bot, pid=pid, kb=sub)

    if action in ("slot", "ps") and st not in active:
        return await cb.answer("Этот пост уже не ждёт решения")

    try:
        if action == "slot":
            key = post["slot_key"]
            if not (key and (await slots.is_free(key) or st == "announced") and slots.key_dt(key) > slots._now()):
                key = await slots.next_free(post["format"] if post["format"] in ("std", "mini") else "std")
            if not key:
                return await cb.answer("Свободных слотов этого формата нет — выбери слот вручную.", show_alert=True)
            await cb.answer("Ставлю в слот…")
            done = await _approve(bot, pid, key)
            return await _list(bot, pid=pid, note=f"🟡 {done}")

        if action == "ps":
            key = slots.dec(p[3])
            if slots.key_dt(key) <= slots._now():
                return await cb.answer("Этот слот уже прошёл", show_alert=True)
            other = await db.approved_in_slot(key)
            if other and other["id"] != pid:
                if st != "approved" or not post["slot_key"]:
                    return await cb.answer("Слот уже занят", show_alert=True)
                await db.update_post(other["id"], slot_key=post["slot_key"])      # меняемся местами
            elif not other and post["slot_key"] != key and not await slots.is_free(key):
                return await cb.answer("Слот занят автопостом — сначала отмени его", show_alert=True)
            await cb.answer("Ставлю…")
            done = await _approve(bot, pid, key)
            return await _list(bot, pid=pid, note=f"🟡 {done}")

        if action == "swap":
            key = post["slot_key"]
            fmt = slots.slot_fmt(key) or post["format"]
            busy = {q["id"] for q in await db.inbox_posts()} | {pid}
            new = await pipeline.pick_next(fmt, exclude=busy)
            if not new:
                return await cb.answer("Замены этого формата в запасе нет", show_alert=True)
            await cb.answer("Меняю…")
            await db.update_post(pid, status="ready", slot_key=None)
            await slots.propose(new["id"], key)
            await _with_text(bot, new["id"])
            return await _list(bot, pid=new["id"], note="🔄 Заменил: прежний вернулся в запас")

        if action == "nowok":
            await cb.answer("Публикую…")
            ok = await cards.publish_post(bot, pid, "вручную")
            return await _list(bot, pid=pid, note="✅ Опубликовано" if ok else "Уже опубликовано")

        if action == "rr":
            reason = screen.REJECT_REASONS.get(p[3], p[3])
            await db.update_post(pid, status="rejected", reject_reason=reason, decided_at=db.now(), slot_key=None)
            await cb.answer(f"Отклонил: {reason}. Учту")
            return await _list(bot, pid=pid)

        if action == "unslot":
            if st not in ("approved", "announced"):
                return await cb.answer("Уже не в слоте")
            await db.update_post(pid, status="sent", slot_key=None, sent_at=db.now())
            await cb.answer("Снял со слота — пост во входящих")
            return await _list(bot, pid=pid)

        if action == "keep":
            await db.update_post(pid, status="approved", decided_at=db.now())
            await cb.answer("Оставил — выйдет по слоту")
            return await _list(bot, pid=pid)

        if action == "toin":
            await slots.propose(pid, None)
            await cb.answer("Во входящих")
            return await _list(bot, pid=pid)

        if action == "restore":
            await db.update_post(pid, status="ready", reject_reason=None, slot_key=None)
            await cb.answer("Вернул в запас")
            return await _list(bot, pid=pid)

        if action == "write":
            await cb.answer("Пишу текст…")
            await _with_text(bot, pid)
            return await _list(bot, pid=pid, note="✍️ Текст готов")

        if action == "fm":
            fmt = "std" if post["format"] == "mini" else "mini"
            data = json.loads(post["data"])
            lost = bool(data.get("_manual"))
            await cb.answer(("Твоя ручная правка сброшена. " if lost else "")
                            + ("Пишу текст большого поста…" if fmt == "std" and not formatter.has_body(data) else
                               f"Теперь {cards.FORMAT_LABEL[fmt]}"), show_alert=lost)
            if fmt == "std" and not formatter.has_body(data):
                await _list(bot, pid=pid, note="✍️ Пишу текст…")
            await pipeline.set_format(pid, fmt, write=True)
            return await _list(bot, pid=pid)

        if action == "ph":
            await cb.answer("Нажми номер, чтобы убрать или вернуть фото")
            view, arg = await screen.current()
            if arg.get("kb") not in ("photos", "cover"):
                await screen.add_temp(await cards.send_album(bot, post))
            return await _list(bot, pid=pid, kb="photos")

        if action == "pc":
            await cb.answer("Какое фото поставить первым?")
            return await _list(bot, pid=pid, kb="cover")

        if action in ("px", "pcs"):
            i = int(p[3])
            data = json.loads(post["data"])
            n = len(json.loads(post["images"]))
            excluded = set(data.get("_excluded") or [])
            if action == "pcs":
                data["_cover"] = None if data.get("_cover") == i else i
                excluded.discard(i)
            elif i in excluded:
                excluded.discard(i)
            else:
                if len(excluded) >= n - 1:
                    return await cb.answer("Должно остаться хотя бы одно фото", show_alert=True)
                excluded.add(i)
                if data.get("_cover") == i:
                    data["_cover"] = None
            data["_excluded"] = sorted(excluded)
            await db.update_post(pid, data=data)
            await cb.answer()
            return await _list(bot, pid=pid, kb="cover" if action == "pcs" else "photos")

        if action == "txt":
            await cb.answer()
            m = await bot.send_message(config.ADMIN_ID, post["caption"][:4000], disable_web_page_preview=True)
            return await screen.add_temp([m.message_id])

        if action == "sbg":
            await cb.answer("Собираю фон…")
            from app import stories
            await stories.send_background(bot, pid)
            return
        if action == "ig":
            await cb.answer("Собираю пакет…")
            await cards.instagram_pack(bot, pid)
            return

        if action == "edit":
            await cb.answer()
            return await _ask(bot, state, Edit.text,
                              "Пришли текст поста целиком, с форматированием, как он должен выйти в канале. "
                              "Бот запомнит твою правку и будет писать ближе к ней. /cancel — отмена.", pid=pid)
        if action == "rw":
            await cb.answer()
            return await _ask(bot, state, Edit.rewrite,
                              "Что поправить? Напиши комментарий или «-», чтобы просто переписать. /cancel — отмена.", pid=pid)
        if action == "ban":
            await cb.answer()
            return await _ask(bot, state, Edit.ban,
                              "Какое слово или оборот боту больше не писать? Можно несколько через «;». /cancel — отмена.",
                              pid=pid)
    except Exception as exc:
        log.exception("Кнопка %s", cb.data)
        try:
            await cb.answer("Не получилось", show_alert=False)
        except Exception:
            pass
        return await _list(bot, pid=pid, note=curator.explain(exc)[:200])
    await cb.answer()


# ======================= ввод текста =======================

_ask = ui.ask


async def _finish(msg: Message, state: FSMContext, bot: Bot) -> dict:
    data = await state.get_data()
    await state.clear()
    await _drop(bot, msg.message_id, data.get("prompt"))
    return data


@router.message(Edit.text, F.text, NOT_COMMAND)
async def on_edit_text(msg: Message, state: FSMContext, bot: Bot):
    html_text = msg.html_text
    d = await _finish(msg, state, bot)
    pid = d["pid"]
    post = await db.get_post(pid)
    before = formatter.plain_text(post["caption"])
    data = json.loads(post["data"])
    data["_manual"] = True
    await db.update_post(pid, caption=html_text, data=data)
    if before and before != formatter.plain_text(html_text):
        await db.add_edit(pid, post["format"], before[:1500], formatter.plain_text(html_text)[:1500])
    await _list(bot, pid=pid, note="✏️ Текст заменён — правку запомнил")


@router.message(Edit.rewrite, F.text, NOT_COMMAND)
async def on_rewrite_comment(msg: Message, state: FSMContext, bot: Bot):
    comment = "" if msg.text.strip() == "-" else msg.text.strip()
    d = await _finish(msg, state, bot)
    pid = d["pid"]
    await _list(bot, pid=pid, note="🔁 Переписываю…")
    try:
        await pipeline.rewrite(pid, comment)
        await _list(bot, pid=pid, note="🔁 Переписано")
    except Exception as exc:
        log.exception("rewrite")
        await _list(bot, pid=pid, note=curator.explain(exc)[:200])


@router.message(Edit.ban, F.text, NOT_COMMAND)
async def on_ban(msg: Message, state: FSMContext, bot: Bot):
    raw = msg.text
    d = await _finish(msg, state, bot)
    added = await voice.add_banned(raw)
    post = await db.get_post(d["pid"])
    note = ("🚫 Запомнил: " + "; ".join(added)) if added else "🚫 Такое уже в запретах"
    if post and any(a.lower() in formatter.plain_text(post["caption"]).lower() for a in added):
        note += ". В этом посте оно есть — нажми «🔁 Переписать»"
    await _list(bot, pid=d["pid"], note=note)


@router.message(VoiceAdd.phrase, F.text, NOT_COMMAND)
async def on_voice_add(msg: Message, state: FSMContext, bot: Bot):
    raw = msg.text
    await _finish(msg, state, bot)
    await voice.add_banned(raw)
    await screen.show(bot, "voice")


@router.message(Src.add, F.text, NOT_COMMAND)
async def on_add_feed(msg: Message, state: FSMContext, bot: Bot):
    m = re.fullmatch(r"\s*([a-z0-9_]{2,20})\s+(https?://\S+)\s*", msg.text, re.I)
    if not m:
        return await msg.answer("Формат: <code>имя https://адрес-rss</code>. Попробуйте ещё раз или /cancel.")
    await _finish(msg, state, bot)
    name, url = m.group(1).lower(), m.group(2)
    try:
        n = await sources.check_feed(url)
    except Exception as exc:
        return await screen.notify(bot, f"Лента не открылась: {exc!r}"[:500])
    if not n:
        return await screen.notify(bot, "По этому адресу нет записей RSS — не добавляю.")
    feeds = await db.get_setting("custom_feeds", {})
    feeds[name] = url
    await db.set_setting("custom_feeds", feeds)
    await screen.show(bot, "src")


# ======================= уведомления =======================

@router.callback_query(F.data.startswith("n:"))
async def on_notice(cb: CallbackQuery, bot: Bot):
    p = _parts(cb)
    action = p[1]
    await cb.answer()
    if action == "cancel":
        pid = int(p[2])
        post = await db.get_post(pid)
        if post and post["status"] == "announced":
            await db.update_post(pid, status="sent", slot_key=None, sent_at=db.now())
            await cb.message.edit_text("🚫 Автопост отменён — пост во входящих.")
        else:
            await cb.message.edit_reply_markup(reply_markup=None)
        return screen.refresh_soon(bot)
    await _drop(bot, cb.message.message_id)
    if action == "inbox":
        return await screen.move_down(bot, "list", mode="inbox", idx=0)
    if action == "open":
        pid = int(p[2])
        post = await db.get_post(pid)
        mode = "inbox" if post and post["status"] in ("sent", "announced") else "one"
        return await screen.move_down(bot, "list", mode=mode, pid=pid)
    return await screen.move_down(bot, "home")


# ======================= пост по ссылке =======================

@router.message(StateFilter(None), F.text.regexp(URL_RE, search=True))
async def on_link(msg: Message, bot: Bot):
    url = URL_RE.search(msg.text).group(0).rstrip(").,")
    wait = await msg.answer("Собираю пост из ссылки…")
    try:
        pid, note = await pipeline.process_link(url)
    except Exception as exc:
        log.exception("link")
        return await wait.edit_text(curator.explain(exc))
    if not pid:
        return await wait.edit_text(f"Пост не собрался: {note}")
    post = await db.get_post(pid)
    if post["status"] == "ready":
        await slots.propose(pid, None)
    await _drop(bot, wait.message_id, msg.message_id)
    await screen.move_down(bot, "list", mode="inbox", pid=pid)


# кнопки из прошлых версий бота — чтобы не висели молча (отдельный роутер, проверяется последним)
stale = Router()


@stale.callback_query()
async def on_stale(cb: CallbackQuery):
    await cb.answer("Эта кнопка из прошлой версии бота. Открой /menu — там всё по-новому.", show_alert=True)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


# порядок важен: сначала свои обработчики этого роутера (пост по ссылке, ввод в диалогах), потом дочерние;
# «пост по запросу» ловит любой обычный текст, поэтому идёт после заметок, а «старые кнопки» — последними
router.include_router(notes.router)
router.include_router(finds.router)
router.include_router(taste.router)
router.include_router(dates.router)
router.include_router(queue_view.router)
router.include_router(reels.router)      # до «поста по запросу»: тот ловит любой текст
router.include_router(request.router)
router.include_router(stale)
