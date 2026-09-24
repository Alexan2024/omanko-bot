"""Самообучение вкуса. Раз в неделю (TASTE_DOW, TASTE_HOUR — воскресенье 19:00) бот смотрит на твои решения
за 60 дней — что одобрил, что отклонил и почему, как правил тексты — и, когда наберётся статистика
(от TASTE_MIN_STATS постов с недельными просмотрами), на то, что заходит подписчикам. Из этого Claude
выводит до пяти коротких правил.

Правила сначала приходят тебе: «🧠 Вкус» (из «✍️ Голос» или по уведомлению) — ✅ принять, ✏️ поправить,
✕ отклонить. В работу идут только принятые: правила отбора — в оценку материалов, правила текста — в тексты.
Отклонённые бот запоминает и больше не предлагает. Принятое можно выключить там же."""
import html
import json
import logging
import os

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app import config, db, screen, ui
from app.screen import btn

log = logging.getLogger(__name__)
router = Router()
router.message.filter(F.from_user.id == config.ADMIN_ID)
router.callback_query.filter(F.from_user.id == config.ADMIN_ID)

DOW = os.getenv("TASTE_DOW", "sun")
HOUR = int(os.getenv("TASTE_HOUR", "19"))
MIN_STATS = int(os.getenv("TASTE_MIN_STATS", "20"))
MAX_ACTIVE = 15
TARGET = {"select": "отбор", "write": "текст"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS taste_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    target TEXT NOT NULL,       -- select | write
    why TEXT,
    status TEXT NOT NULL,       -- proposed | active | rejected | off
    created_at TEXT NOT NULL,
    decided_at TEXT
);
"""

SYSTEM = """Ты помогаешь автору Telegram-канала AHMAG (архитектура, искусство, фотография, архив, кино) понять его собственный вкус. Тебе дают его решения по постам, которые предлагал бот, его правки текстов и, если есть, статистику просмотров.

Выведи до 5 новых правил. Правило — одна короткая конкретная фраза, которую можно применить к новому материалу: что брать и что не брать (target select) или как писать (target write). Хорошо: «Интерьеры без людей и без мебели из каталога берём охотно», «Не брать реставрации, где от старого здания остался только фасад», «В мини-фразе называть материал, если он виден на фото». Плохо: общие слова («качественная архитектура»), пересказ профиля канала, правило по одному-двум случаям.

Опирайся только на то, что видно в данных, и в why коротко укажи, на чём основано (например: «отклонены 4 из 5 рендерных интерьеров»). Статистика просмотров — отдельный сигнал: правило на её основе помечай в why словом «просмотры». Не повторяй действующие правила и не предлагай снова отклонённые автором. Если данных мало или закономерностей нет — верни меньше правил или пустой список.

