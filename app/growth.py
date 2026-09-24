"""Раздел «📈 Рост» на экране бота: откуда приходят подписчики, пригласительные ссылки, подборки, партнёры.
Встраивается в экран без правки screen.py: install() добавляет свои виды в screen.VIEWS и кнопку на пульт.
Кнопки раздела — g:…; router подключается в main.py раньше основного, чтобы их не перехватил обработчик
«кнопок из прошлой версии»."""
import asyncio
import html
import logging
import os

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app import attribution, config, curator, db, digests, partners, screen, slots

log = logging.getLogger(__name__)
router = Router(name="growth")
router.message.filter(F.from_user.id == config.ADMIN_ID)
router.callback_query.filter(F.from_user.id == config.ADMIN_ID)

NOT_COMMAND = ~F.text.startswith("/")
btn = screen.btn
_bot: Bot | None = None

# расписание: подборка недели — во входящие к воскресенью, поиск партнёров — в понедельник
WEEK_DOW = os.getenv("G_WEEK_DOW", "sun")
WEEK_HOUR = int(os.getenv("G_WEEK_HOUR", "11"))
PARTNERS_DOW = os.getenv("G_PARTNERS_DOW", "mon")
PARTNERS_HOUR = int(os.getenv("G_PARTNERS_HOUR", "12"))


class G(StatesGroup):
    link = State()
    partner = State()


async def init() -> None:
    await attribution.init()
    await partners.init()


def install(bot: Bot) -> None:
    """Виды раздела — в экран. Кнопка «📈 Рост» на пульте — в screen._home."""
    global _bot
    _bot = bot
    screen.VIEWS.update({"growth": _v_growth, "glinks": _v_links, "gthemes": _v_themes, "gpart": _v_part})


def schedule(sched, bot: Bot, guarded) -> None:
    sched.add_job(guarded(bot, "подписчики: снимок", attribution.snapshot, bot), "cron",
                  hour=23, minute=50, id="g_snapshot")
    sched.add_job(guarded(bot, "подборка недели", weekly_job, bot), "cron",
                  day_of_week=WEEK_DOW, hour=WEEK_HOUR, minute=0, id="g_week")
    sched.add_job(guarded(bot, "поиск партнёров", partners_job, bot), "cron",
                  day_of_week=PARTNERS_DOW, hour=PARTNERS_HOUR, minute=0, id="g_partners")
    sched.add_job(guarded(bot, "рост за неделю", summary_job, bot), "cron",
                  day_of_week=config.DIGEST_DOW, hour=config.DIGEST_HOUR, minute=5, id="g_summary")


def _n(x: int | None) -> str:
    return f"{x:,}".replace(",", " ") if x is not None else "?"


# ======================= виды экрана =======================

async def _v_growth(arg: dict):
    days = int(arg.get("days") or 7)
    subs = await attribution.subscribers(_bot)
    was = await attribution.count_days_ago(days)
    head = f"Подписчиков: {_n(subs)}"
    if subs is not None and was is not None:
        head += f" · за {days} дн. {subs - was:+d}"
    joins, leaves = await attribution.totals(days)
    lines = ["<b>📈 Рост</b>", head, f"Пришли {joins} · ушли {leaves}", ""]
    src = await attribution.by_source(days)
    if src:
        lines.append(f"<b>Откуда за {days} дн.</b>")
        lines += [f"{html.escape(name)} — {j}" + (f", ушли {l}" if l else "") for name, j, l in src[:7]]
    else:
        lines.append("Вступлений пока не видно: учёт идёт с этой версии бота. "
                     "Заведи ссылку на каждую площадку в «🔗 Ссылки».")
    if await attribution.can_invite(_bot) is False:
        lines += ["", "⚠️ У бота нет права «Приглашать пользователей» в канале — ссылки не создать. "
                      "Канал → Администраторы → бот → включи это право."]
    links = await attribution.links()
    c = await partners.counts()
    work = sum(c.get(s, 0) for s in partners.WORK)
    other = 30 if days == 7 else 7
    rows = [
        [btn(f"🔗 Ссылки · {len(links)}", "g:links"), btn(f"📊 За {other} дней", f"g:days:{other}")],
        [btn("🗞 Подборка недели", "g:week"), btn("🧩 Тематическая", "g:themes")],
        [btn(f"🤝 Партнёры · {c.get('proposed', 0)}", "g:part:prop:0"), btn(f"📋 В работе · {work}", "g:part:work:0")],
        [btn("← Пульт", "h:home")],
    ]
    return screen.banner(), "\n".join(lines)[:1000], screen._kb(rows), arg


