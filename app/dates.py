"""📅 Даты: посты к значимым датам — дни рождения архитекторов, художников, фотографов, режиссёров,
круглые даты зданий и событий, международные дни (8 марта, Всемирный день архитектуры, день фотографии…).

Раз в неделю (DATES_DOW, DATES_HOUR — понедельник 12:00) Claude с веб-поиском собирает календарь на
DATES_AHEAD дней вперёд: до DATES_MAX поводов, у которых есть угол для канала, а не просто праздник.
Приходит уведомление, экран «📅 Даты» (кнопка на пульте): ✅ взять / ✕ пропустить. Свою дату — командой
/date 9.03 Луис Барраган, дом-студия.

Взятую дату бот собирает за DATES_LEAD дней (каждый день в 11:00) как пост по запросу — с веб-поиском,
фото и текстом, где повод назван в первой фразе, — и ставит в первый большой слот этого дня
(стоявший там пост сдвигается). Приходит уведомление, пост можно открыть и поправить."""
import asyncio
import html
import json
import logging
import os
import re
from datetime import date, datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from app import config, db, screen, slots, ui
from app.screen import btn

log = logging.getLogger(__name__)
router = Router()
router.message.filter(F.from_user.id == config.ADMIN_ID)
router.callback_query.filter(F.from_user.id == config.ADMIN_ID)

DOW = os.getenv("DATES_DOW", "mon")
HOUR = int(os.getenv("DATES_HOUR", "12"))
AHEAD = int(os.getenv("DATES_AHEAD", "21"))
MAX = int(os.getenv("DATES_MAX", "4"))
LEAD = int(os.getenv("DATES_LEAD", "2"))
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS dates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,          -- YYYY-MM-DD
    occasion TEXT NOT NULL,     -- «120 лет со дня рождения Луиса Баррагана»
    query TEXT NOT NULL,        -- о чём пост: объект или работа
    category TEXT,
    why TEXT,
    status TEXT NOT NULL,       -- proposed | accepted | built | skipped | failed
    post_id INTEGER,
    note TEXT,
    created_at TEXT NOT NULL
);
"""

SYSTEM = """Ты составляешь календарь поводов для Telegram-канала AHMAG: построенная архитектура (модернизм, частные дома, сакральное, руины), искусство, документальная и архивная фотография, визуальная культура, авторское кино. Пишет архитектор, для людей со вкусом.

Нужны даты, к которым у канала есть конкретный пост: день рождения или смерти архитектора, художника, фотографа, режиссёра (лучше круглые — 50, 100, 125 лет); круглая дата здания, выставки, фильма, события; международный день, если у него есть визуальный угол (8 марта — работа женщины-архитектора или фотографа; Всемирный день архитектуры — здание; день фотографии — снимок). Не нужны праздники без угла, политика, спорт, мемы.

Для каждой даты сразу выбери, о какой одной вещи будет пост: здание, серия, фильм, работа. query — короткий запрос для поиска этой вещи (например «Casa Luis Barragán Mexico City»). Даты и годы проверяй поиском; не уверен — не включай. Не предлагай то, что уже в списке «было».

