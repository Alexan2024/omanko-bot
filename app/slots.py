"""Слоты публикации и режимы.
manual — бот кладёт посты во входящие в часы DELIVERY_HOURS; в слоты уходит только то, что поставил автор.
semi   — вечером бот собирает план на завтра: по посту на каждый слот, автор одобряет или меняет.
         Страховка: если за SLOT_LEAD_MIN минут до слота там ничего не одобрено, бот ставит пост
         с оценкой от AUTO_MIN_SCORE без замечаний и присылает «выйдет сам» с кнопкой отмены.
auto   — бот сам анонсирует пост к слоту и публикует, если автор не отменил.

Слот находки (FIND_SLOT, по умолчанию средний мини-слот) сначала берёт пост из нишевых источников.

Пост привязывается к конкретному слоту: posts.slot_key = 'YYYY-MM-DD HH:MM'.
После публикации ключ остаётся — расписание помнит, чем слот был занят."""
import json
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot

from app import cards, config, db, formatter, pipeline

log = logging.getLogger(__name__)

MODES = {"manual": "✋ ручной", "semi": "🤝 полуавтомат", "auto": "🤖 автомат"}
ICON = {"published": "✅", "approved": "🟡", "announced": "🤖", "offered": "📥",
        "skipped": "⏭", "empty": "⚪️", "missed": "✖️"}


async def mode() -> str:
    m = await db.get_setting("mode", "manual")
    return m if m in MODES else "manual"


async def paused() -> bool:
    return bool(await db.get_setting("paused", False))


# ---------- ключи и время ----------

def _now() -> datetime:
    return datetime.now(ZoneInfo(config.TZ_NAME))