async def _v_links(arg: dict):
    links = await attribution.links()
    lines = ["<b>🔗 Пригласительные ссылки</b>",
             "Своя ссылка на каждую площадку: Instagram, VK, сайт, каждый посев. "
             "Бот видит, кто по какой вступил и кто потом ушёл.", ""]
    for i, l in enumerate(links):
        lines.append(f"{i + 1}. <b>{html.escape(l['label'])}</b> — пришли {l['joins']}"
                     + (f", ушли {l['leaves']}" if l["leaves"] else ""))
        lines.append(f"<code>{html.escape(l['url'])}</code>")
    if not links:
        lines.append("Ссылок пока нет.")
    if arg.get("note"):
        lines += ["", f"<b>{html.escape(arg['note'])}</b>"]
    dels = [btn(f"✕ {i + 1}", f"g:lrev:{l['id']}") for i, l in enumerate(links)]
    rows = [dels[i:i + 5] for i in range(0, len(dels), 5)]
    rows += [[btn("➕ Новая ссылка", "g:ladd")], [btn("← Рост", "g:home")]]
    return screen.banner(), "\n".join(lines)[:1000], screen._kb(rows), arg


async def _v_themes(arg: dict):
    themes = await db.get_setting("g_themes", [])
    lines = ["<b>🧩 Тематические подборки</b>",
             "Claude ищет в вышедших постах общие темы. Выбери — подборка ляжет во входящие.", ""]
    if arg.get("note"):
        lines += [f"<b>{html.escape(arg['note'])}</b>", ""]
    for i, t in enumerate(themes):
        lines.append(f"{i + 1}. <b>{html.escape(t['title'])}</b> · постов {len(t['ids'])}")
        if t.get("intro"):
            lines.append(f"<i>{html.escape(t['intro'])}</i>")
    if not themes and not arg.get("note"):
        lines.append("Тем пока нет — нажми «Предложить темы».")
    rows = [[btn(f"{i + 1}", f"g:th:{i}") for i in range(len(themes))],
            [btn("🔄 Предложить темы" if not themes else "🔄 Другие темы", "g:thnew")],
            [btn("← Рост", "g:home")]]
    return screen.banner(), "\n".join(lines)[:1000], screen._kb(rows), arg


async def _v_part(arg: dict):
    tab = arg.get("tab", "prop")
    items = await partners.listing(tab)
    c = await partners.counts()
    work = sum(c.get(s, 0) for s in partners.WORK)
    tabs = [btn(("● " if tab == "prop" else "") + f"Новые · {c.get('proposed', 0)}", "g:part:prop:0"),
            btn(("● " if tab == "work" else "") + f"В работе · {work}", "g:part:work:0")]
    tools = [btn("➕ Добавить каналы", "g:padd"), btn("🔄 Найти новых", "g:pfind")]
    head = "<b>🤝 Партнёры</b>"
    if not items:
        if tab == "prop" and not c.get("_seeds"):
            text = (f"{head}\nДобавь 3–5 каналов, близких по духу: названия через пробел, @имя или t.me-ссылки. "
                    "Бот будет искать новых через их упоминания и репосты, раз в неделю предложит лучших.")
        elif tab == "prop":
            text = f"{head}\nНовых кандидатов нет. «Найти новых» — пройтись по семенам сейчас (пара минут)."
        else:
            text = f"{head}\nВ работе пока никого. Отметь «✅ Написал», когда отправишь письмо."
        if arg.get("note"):
            text += f"\n\n<b>{html.escape(arg['note'])}</b>"
        return screen.banner(), text, screen._kb([tabs, tools, [btn("← Рост", "g:home")]]), arg
    idx = min(int(arg.get("idx") or 0), len(items) - 1)
    p = items[idx]
    me = await partners.ours(_bot)
    u = p["username"]
    text = f"{head} · {idx + 1}/{len(items)}\n\n" + partners.card(p, me["subs"] or 0)
    if arg.get("note"):
        text += f"\n\n<b>{html.escape(arg['note'])}</b>"
    rows = [tabs]
    if p["status"] == "proposed":
        rows += [[btn("✉️ Письмо: взаимный пиар", f"g:pd:m:{u}"), btn("📁 Письмо: папка", f"g:pd:f:{u}")],
                 [btn("✅ Написал", f"g:ps:contacted:{u}"), btn("✕ Не подходит", f"g:ps:skip:{u}")]]
    elif p["status"] == "contacted":
        rows += [[btn("🤝 Договорились", f"g:ps:agreed:{u}"), btn("↩️ Не вышло", f"g:ps:declined:{u}")],
                 [btn("✉️ Ещё письмо", f"g:pd:m:{u}")]]
    else:
        rows += [[btn("↩️ Сотрудничество закончилось", f"g:ps:declined:{u}")]]
    rows.append([InlineKeyboardButton(text="🔗 Открыть канал", url=f"https://t.me/{u}")])
    if len(items) > 1:
        rows.append([btn("◀", f"g:part:{tab}:{(idx - 1) % len(items)}"), btn(f"{idx + 1} / {len(items)}", "g:noop"),
                     btn("▶", f"g:part:{tab}:{(idx + 1) % len(items)}")])
    rows += [tools, [btn("← Рост", "g:home")]]
    arg.update(tab=tab, idx=idx)
    return screen.banner(), text[:1000], screen._kb(rows), arg