Верни ТОЛЬКО JSON: {"dates": [{"day": "YYYY-MM-DD", "occasion": "по-русски: повод", "query": "...", "category": "architecture|art|photography|archive|cinema", "why": "одна фраза: почему это наше"}]}"""


async def init() -> None:
    async with db.connect() as c:
        await c.executescript(SCHEMA)
        await c.commit()


def install(bot: Bot) -> None:
    screen.VIEWS["dates"] = _v_dates


def schedule(sched, bot: Bot, guarded) -> None:
    sched.add_job(guarded(bot, "даты: календарь", weekly, bot), "cron", day_of_week=DOW, hour=HOUR, id="dates")
    sched.add_job(guarded(bot, "даты: сборка постов", build_due, bot), "cron", hour=11, minute=0, id="dates_build")


def human(day: str) -> str:
    d = date.fromisoformat(day)
    return f"{d.day} {MONTHS[d.month - 1]}"


async def items(*statuses: str) -> list:
    async with db.connect() as c:
        q = "SELECT * FROM dates" + (f" WHERE status IN ({','.join('?' * len(statuses))})" if statuses else "")
        return await (await c.execute(q + " ORDER BY day, id", statuses)).fetchall()


async def _set(did: int, **f) -> None:
    async with db.connect() as c:
        await c.execute(f"UPDATE dates SET {','.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), did))
        await c.commit()


async def _add(day: str, occasion: str, query: str, category: str = "", why: str = "", status: str = "proposed") -> int:
    async with db.connect() as c:
        cur = await c.execute("INSERT INTO dates(day, occasion, query, category, why, status, created_at) "
                              "VALUES (?,?,?,?,?,?,?)", (day, occasion[:200], query[:200], category, why[:200],
                                                         status, db.now()))
        await c.commit()
        return cur.lastrowid


# ======================= календарь =======================

async def propose() -> int:
    from app import curator
    today = slots._now().date()
    old = [f"{r['day']} {r['occasion']}" for r in await items() if r["day"] >= (today - timedelta(days=400)).isoformat()]
    prompt = (f"Сегодня {today.isoformat()}. Найди до {MAX} поводов с {(today + timedelta(days=LEAD + 1)).isoformat()} "
              f"по {(today + timedelta(days=AHEAD)).isoformat()}.\n\n# Уже было или предлагалось\n"
              + ("\n".join(old[-60:]) or "—"))
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 6}]
    data = await curator._call(prompt, system=SYSTEM, model=config.CLAUDE_MODEL, max_tokens=2000, tools=tools)
    have = {(r["day"], r["query"].lower()) for r in await items()}
    n = 0
    for d in (data.get("dates") or [])[:MAX]:
        day, q = str(d.get("day") or ""), str(d.get("query") or "").strip()
        try:
            ok = date.fromisoformat(day) > today + timedelta(days=LEAD)
        except ValueError:
            ok = False
        if ok and q and d.get("occasion") and (day, q.lower()) not in have:
            await _add(day, str(d["occasion"]), q, str(d.get("category") or ""), str(d.get("why") or ""))
            n += 1
    return n


async def weekly(bot: Bot) -> None:
    n = await propose()
    if n:
        await screen.notify(bot, f"📅 Поводы на ближайшие недели: {n}. Выбери, к каким датам делать посты.",
                            [("📅 Открыть", "dt:go")])


# ======================= сборка =======================

def _slot_key(day: str) -> str | None:
    """Первый большой слот этого дня (или первый любой, если больших нет), если он ещё впереди."""
    d = date.fromisoformat(day)
    ordered = sorted(config.SLOTS, key=lambda s: (s[2] != "std", s[0], s[1]))
    for h, m, _ in ordered:
        dt = datetime(d.year, d.month, d.day, h, m, tzinfo=slots._now().tzinfo)
        if dt > slots._now() + timedelta(minutes=30):
            return slots.key_of(dt)
    return None


async def build(did: int) -> tuple[int | None, str]:
    from app import request
    r = next((x for x in await items() if x["id"] == did), None)
    if not r:
        return None, "дата не найдена"
    key = _slot_key(r["day"])
    if not key:
        await _set(did, status="failed", note="день уже прошёл")
        return None, "день уже прошёл"
    brief = await request.research(r["query"], False)
    if brief.get("status") != "ok" or not brief.get("facts"):
        await _set(did, status="failed", note="материал не нашёлся")
        return None, "материал не нашёлся"
    brief["facts"] = [{"text": f"Повод поста: {r['occasion']} ({human(r['day'])}). Назови повод в первой фразе."}] \
        + brief["facts"]

    async def quiet(_t):
        return None

    pid, note = await request.assemble(None, r["query"], brief, [], "std", quiet)
    if not pid:
        await _set(did, status="failed", note=str(note)[:120])
        return None, note
    post = await db.get_post(pid)
    data = json.loads(post["data"])
    data["_occasion"] = f"{human(r['day'])}: {r['occasion']}"
    await db.update_post(pid, data=data)
    placed = await slots.place(pid, key)
    await _set(did, status="built", post_id=pid, note=placed[:120])
    return pid, placed


async def build_due(bot: Bot) -> None:
    """Каждый день: взятые даты, до которых осталось DATES_LEAD дней или меньше, — в посты."""
    edge = (slots._now().date() + timedelta(days=LEAD)).isoformat()
    for r in await items("accepted"):
        if r["day"] > edge:
            continue
        try:
            pid, note = await build(r["id"])
        except Exception as exc:
            log.exception("Дата %s", r["id"])
            await _set(r["id"], status="failed", note=str(exc)[:120])
            pid, note = None, "не собрался"
        if pid:
            await screen.notify(bot, f"📅 Пост к дате {human(r['day'])} — {r['occasion']}. {note}",
                                [("👁 Открыть", f"n:open:{pid}")])
        else:
            await screen.notify(bot, f"📅 К дате {human(r['day'])} пост не собрался: {note}. "
                                     "Можно сделать его вручную через «✍️ Пост по запросу».", [("📅 Даты", "dt:go")])
    screen.refresh_soon(bot)


# ======================= экран и команда =======================

ICON = {"proposed": "❔", "accepted": "✅", "built": "🟡", "failed": "⚠️"}


async def _v_dates(arg: dict):
    today = slots._now().date().isoformat()
    rows_d = [r for r in await items("proposed", "accepted", "built", "failed") if r["day"] >= today]
    lines = ["<b>📅 Даты</b> — посты к поводам. ❔ предложено · ✅ взято · 🟡 пост собран · ⚠️ не собрался"]
    if arg.get("note"):
        lines.append(f"<b>{html.escape(arg['note'])}</b>")
    rows = []
    for r in rows_d[:12]:
        lines.append(f"{ICON[r['status']]} {human(r['day'])} — {html.escape(r['occasion'])}"
                     + (f" · <i>{html.escape(r['query'])}</i>" if r["status"] == "proposed" else ""))
        if r["status"] == "proposed":
            rows.append([btn(f"✅ {human(r['day'])}", f"dt:ok:{r['id']}"), btn("✕", f"dt:no:{r['id']}")])
        elif r["status"] == "accepted":
            rows.append([btn(f"⚡️ Собрать сейчас · {human(r['day'])}", f"dt:now:{r['id']}"),
                         btn("✕", f"dt:no:{r['id']}")])
    if not rows_d:
        lines.append("\nВпереди поводов нет. «🔄 Найти» — поищу сейчас.")
    lines.append(f"\nСвоя дата: <code>/date 9.03 Луис Барраган, дом-студия</code>. Пост собирается за {LEAD} дн. "
                 "и встаёт в большой слот этого дня.")
    rows.append([btn("🔄 Найти поводы", "dt:new"), btn("← Пульт", "h:home")])
    return screen.banner(), "\n".join(lines)[:1020], screen._kb(rows), arg


@router.callback_query(F.data.startswith("dt:"))
async def on_cb(cb: CallbackQuery, bot: Bot):
    p = cb.data.split(":")
    a = p[1]
    if a == "go":
        await cb.answer()
        await ui.drop(bot, cb.message.message_id)
        return await screen.move_down(bot, "dates")
    await screen.adopt(cb.message)
    if a == "home":
        await cb.answer()
        return await screen.show(bot, "dates")
    if a == "new":
        await cb.answer("Ищу поводы — до пары минут…")
        from app import curator
        try:
            n = await propose()
        except Exception as exc:
            return await screen.show(bot, "dates", note=curator.explain(exc)[:200])
        return await screen.show(bot, "dates", note=f"Новых поводов: {n}" if n else "Новых поводов не нашлось")
    did = int(p[2])
    if a == "ok":
        await _set(did, status="accepted")
        await cb.answer(f"Взято — соберу за {LEAD} дн. до даты")
        r = next((x for x in await items() if x["id"] == did), None)
        if r and r["day"] <= (slots._now().date() + timedelta(days=LEAD)).isoformat():
            asyncio.create_task(build_due(bot))
    elif a == "no":
        await _set(did, status="skipped")
        await cb.answer("Пропускаю")
    elif a == "now":
        await cb.answer("Собираю — до пары минут…")
        from app import curator
        try:
            pid, note = await build(did)
        except Exception as exc:
            return await screen.show(bot, "dates", note=curator.explain(exc)[:200])
        if pid:
            return await screen.show(bot, "list", mode="sched", pid=pid, note=f"📅 {note}"[:200])
        return await screen.show(bot, "dates", note=f"Не собрался: {note}")
    return await screen.show(bot, "dates")


DATE_RE = re.compile(r"^\s*(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\s+(.+)$", re.S)


@router.message(Command("date"))
async def cmd_date(msg: Message, command: CommandObject, bot: Bot):
    m = DATE_RE.match(command.args or "")
    await ui.drop(bot, msg.message_id)
    if not m:
        return await screen.notify(bot, "Формат: <code>/date 9.03 Луис Барраган, дом-студия</code> — дата и о чём пост.")
    d, mo, y, text = int(m.group(1)), int(m.group(2)), m.group(3), m.group(4).strip()
    today = slots._now().date()
    try:
        day = date(int(y) + (2000 if y and len(y) == 2 else 0) if y else today.year, mo, d)
        if not y and day < today:
            day = day.replace(year=today.year + 1)
    except ValueError:
        return await screen.notify(bot, "Такой даты нет — проверь число и месяц.")
    await _add(day.isoformat(), text, text, status="accepted")
    await screen.move_down(bot, "dates", note=f"Добавил: {human(day.isoformat())} — {text[:60]}")
    if day <= today + timedelta(days=LEAD):
        asyncio.create_task(build_due(bot))