Верни ТОЛЬКО JSON: {"rules": [{"text": "...", "target": "select|write", "why": "..."}]}"""


class TasteEdit(StatesGroup):
    text = State()


async def init() -> None:
    async with db.connect() as c:
        await c.executescript(SCHEMA)
        await c.commit()


def install(bot: Bot) -> None:
    screen.VIEWS["taste"] = _v_taste


def schedule(sched, bot: Bot, guarded) -> None:
    sched.add_job(guarded(bot, "правила вкуса", weekly, bot), "cron", day_of_week=DOW, hour=HOUR, minute=0,
                  id="taste")


# ======================= правила =======================

async def rules(status: str | None = None) -> list:
    q, args = "SELECT * FROM taste_rules", ()
    if status:
        q, args = q + " WHERE status=?", (status,)
    async with db.connect() as c:
        return await (await c.execute(q + " ORDER BY id", args)).fetchall()


async def active_text(target: str) -> str:
    """Для промптов: принятые автором правила нужного вида."""
    rs = [r["text"] for r in await rules("active") if r["target"] == target][-MAX_ACTIVE:]
    if not rs:
        return ""
    head = "# Правила вкуса, подтверждённые автором — учитывай при оценке" if target == "select" \
        else "# Правила текста, подтверждённые автором"
    return head + "\n" + "\n".join(f"- {t}" for t in rs)


async def _set(rid: int, **f) -> None:
    f["decided_at"] = db.now()
    async with db.connect() as c:
        await c.execute(f"UPDATE taste_rules SET {','.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), rid))
        await c.commit()


# ======================= данные для Claude =======================

async def _material() -> tuple[str, int]:
    """→ (текст для Claude, сколько решений в нём)."""
    async with db.connect() as c:
        posts = await (await c.execute(
            "SELECT id, status, source, category, format, score, data, reject_reason, slot_key FROM posts "
            "WHERE status IN ('published','approved','rejected') AND format!='notes' AND source!='digest' "
            "AND COALESCE(decided_at, created_at)>=? ORDER BY id DESC LIMIT 150", (db.days_ago(60),))).fetchall()
        edits = await (await c.execute("SELECT before, after FROM edits ORDER BY id DESC LIMIT 12")).fetchall()
        stats = await (await c.execute(
            "SELECT p.source, p.category, p.format, p.slot_key, p.data, s.tg_7d FROM posts p "
            "JOIN post_stats s ON s.post_id=p.id WHERE s.tg_7d IS NOT NULL AND p.decided_at>=? "
            "ORDER BY s.tg_7d DESC", (db.days_ago(60),))).fetchall()
    head = lambda d: (json.loads(d).get("headline") or "?")[:90]
    lines = ["# Решения автора за 60 дней: ✓ взял, ✗ отклонил | рубрика | источник | формат | оценка бота | заголовок"]
    for p in posts:
        mark = "✗" if p["status"] == "rejected" else "✓"
        why = f" — причина: {p['reject_reason']}" if p["status"] == "rejected" and p["reject_reason"] else ""
        lines.append(f"{mark} | {p['category']} | {p['source']} | {p['format']} | {p['score']} | {head(p['data'])}{why}")
    if edits:
        lines += ["", "# Как автор правил тексты бота (было → стало)"]
        lines += [f"- {e['before'][:300]} → {e['after'][:300]}" for e in edits]
    if len(stats) >= MIN_STATS:
        q = max(3, len(stats) // 4)
        lines += ["", f"# Просмотры через неделю ({len(stats)} постов): четверть лучших и четверть худших"]
        for label, part in (("лучшие", stats[:q]), ("худшие", stats[-q:])):
            lines.append(f"## {label}")
            lines += [f"- {s['tg_7d']} | {s['category']} | {s['source']} | {s['format']} | "
                      f"{(s['slot_key'] or '')[-5:]} | {head(s['data'])}" for s in part]
    old = await rules()
    for st, title in (("active", "Действующие правила"), ("rejected", "Автор отклонил — не предлагать снова")):
        rs = [r["text"] for r in old if r["status"] == st]
        if rs:
            lines += ["", f"# {title}"] + [f"- {t}" for t in rs]
    return "\n".join(lines), len(posts)


async def propose() -> tuple[int, str]:
    """Новые правила в статус proposed. → (сколько, пояснение)."""
    from app import curator
    text, n = await _material()
    if n < 10:
        return 0, f"решений за 60 дней пока {n} — нужно хотя бы 10"
    data = await curator._call(text, system=SYSTEM, model=config.CLAUDE_MODEL, max_tokens=1200)
    have = {r["text"].strip().lower() for r in await rules()}
    added = 0
    async with db.connect() as c:
        for r in (data.get("rules") or [])[:5]:
            t = str(r.get("text") or "").strip()
            if not t or t.lower() in have or len(t) > 300:
                continue
            target = r.get("target") if r.get("target") in TARGET else "select"
            await c.execute("INSERT INTO taste_rules(text, target, why, status, created_at) VALUES (?,?,?,?,?)",
                            (t, target, str(r.get("why") or "")[:200], "proposed", db.now()))
            added += 1
        await c.commit()
    return added, "" if added else "новых закономерностей не нашлось"


async def weekly(bot: Bot) -> None:
    n, _ = await propose()
    if n:
        await screen.notify(bot, f"🧠 Бот вывел правил о твоём вкусе: {n}. Прими, поправь или отклони — "
                                 "в работу пойдут только принятые.", [("🧠 Открыть", "tr:go")])


# ======================= экран =======================

async def _v_taste(arg: dict):
    prop, act = await rules("proposed"), await rules("active")
    lines = ["<b>🧠 Вкус</b> — правила, которые бот вывел из твоих решений" +
             (" и просмотров" if await _has_stats() else "") + "."]
    if arg.get("note"):
        lines.append(f"<b>{html.escape(arg['note'])}</b>")
    rows = []
    if prop:
        r = prop[0]
        lines += ["", f"<b>Новое · {len(prop)}</b> ({TARGET[r['target']]})", f"«{html.escape(r['text'])}»"]
        if r["why"]:
            lines.append(f"<i>{html.escape(r['why'])}</i>")
        rows.append([btn("✅ Принять", f"tr:ok:{r['id']}"), btn("✏️ Поправить", f"tr:ed:{r['id']}"),
                     btn("✕ Отклонить", f"tr:no:{r['id']}")])
    lines += ["", f"<b>Действуют · {len(act)}</b>"]
    lines += [f"{i + 1}. {html.escape(r['text'])} <i>({TARGET[r['target']]})</i>" for i, r in enumerate(act)] or ["пока нет"]
    offs = [btn(f"✕ {i + 1}", f"tr:off:{r['id']}") for i, r in enumerate(act)]
    rows += [offs[i:i + 6] for i in range(0, len(offs), 6)]
    rows.append([btn("🔄 Предложить сейчас", "tr:new"), btn("← Голос", "h:voice")])
    return screen.banner(), "\n".join(lines)[:1020], screen._kb(rows), arg


async def _has_stats() -> bool:
    async with db.connect() as c:
        r = await (await c.execute("SELECT COUNT(*) n FROM post_stats WHERE tg_7d IS NOT NULL")).fetchone()
    return r["n"] >= MIN_STATS


@router.callback_query(F.data.startswith("tr:"))
async def on_cb(cb: CallbackQuery, bot: Bot, state: FSMContext):
    p = cb.data.split(":")
    a = p[1]
    if a == "go":
        await cb.answer()
        await ui.drop(bot, cb.message.message_id)
        return await screen.move_down(bot, "taste")
    await screen.adopt(cb.message)
    if a == "new":
        await cb.answer("Смотрю на твои решения — до минуты…")
        from app import curator
        try:
            n, why = await propose()
        except Exception as exc:
            return await screen.show(bot, "taste", note=curator.explain(exc)[:200])
        return await screen.show(bot, "taste", note=f"Новых правил: {n}" if n else why)
    rid = int(p[2])
    if a == "ok":
        await _set(rid, status="active")
        await cb.answer("Принято — учитываю")
    elif a == "no":
        await _set(rid, status="rejected")
        await cb.answer("Отклонено — больше не предложу")
    elif a == "off":
        await _set(rid, status="off")
        await cb.answer("Правило выключено")
    elif a == "ed":
        await cb.answer()
        return await ui.ask(bot, state, TasteEdit.text, "Пришли правило, как оно должно звучать. "
                                                        "Оно сразу пойдёт в работу. /cancel — отмена.", rid=rid)
    return await screen.show(bot, "taste")


@router.message(TasteEdit.text, F.text, ~F.text.startswith("/"))
async def on_edit(msg: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    await ui.drop(bot, msg.message_id, data.get("prompt"))
    await _set(int(data["rid"]), text=msg.text.strip()[:300], status="active")
    await screen.show(bot, "taste", note="Правило поправлено и принято")