# ======================= кнопки =======================

async def _drop(bot: Bot, *ids) -> None:
    for mid in ids:
        if mid:
            try:
                await bot.delete_message(config.ADMIN_ID, mid)
            except Exception:
                pass


def _background(bot: Bot, coro, what: str) -> None:
    async def run():
        try:
            await coro
        except Exception as exc:
            log.exception(what)
            await screen.notify(bot, f"⚠️ {what}: {curator.explain(exc)}"[:900])
    asyncio.create_task(run())


async def _ask(bot: Bot, state: FSMContext, st, text: str) -> None:
    await state.set_state(st)
    m = await bot.send_message(config.ADMIN_ID, text)
    await state.update_data(prompt=m.message_id)
    await screen.add_temp([m.message_id])


async def _part_screen(bot: Bot, note: str | None = None) -> None:
    """Перерисовать карточку партнёра, если экран сейчас на ней, иначе открыть список."""
    view, arg = await screen.current()
    arg = arg if view == "gpart" else {"tab": "prop", "idx": 0}
    await screen.show(bot, "gpart", **{**arg, "note": note})


@router.callback_query(F.data.startswith("g:"))
async def on_growth(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await screen.adopt(cb.message)
    p = cb.data.split(":")
    a = p[1]

    if a == "noop":
        return await cb.answer()
    if a == "rm":
        await cb.answer()
        return await _drop(bot, cb.message.message_id)
    if a == "go":   # кнопка в уведомлении: экран переезжает вниз, уведомление исчезает
        await cb.answer()
        await _drop(bot, cb.message.message_id)
        if p[2] == "part":
            return await screen.move_down(bot, "gpart", tab="prop", idx=0)
        return await screen.move_down(bot, "growth")
    if a in ("home", "days"):
        await cb.answer()
        return await screen.show(bot, "growth", days=int(p[2]) if a == "days" else 7)

    # ---- ссылки ----
    if a == "links":
        await cb.answer()
        return await screen.show(bot, "glinks")
    if a == "ladd":
        await cb.answer()
        return await _ask(bot, state, G.link, "Как назвать ссылку? Коротко, до 32 знаков: «Instagram», «VK», "
                                              "«посев @имяканала», «сайт». /cancel — отмена.")
    if a == "lrev":
        label = await attribution.revoke(bot, int(p[2]))
        await cb.answer(f"Ссылка «{label}» отозвана. Статистика по ней сохранится" if label else "Уже нет",
                        show_alert=bool(label))
        return await screen.show(bot, "glinks")

    # ---- подборки ----
    if a == "week":
        pid, why = await digests.weekly(bot)
        if not pid:
            return await cb.answer(f"Не собралась: {why}", show_alert=True)
        await cb.answer("Подборка во входящих")
        return await screen.show(bot, "list", mode="inbox", pid=pid, kb=None, note="🗞 Подборка недели собрана")
    if a == "themes":
        await cb.answer()
        return await screen.show(bot, "gthemes")
    if a == "thnew":
        await cb.answer("Ищу темы в архиве — до минуты…")
        await screen.show(bot, "gthemes", note="🔄 Ищу темы…")
        try:
            themes, why = await digests.propose_themes()
        except Exception as exc:
            log.exception("темы подборок")
            return await screen.show(bot, "gthemes", note=curator.explain(exc)[:200])
        return await screen.show(bot, "gthemes", note=None if themes else f"Тем нет: {why}")
    if a == "th":
        await cb.answer("Собираю подборку…")
        pid, why = await digests.build_theme(bot, int(p[2]))
        if not pid:
            return await screen.show(bot, "gthemes", note=f"Не собралась: {why}")
        return await screen.show(bot, "list", mode="inbox", pid=pid, kb=None, note="🧩 Подборка собрана")

    # ---- партнёры ----
    if a == "part":
        await cb.answer()
        return await screen.show(bot, "gpart", tab=p[2], idx=int(p[3]))
    if a == "padd":
        await cb.answer()
        return await _ask(bot, state, G.partner, "Пришли каналы через пробел: @имя, имя или t.me-ссылку. "
                                                 "До 15 за раз. /cancel — отмена.")
    if a == "pfind":
        await cb.answer("Ищу. Это пара минут — пришлю сводку", show_alert=False)
        return _background(bot, _find(bot, manual=True), "Поиск партнёров")
    if a == "ps":
        status, u = p[2], p[3]
        await partners.set_status(u, status)
        await cb.answer({"contacted": "Отметил: написал", "agreed": "Отлично, отметил",
                         "declined": "Отметил", "skip": "Убрал из списка"}.get(status, "Готово"))
        if not getattr(cb.message, "photo", None):   # нажато в сообщении с черновиком
            try:
                await cb.message.edit_reply_markup(reply_markup=screen._kb([[btn("🗑 Убрать", "g:rm")]]))
            except Exception:
                pass
        return await _part_screen(bot)
    if a == "pd":
        kind, u = p[2], p[3]
        await cb.answer("Пишу черновик…")
        try:
            text = await partners.draft(bot, u, kind)
        except Exception as exc:
            log.exception("черновик письма")
            return await _part_screen(bot, curator.explain(exc)[:200])
        row = await partners.get(u)
        to = f" · писать: {row['contact']}" if row and row["contact"] else ""
        kb = InlineKeyboardMarkup(inline_keyboard=[[btn("✅ Отправил", f"g:ps:contacted:{u}"), btn("🗑 Убрать", "g:rm")]])
        await bot.send_message(config.ADMIN_ID, f"✉️ Черновик для @{html.escape(u)}{html.escape(to)}\n\n"
                                                f"{html.escape(text)}", reply_markup=kb, disable_web_page_preview=True)
        return
    await cb.answer()


# ======================= ввод текста =======================

async def _finish(msg: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()
    await _drop(bot, msg.message_id, data.get("prompt"))


@router.message(G.link, F.text, NOT_COMMAND)
async def on_link_label(msg: Message, state: FSMContext, bot: Bot):
    label = " ".join(msg.text.split())[:32]
    await _finish(msg, state, bot)
    try:
        await attribution.create(bot, label)
        note = f"Ссылка «{label}» готова — скопируй её и поставь на площадку"
    except Exception as exc:
        log.exception("ссылка")
        note = ("Не получилось: у бота нет права «Приглашать пользователей»"
                if "not enough rights" in str(exc).lower() or "admin" in str(exc).lower() else f"Не получилось: {exc}")
    await screen.show(bot, "glinks", note=note[:200])


@router.message(G.partner, F.text, NOT_COMMAND)
async def on_partner_add(msg: Message, state: FSMContext, bot: Bot):
    raw = msg.text
    await _finish(msg, state, bot)
    await screen.show(bot, "gpart", tab="prop", idx=0, note="Открываю каналы…")
    added, failed = await partners.add_manual(raw)
    note = f"Добавлено: {len(added)}" + (f". Не открылись (закрыты или не каналы): {', '.join(failed)}" if failed else "")
    await screen.show(bot, "gpart", tab="prop", idx=0, note=note[:250])


# ======================= по расписанию =======================

async def _find(bot: Bot, manual: bool = False) -> None:
    s = await partners.crawl(bot)
    if not s["seeds"]:
        if manual or not await db.get_setting("g_seed_hint"):
            await db.set_setting("g_seed_hint", True)
            await screen.notify(bot, "🤝 Для поиска партнёров добавь 3–5 каналов, близких по духу, — "
                                     "бот будет искать через их упоминания и репосты.", [("🤝 Открыть", "g:go:part")])
        return
    n = await partners.propose(bot)
    text = (f"🤝 Прошёлся по {s['seeds']} каналам: упомянуто {s['seen']}, открыл новых {s['opened']}, "
            f"по теме и цифрам подошли {s['fit']}.\n" + (f"В список «Новые» добавил {n}." if n else "Новых в списке нет."))
    if n or manual:
        await screen.notify(bot, text, [("🤝 Открыть", "g:go:part")])


async def partners_job(bot: Bot) -> None:
    await _find(bot)


async def weekly_job(bot: Bot) -> None:
    week = slots_week()
    if await db.get_setting("g_week_last") == week:
        return
    pid, why = await digests.weekly(bot)
    if not pid:
        log.info("Подборка недели не собралась: %s", why)
        return
    await db.set_setting("g_week_last", week)
    await screen.notify(bot, "🗞 Подборка недели собрана и ждёт во входящих. Поставь её в слот или поправь текст.",
                        [("📥 Открыть", f"n:open:{pid}")])
    screen.refresh_soon(bot)


def slots_week() -> str:
    y, w, _ = slots._now().isocalendar()
    return f"{y}-W{w:02d}"


async def summary_job(bot: Bot) -> None:
    await screen.notify(bot, await attribution.summary(bot, 7), [("📈 Рост", "g:go:growth")])