def key_of(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def key_dt(key: str) -> datetime:
    return datetime.strptime(key, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo(config.TZ_NAME))


def enc(key: str) -> str:
    """Ключ для callback_data: только цифры, без двоеточий."""
    return "".join(ch for ch in key if ch.isdigit())


def dec(s: str) -> str:
    return f"{s[:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}"


def slot_fmt(key: str) -> str | None:
    hm = key[-5:]
    return next((f for h, m, f in config.SLOTS if f"{h:02d}:{m:02d}" == hm), None)


def human(dt: datetime) -> str:
    days = (dt.date() - _now().date()).days
    day = {0: "сегодня", 1: "завтра"}.get(days, dt.strftime("%d.%m"))
    return f"{day} в {dt:%H:%M}"


def human_key(key: str) -> str:
    return human(key_dt(key))


def upcoming(fmt: str | None, n: int) -> list[tuple[datetime, str]]:
    """Ближайшие n слотов (нужного формата или любых)."""
    now, out, d = _now(), [], 0
    while len(out) < n and d < 60:
        day = now + timedelta(days=d)
        for h, m, f in config.SLOTS:
            dt = day.replace(hour=h, minute=m, second=0, microsecond=0)
            if dt > now and (fmt is None or f == fmt):
                out.append((dt, f))
        d += 1
    return sorted(out)[:n]


def headline(post, limit: int = 44) -> str:
    h = json.loads(post["data"]).get("headline") or "без заголовка"
    return h if len(h) <= limit else h[: limit - 1] + "…"


# ---------- слот находки ----------

def find_slot() -> tuple[int, int] | None:
    """Мини-слот, который сначала берёт находку. FIND_SLOT=HH:MM, off — выключить; по умолчанию средний мини-слот."""
    raw = os.getenv("FIND_SLOT", "").strip().lower()
    minis = [(h, m) for h, m, f in config.SLOTS if f == "mini"]
    if raw in ("off", "0", "none", "нет"):
        return None
    if raw:
        try:
            h, m = (int(x) for x in raw.split(":"))
            if (h, m) in minis:
                return h, m
            log.warning("FIND_SLOT=%s — такого мини-слота нет, беру средний", raw)
        except ValueError:
            log.warning("FIND_SLOT=%s не разобран, беру средний мини-слот", raw)
    return minis[len(minis) // 2] if minis else None


def is_find_key(key: str) -> bool:
    """Этот слот — слот находки? С FIND_EVERY_DAYS=2 находка идёт через день."""
    fs = find_slot()
    if not fs or key[-5:] != f"{fs[0]:02d}:{fs[1]:02d}":
        return False
    return key_dt(key).date().toordinal() % max(1, config.FIND_EVERY_DAYS) == 0


# ---------- пропуски ----------

async def skipped() -> set[str]:
    return set(await db.get_setting("skipped_slots", []))


async def toggle_skip(key: str) -> bool:
    """→ True, если слот теперь пропускается."""
    sk = {k for k in await skipped() if k >= key_of(_now() - timedelta(days=1))}
    now_skipped = key not in sk
    sk.symmetric_difference_update({key})
    await db.set_setting("skipped_slots", sorted(sk))
    return now_skipped


# ---------- свободные слоты ----------

async def free_slots(fmt: str | None = None, limit: int = 10, horizon: int = 40) -> list[tuple[str, str]]:
    """[(ключ, формат)] — будущие слоты без поста автора и автопоста, не пропущенные."""
    taken = await db.taken_keys(key_of(_now())) | await skipped()
    out = [(key_of(dt), f) for dt, f in upcoming(fmt, horizon) if key_of(dt) not in taken]
    return out[:limit]


async def next_free(fmt: str) -> str | None:
    free = await free_slots(fmt, limit=1)
    return free[0][0] if free else None


async def is_free(key: str) -> bool:
    return key_dt(key) > _now() and key not in await db.taken_keys(key)


async def reschedule() -> int:
    """Одобренные посты, чей слот прошёл, не назначен или больше не существует в расписании, — в ближайшие свободные."""
    moved = 0
    times = {f"{h:02d}:{m:02d}" for h, m, _ in config.SLOTS}
    now_key = key_of(_now())
    orphans = [p for p in await db.approved_posts()
               if p["slot_key"] and p["slot_key"] >= now_key and p["slot_key"][-5:] not in times]
    for p in list(await db.overdue_approved(now_key)) + orphans:
        key = await next_free(p["format"] if p["format"] in ("std", "mini") else "std")
        if not key:
            continue
        await db.update_post(p["id"], slot_key=key)
        moved += 1
    return moved


BUMP_MIN_LEAD = 2          # минут до слота: ближе — берём следующий


# ---------- ближайший слот, даже занятый (пост по запросу, ⚡️ на карточке) ----------

async def nearest_key() -> str | None:
    """Ближайший будущий слот любого формата, кроме пропущенных и тех, до которых меньше пары минут."""
    sk = await skipped()
    edge = _now() + timedelta(minutes=BUMP_MIN_LEAD)
    for dt, _ in upcoming(None, 20):
        key = key_of(dt)
        if dt > edge and key not in sk:
            return key
    return None


async def slot_choices(limit_today_min: int = 3) -> list[dict]:
    """Оставшиеся слоты сегодня (если их меньше трёх — плюс завтрашние): ключ, формат и кто там стоит."""
    sk = await skipped()
    edge = _now() + timedelta(minutes=BUMP_MIN_LEAD)
    today = _now().date()
    out = [{"key": key_of(dt), "fmt": f, "dt": dt} for dt, f in upcoming(None, 20)
           if dt > edge and key_of(dt) not in sk]
    todays = [c for c in out if c["dt"].date() == today]
    if len(todays) >= limit_today_min:
        out = todays
    else:
        out = todays + [c for c in out if c["dt"].date() == today + timedelta(days=1)]
    rows = await db.posts_in_slots([c["key"] for c in out])
    for c in out:
        here = [r for r in rows if r["slot_key"] == c["key"] and r["status"] in ("approved", "announced")]
        c["post"] = here[0] if here else None
    return out


def slot_label(c: dict, short: bool = False) -> str:
    t = f"{c['dt']:%H:%M}" if c["dt"].date() == _now().date() else f"завтра {c['dt']:%H:%M}"
    fmt = "мини" if c["fmt"] == "mini" else "большой"
    if c["post"]:
        return f"⤵ {t} · вместо «{headline(c['post'], 18 if short else 24)}»"
    return f"⚪️ {t} · свободен · {fmt}"


async def place(pid: int, key: str) -> str:
    """Ставит пост в слот key. Занявший его пост сдвигается. → строка для экрана."""
    if key_dt(key) <= _now():
        raise RuntimeError("этот слот уже прошёл — выбери другой")
    moved = []
    for p in await db.posts_in_slots([key]):
        if p["id"] == pid:
            continue
        if p["status"] == "announced":          # автопост к этому слоту — во входящие
            await db.update_post(p["id"], status="sent", slot_key=None, sent_at=db.now())
            moved.append(f"«{headline(p, 30)}» → во входящие")
        elif p["status"] == "approved":
            fmt = p["format"] if p["format"] in ("std", "mini") else "std"
            nk = await next_free(fmt)     # сам key ещё занят этим постом, поэтому next_free его не вернёт
            await db.update_post(p["id"], slot_key=nk)
            moved.append(f"«{headline(p, 30)}» → {human_key(nk) if nk else 'первый свободный слот'}")
    post = await db.get_post(pid)
    if post["format"] == "std" and not formatter.has_body(json.loads(post["data"])):
        await pipeline.ensure_text(pid)
    await db.update_post(pid, status="approved", decided_at=db.now(), slot_key=key)
    note = f"Выйдет {human_key(key)}"
    if await paused():
        note += " (сейчас пауза)"
    if moved:
        note += ". Сдвинут: " + "; ".join(moved)
    return note


async def bump(pid: int) -> str:
    """В ближайший слот, даже занятый."""
    key = await nearest_key()
    if not key:
        raise RuntimeError("впереди нет ни одного слота — проверь SLOTS и пропуски")
    return "⚡️ " + await place(pid, key)


# ---------- состояние дня (для экрана) ----------

async def day_state(offset: int = 0) -> list[dict]:
    now = _now()
    day = now + timedelta(days=offset)
    slots_ = []
    for h, m, f in config.SLOTS:
        dt = day.replace(hour=h, minute=m, second=0, microsecond=0)
        slots_.append({"key": key_of(dt), "dt": dt, "fmt": f, "state": "empty", "post": None, "n": 0})
    rows = await db.posts_in_slots([s["key"] for s in slots_])
    sk = await skipped()
    rank = {"published": 4, "approved": 3, "announced": 2, "sent": 1}
    for s in slots_:
        here = [r for r in rows if r["slot_key"] == s["key"]]
        if here:
            best = max(here, key=lambda r: rank[r["status"]])
            s["post"], s["n"] = best, len(here)
            s["state"] = "offered" if best["status"] == "sent" else best["status"]
        elif s["key"] in sk:
            s["state"] = "skipped"
        elif s["dt"] <= now:
            s["state"] = "missed"
    return slots_


# ---------- предложение поста к слоту ----------

async def propose(pid: int, key: str | None) -> None:
    post = await db.get_post(pid)
    await db.update_post(pid, status="sent", sent_at=db.now(), slot_key=key, offers=(post["offers"] or 0) + 1)


async def build_plan(offset: int) -> tuple[list, int]:
    """План на день: на каждый свободный слот — пост нужного формата. Большим постам сразу пишется текст.
    → (предложенные посты, сколько слотов осталось без поста)"""
    made, missing = [], 0
    for s in await day_state(offset):
        if s["state"] != "empty" or s["dt"] <= _now() + timedelta(minutes=5):
            continue
        post = await pipeline.pick_next(s["fmt"], exclude={p["id"] for p in made}, planned=made,
                                        find_slot=s["fmt"] == "mini" and is_find_key(s["key"]))
        if not post:
            missing += 1
            continue
        await propose(post["id"], s["key"])
        if s["fmt"] == "std":
            try:
                post = await pipeline.ensure_text(post["id"])
            except Exception:
                log.exception("Текст для плана, пост %s", post["id"])  # напишется, когда автор выберет
        made.append(await db.get_post(post["id"]))
    return made, missing


def plan_summary(made: list, missing: int, day_word: str) -> str:
    mini = sum(1 for p in made if p["format"] == "mini")
    if made:
        text = f"🗓 План на {day_word}: {len(made)} (мини {mini}, больших {len(made) - mini})"
    elif missing:
        text = f"🗓 План на {day_word}: постов в запасе не хватило"
    else:
        text = f"🗓 План на {day_word}: свободных слотов нет"
    if missing and made:
        text += f"\nНа {missing} слот(а) постов не хватило — оценка ещё идёт или запас пуст."
    return text


async def on_mode_change(new_mode: str) -> str:
    """Полуавтомат включили — сразу план на остаток дня (и на завтра, если вечер)."""
    if new_mode != "semi" or await paused():
        return ""
    made, missing = await build_plan(0)
    text = plan_summary(made, missing, "сегодня") if (made or missing) else ""
    if _now().hour * 60 + _now().minute >= config.PLAN_TIME[0] * 60 + config.PLAN_TIME[1]:
        m2, x2 = await build_plan(1)
        text = (text + "\n" if text else "") + plan_summary(m2, x2, "завтра")
    return text


# ---------- задачи расписания ----------

async def evening(bot: Bot) -> None:
    """В PLAN_TIME: полуавтомат собирает план на завтра; в любом режиме — напоминание о том, что ждёт решения."""
    from app import screen
    lines = []
    if await mode() == "semi" and not await paused():
        made, missing = await build_plan(1)
        if made or missing:
            lines.append(plan_summary(made, missing, "завтра"))
    pending = len(await db.inbox_posts())
    if not pending and not lines:
        return
    lines.append(f"📥 Ждут твоего решения: {pending}")
    await screen.notify(bot, "\n".join(lines), [("📥 Разобрать", "n:inbox")])
    screen.refresh_soon(bot)


async def prepare(bot: Bot, h: int, m: int, fmt: str) -> None:
    """За SLOT_LEAD_MIN минут до слота. Автомат — анонс поста, который выйдет сам.
    Полуавтомат — страховка: если к слоту ничего не одобрено, ставим пост, который выйдет сам."""
    md = await mode()
    if md not in ("auto", "semi") or await paused():
        return
    if md == "semi" and not config.SEMI_FALLBACK:
        return
    key = key_of((_now() + timedelta(minutes=config.SLOT_LEAD_MIN)).replace(hour=h, minute=m))
    if key in await skipped() or await db.approved_in_slot(key) or await db.announced_posts(key):
        return
    if await db.overdue_approved(key_of(_now()), fmt):
        return  # слот закроет пост, чей собственный слот уже прошёл
    if md == "semi":
        return await _insure(bot, key, h, m, fmt)
    from app import screen
    post = await pipeline.pick_auto(fmt, find_slot=fmt == "mini" and is_find_key(key))
    if post:
        if fmt == "std":
            post = await pipeline.ensure_text(post["id"])
        await propose(post["id"], key)
        await db.update_post(post["id"], status="announced")
        await screen.notify(bot, f"🤖 В {h:02d}:{m:02d} выйдет сам: {headline(post, 80)}",
                            [("👁 Открыть", f"n:open:{post['id']}"), ("🚫 Отменить", f"n:cancel:{post['id']}")])
    else:
        post = await pipeline.pick_next(fmt, find_slot=fmt == "mini" and is_find_key(key))
        if not post:
            return await screen.notify(bot, f"К слоту {h:02d}:{m:02d} в запасе пусто.", [("🏠 Экран", "n:home")])
        await propose(post["id"], key)
        await screen.notify(bot, f"🤖 К слоту {h:02d}:{m:02d} нет поста с оценкой ≥ {config.AUTO_MIN_SCORE} "
                                 "без замечаний — предложил вариант, реши сам.", [("📥 Открыть", f"n:open:{post['id']}")])
    screen.refresh_soon(bot)


async def _insure(bot: Bot, key: str, h: int, m: int, fmt: str) -> None:
    """Страховка полуавтомата. Сначала — пост, который бот уже предлагал на этот слот, если он годится
    выйти без автора (оценка от AUTO_MIN_SCORE, без замечаний, источник не под подозрением);
    иначе — лучший такой же из запаса. Подходящего нет — слот проходит пустым, как раньше."""
    from app import screen
    untrusted = await pipeline.untrusted_sources()
    offered = [p for p in await db.posts_in_slots([key]) if p["status"] == "sent"]
    post = next((p for p in sorted(offered, key=lambda p: -(p["score"] or 0))
                 if p["format"] == fmt and pipeline.auto_ok(p, untrusted)), None)
    if not post:
        post = await pipeline.pick_auto(fmt, find_slot=fmt == "mini" and is_find_key(key))
        if post:
            await propose(post["id"], key)
    if not post:
        if offered:
            await screen.notify(bot, f"🛟 К {h:02d}:{m:02d} ничего не одобрено, а поста без замечаний с оценкой "
                                     f"от {config.AUTO_MIN_SCORE} нет. Одобри сам — иначе слот пройдёт пустым.",
                                [("📥 Разобрать", "n:inbox")])
        return
    if fmt == "std":
        try:
            post = await pipeline.ensure_text(post["id"])
        except Exception:
            log.exception("Страховка: текст для %s", post["id"])
            await db.update_post(post["id"], status="sent", slot_key=key)
            return await screen.notify(bot, f"🛟 К {h:02d}:{m:02d} ничего не одобрено, а текст для замены "
                                            "не написался. Одобри сам — иначе слот пройдёт пустым.",
                                       [("📥 Разобрать", "n:inbox")])
    await db.update_post(post["id"], status="announced", slot_key=key)
    await screen.notify(bot, f"🛟 К {h:02d}:{m:02d} ничего не одобрено — выйдет сам: {headline(post, 80)}",
                        [("👁 Открыть", f"n:open:{post['id']}"), ("🚫 Снять", f"n:cancel:{post['id']}")])
    screen.refresh_soon(bot)


async def fire(bot: Bot, h: int, m: int, fmt: str) -> None:
    from app import screen
    key = key_of(_now().replace(hour=h, minute=m))
    if not await paused():
        how = f"по слоту {h:02d}:{m:02d}"
        post = await db.approved_in_slot(key)
        if not post:
            overdue = await db.overdue_approved(key, fmt)   # ждал прошедшего слота — выходит в первом же свободном
            post = overdue[0] if overdue else None
        if not post and await mode() in ("auto", "semi") and key not in await skipped():
            announced = await db.announced_posts(key)
            label = "автомат" if await mode() == "auto" else "страховка"
            post, how = (announced[0], f"{label}, {h:02d}:{m:02d}") if announced else (None, how)
        if post:
            try:
                await cards.publish_post(bot, post["id"], how, slot_key=key)
            except Exception as exc:
                log.exception("Слот %s: публикация %s", key, post["id"])
                await screen.notify(bot, f"⚠️ Слот {h:02d}:{m:02d}: пост не опубликован — {exc!r}"[:900])
        await reschedule()

    # невыбранные предложения и несостоявшиеся анонсы этого и прошлых слотов — обратно в запас
    for p in await db.slot_leftovers(key):
        if (p["offers"] or 0) >= config.MAX_OFFERS:
            await db.update_post(p["id"], status="auto_rejected", slot_key=None,
                                 reject_reason=f"не выбран {config.MAX_OFFERS} раза")
        else:
            await db.update_post(p["id"], status="ready", slot_key=None)
    screen.refresh_soon(bot)


async def expire_inbox(bot: Bot) -> int:
    """Посты, которые ждут решения дольше INBOX_TTL_HOURS, уходят обратно в запас (или снимаются)."""
    from app import screen
    n = 0
    for p in await db.stale_inbox(config.INBOX_TTL_HOURS, key_of(_now())):
        if (p["offers"] or 0) >= config.MAX_OFFERS:
            await db.update_post(p["id"], status="auto_rejected", slot_key=None,
                                 reject_reason=f"не выбран {config.MAX_OFFERS} раза")
        else:
            await db.update_post(p["id"], status="ready", slot_key=None)
        n += 1
    if n:
        screen.refresh_soon(bot)
    return n


async def deliver_manual(bot: Bot, n: int) -> None:
    """Ручной режим: несколько постов во входящие, 70% мини и 30% больших, не больше DAILY_MAX в день."""
    from app import screen
    if await mode() != "manual" or await paused():
        return
    left = config.DAILY_MAX - len(await db.sent_today())
    added = 0
    for _ in range(max(0, min(n, left))):
        today = await db.inbox_posts()
        minis = sum(1 for p in today if p["format"] == "mini")
        fmt = "mini" if minis < 0.7 * (len(today) + 1) else "std"
        post = await pipeline.pick_next(fmt)
        if not post:
            break
        await propose(post["id"], None)
        if post["format"] == "std":
            try:
                await pipeline.ensure_text(post["id"])
            except Exception:
                log.exception("Текст для входящих %s", post["id"])
        added += 1
    if added:
        await screen.notify(bot, f"📥 Во входящих новых постов: {added}", [("📥 Разобрать", "n:inbox")])
        screen.refresh_soon(bot)


def has_text(post) -> bool:
    return post["format"] != "std" or formatter.has_body(json.loads(post["data"]))
