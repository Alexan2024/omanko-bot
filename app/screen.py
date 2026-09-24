"""Экран бота — одно закреплённое сообщение (картинка + подпись + кнопки), которое перерисовывается на месте:
пульт, входящие, запас, слот, план, статистика, источники, голос. Уведомления приходят отдельными
сообщениями; их кнопка «Открыть» переносит экран вниз и сама исчезает."""
import asyncio
import html
import json
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Message
from PIL import Image, ImageDraw, ImageFont

from app import cards, config, db, formatter, niche, reports, slots, voice

log = logging.getLogger(__name__)

LOCK = asyncio.Lock()
BANNER = config.DATA_DIR / "ui" / "screen_v3.jpg"
_fid: dict[str, str] = {}          # путь к картинке → file_id в Telegram, чтобы не загружать повторно
_pending: asyncio.Task | None = None

REJECT_REASONS = {
    "taste": "не мой вкус",
    "photo": "слабые фото",
    "dup": "уже было",
    "topic": "не та тема",
    "text": "плохой текст",
}
LEGEND = "<i>✅ вышел · 🟡 одобрен · 📥 ждёт решения · 🤖 автопост · ⚪️ пусто</i>"


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _kb(rows: list[list]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[r for r in rows if r])


def banner() -> str:
    """Заставка экрана: тёплый серый, тонкая линия, название канала."""
    if not BANNER.exists():
        BANNER.parent.mkdir(parents=True, exist_ok=True)
        im = Image.new("RGB", (1280, 360), (236, 233, 226))
        d = ImageDraw.Draw(im)
        d.line([(80, 280), (1200, 280)], fill=(28, 28, 28), width=2)
        try:
            font = ImageFont.load_default(size=44)
        except TypeError:
            font = ImageFont.load_default()
        d.text((80, 214), "AHMAG", fill=(28, 28, 28), font=font)
        im.save(BANNER, "JPEG", quality=90)
    return str(BANNER)


def _input(photo: str):
    return _fid.get(photo) or FSInputFile(photo)


def _learn(photo: str, msg) -> None:
    if isinstance(msg, Message) and msg.photo:
        _fid[photo] = msg.photo[-1].file_id


async def _st() -> dict:
    return await db.get_setting("screen", {}) or {}


async def _put(st: dict) -> None:
    await db.set_setting("screen", st)


# ======================= отрисовка =======================

def _slot_line(s: dict) -> str:
    line = f"{slots.ICON[s['state']]} {s['dt']:%H:%M} {cards.SHORT[s['fmt']]}"
    post, state = s["post"], s["state"]
    if state in ("published", "approved"):
        return f"{line} · {html.escape(slots.headline(post, 30))}"
    if state == "announced":
        return f"{line} · автопост: {html.escape(slots.headline(post, 24))}"
    if state == "offered":
        return f"{line} · ждёт решения"
    return f"{line} · " + {"skipped": "пропуск", "missed": "прошёл пустым"}.get(state, "пусто")


def _stock_totals(stock: dict) -> tuple[int, int, int]:
    mini = sum(v.get("mini", 0) for v in stock.values())
    std = sum(v.get("std", 0) for v in stock.values())
    return mini + std, mini, std


async def _home(arg: dict):
    day = int(arg.get("day", 0))
    md, ps = await slots.mode(), await slots.paused()
    state, other = await slots.day_state(day), await slots.day_state(1 - day)
    inbox = await db.inbox_posts()
    sched = await db.scheduled_posts(slots.key_of(slots._now()))
    stock = await db.stock_counts()
    total, mini, std = _stock_totals(stock)
    cc = await db.candidate_counts()
    lines = [f"<b>AHMAG</b> · {slots.MODES[md]}" + (" · ⏸ <b>пауза</b>" if ps else "")]
    err = await db.get_setting("api_error")
    if err:
        lines.append(f"⚠️ {html.escape(err['text'][:300])}")
    title = "Сегодня" if day == 0 else "Завтра"
    lines += ["", f"<b>{title}, {state[0]['dt']:%d.%m}</b>" if state else f"<b>{title}</b>"]
    lines += [_slot_line(s) for s in state]
    lines += [("Завтра: " if day == 0 else "Сегодня: ") + "".join(slots.ICON[x["state"]] for x in other), ""]
    lines.append(f"📥 Ждут решения: <b>{len(inbox)}</b>")
    lines.append(f"🗓 Стоят в слотах: {len(sched)}")
    lines.append(f"📦 В запасе: {total} · мини {mini} · больших {std}")
    if total:
        lines.append("      " + " · ".join(f"{cards.cat_label(c)} {sum(stock[c].values())}"
                                           for c in config.CATEGORIES if c in stock))
    waiting = cc.get("new", 0) + cc.get("triaged", 0)
    lines.append(f"🔎 Найдено, ждёт оценки: {waiting}" + (f" · на оценке: {cc['batched']}" if cc.get("batched") else ""))
    lines.append(f"💵 Сегодня {reports.money(await db.cost_today())} · за 7 дней {reports.money(await db.cost_days(7))}")
    lines += ["", LEGEND]

    slot_btns = [btn(f"{x['dt']:%H:%M} {slots.ICON[x['state']]}", f"h:slot:{slots.enc(x['key'])}") for x in state]

    def mode_btn(key: str, label: str):
        return btn(("● " if md == key else "") + label, f"h:mode:{key}")

    rows = [slot_btns[i:i + 4] for i in range(0, len(slot_btns), 4)]
    rows += [
        [btn("Завтра ▸" if day == 0 else "◂ Сегодня", f"h:day:{1 - day}"), btn("🔄 Обновить", f"h:day:{day}")],
        [btn(f"📥 Входящие · {len(inbox)}", "h:inbox"), btn(f"📦 Запас · {total}", "h:stock")],
        [btn("▶️ Следующий пост", "h:next:mini"), btn("🗓 План", "h:plan")],
        [mode_btn("manual", "✋ Ручной"), mode_btn("semi", "🤝 Полуавто"), mode_btn("auto", "🤖 Авто")],
        [btn("📝 #ahmagnotes", "h:notes"), btn("🔎 Собрать сейчас", "h:collect")],
        [btn("📊 Статистика", "h:stats"), btn("📡 Источники", "h:src")],
    ]
    rows += await _home_extras()
    rows.append([btn("✍️ Голос", "h:voice"), btn("▶️ Снять с паузы" if ps else "⏸ Пауза", "h:pause")])
    return banner(), "\n".join(lines), _kb(rows), arg


async def _home_extras() -> list[list]:
    """Разделы, у которых своя логика в отдельных модулях: запрос и отложенные, очередь, находки, рост, Instagram.
    Модули импортируются здесь, а не наверху: они сами пользуются экраном."""
    from app import finds, instagram, request
    rows = []
    try:
        n = len(await request.wishlist())
        rows.append([btn("✍️ Пост по запросу", "rq:ask"), btn(f"🕓 Отложенные · {n}" if n else "🕓 Отложенные", "rq:wl")])
    except Exception:
        log.warning("Кнопка «Пост по запросу»", exc_info=True)
    try:
        from app import dates
        n = len(await dates.items("proposed"))
        rows.append([btn("🗂 Очередь публикаций", "qv:show"), btn(f"📅 Даты · {n} новых" if n else "📅 Даты", "dt:home")])
    except Exception:
        log.warning("Кнопка «Даты»", exc_info=True)
        rows.append([btn("🗂 Очередь публикаций", "qv:show")])
    try:
        from app import reels
        rows.append([btn(await reels.home_label(), "rl:home")])
    except Exception:
        log.warning("Кнопка «Рилсы»", exc_info=True)
    try:
        rows.append([btn(await finds.home_label(), "fa:info")])
    except Exception:
        log.warning("Кнопка «Находки»", exc_info=True)
    try:
        ig = f"📸 Instagram {await instagram.status_icon()}"
    except Exception:
        log.warning("Кнопка «Instagram»", exc_info=True)
        ig = "📸 Instagram"
    rows.append([btn("📈 Рост", "g:home"), btn(ig, "ig:home")])
    return rows


async def _slot(arg: dict):
    key = arg["key"]
    offset = (slots.key_dt(key).date() - slots._now().date()).days
    state = next((x for x in await slots.day_state(offset) if x["key"] == key), None)
    back = [btn("← Пульт", f"h:day:{min(max(offset, 0), 1)}")]
    if not state:
        return banner(), "Такого слота в расписании нет.", _kb([back]), arg
    post, st, fmt, code = state["post"], state["state"], state["fmt"], slots.enc(key)
    text = f"<b>Слот {slots.human_key(key)}</b> · {cards.FORMAT_LABEL[fmt]}\n{slots.ICON[st]} "
    rows = []
    if st == "published":
        text += f"Вышел: {html.escape(slots.headline(post, 90))}"
        link = cards.post_link(post)
        rows.append([btn("👁 Открыть", f"h:open:{post['id']}:one"), btn("📸 Для Instagram", f"v:ig:{post['id']}")])
        if link:
            rows.append([InlineKeyboardButton(text="🔗 В канале", url=link)])
    elif st == "approved":
        text += f"Стоит: {html.escape(slots.headline(post, 90))}"
        rows.append([btn("👁 Открыть", f"h:open:{post['id']}:sched"), btn("↩️ Освободить", f"h:sf:{post['id']}:{code}")])
    elif st == "announced":
        text += f"Автопост: {html.escape(slots.headline(post, 90))}\nВыйдет сам, если не отменить."
        rows.append([btn("👁 Открыть", f"h:open:{post['id']}:inbox"), btn("🚫 Отменить", f"h:sf:{post['id']}:{code}")])
    elif st == "offered":
        text += f"Предложен и ждёт решения: {html.escape(slots.headline(post, 90))}"
        rows.append([btn("👁 Открыть", f"h:open:{post['id']}:inbox")])
    elif st == "missed":
        text += "Прошёл пустым."
    else:
        text += "Пропуск: бот не будет его заполнять." if st == "skipped" else "Пусто."
        if st == "empty":
            stock = await db.stock_counts()
            total = sum(v.get(fmt, 0) for v in stock.values())
            text += f"\n\nПредложить пост — выбери рубрику (в запасе {cards.FORMAT_LABEL[fmt]}):"
            rows.append([btn(f"Любая · {total}", f"h:so:{code}:any")])
            cats = [btn(f"{config.CATEGORIES[c]} · {stock.get(c, {}).get(fmt, 0)}", f"h:so:{code}:{c}")
                    for c in config.CATEGORIES]
            rows += [cats[i:i + 2] for i in range(0, len(cats), 2)]
        rows.append([btn("↩️ Вернуть слот" if st == "skipped" else "⏭ Пропустить слот", f"h:sk:{code}")])
    rows.append(back)
    photo = (cards.cover_path(post) if post else None) or banner()
    return photo, text, _kb(rows), arg


async def _next(arg: dict):
    fmt = arg.get("fmt", "mini")
    stock = await db.stock_counts()
    total = sum(v.get(fmt, 0) for v in stock.values())
    text = ("<b>▶️ Какой пост показать?</b>\nПоложу его во входящие. Цифры — сколько в запасе этого формата."
            + ("\n\nНужного формата нет — возьму другой и переделаю." if not total else ""))
    rows = [[btn(("● " if fmt == "mini" else "") + "▫️ Мини", "h:next:mini"),
             btn(("● " if fmt == "std" else "") + "◻️ Большой", "h:next:std")],
            [btn(f"Любая рубрика · {total}", f"h:nx:{fmt}:any")]]
    cats = [btn(f"{config.CATEGORIES[c]} · {stock.get(c, {}).get(fmt, 0)}", f"h:nx:{fmt}:{c}")
            for c in config.CATEGORIES]
    rows += [cats[i:i + 2] for i in range(0, len(cats), 2)]
    rows.append([btn("← Пульт", "h:home")])
    return banner(), text, _kb(rows), arg


async def _stockmenu(arg: dict):
    stock = await db.stock_counts()
    total, mini, std = _stock_totals(stock)
    lines = ["<b>📦 Запас</b> — оценено и готово, но тебе ещё не показано", ""]
    rows, cats = [], []
    for c, label in config.CATEGORIES.items():
        v = stock.get(c, {})
        n = sum(v.values())
        parts = ([f"мини {v['mini']}"] if v.get("mini") else []) + ([f"больших {v['std']}"] if v.get("std") else [])
        lines.append(f"{label}: {n}" + (f" ({' · '.join(parts)})" if parts else ""))
        if n:
            cats.append(btn(f"{label} · {n}", f"h:stk:{c}"))
    lines += ["", f"Всего {total}: мини {mini} · больших {std}. Цель — запас на {config.STOCK_DAYS:g} дня слотов."]
    if not total:
        lines.append("Запас пуст — нажми «🔎 Собрать сейчас» на пульте.")
    rows += [cats[i:i + 2] for i in range(0, len(cats), 2)]
    rows.append([btn("← Пульт", "h:home")])
    return banner(), "\n".join(lines), _kb(rows), arg


async def _plan(arg: dict):
    now = slots._now()
    free = [sum(1 for s in await slots.day_state(d) if s["state"] == "empty" and s["dt"] > now) for d in (0, 1)]
    text = ("<b>🗓 План</b>\nНа каждый свободный слот бот предложит пост нужного формата, большим постам сразу "
            "напишет текст. Одобряешь во входящих.\n\n"
            f"Свободно сегодня: {free[0]} · завтра: {free[1]}")
    if await slots.mode() == "semi":
        text += f"\nВ полуавтомате план на завтра собирается сам в {config.PLAN_TIME[0]:02d}:{config.PLAN_TIME[1]:02d}."
    rows = [[btn(f"На сегодня · {free[0]}", "h:plan:0"), btn(f"На завтра · {free[1]}", "h:plan:1")],
            [btn("← Пульт", "h:home")]]
    return banner(), text, _kb(rows), arg


async def _stats(arg: dict):
    return banner(), await reports.stats_text(), _kb([[btn("🏆 Что заходит", "h:perf"), btn("📈 Итоги недели", "h:digest")],
                                                       [btn("← Пульт", "h:home")]]), arg


async def _digest(arg: dict):
    return banner(), await reports.digest_text(7), _kb([[btn("← Статистика", "h:stats"), btn("← Пульт", "h:home")]]), arg


async def _sources(arg: dict):
    rows_data = await reports.sources_rows()
    lines = ["<b>📡 Источники</b> — доля одобренных тобой"]
    toggles = []
    for name, on, rate in rows_data:
        lines.append(f"{'🟢' if on else '⚪️'} {name}: {rate}")
        toggles.append(btn(f"{'🟢' if on else '⚪️'} {name}", f"h:srct:{name}"))
    lines.append("\n<i>Нажми на источник, чтобы выключить или включить.</i>")
    rows = [toggles[i:i + 3] for i in range(0, len(toggles), 3)]
    rows.append([btn("➕ Добавить RSS", "h:srcadd"), btn("← Пульт", "h:home")])
    return banner(), "\n".join(lines)[:1000], _kb(rows), arg


async def _voice(arg: dict):
    banned = await voice.banned()
    n_edits = await db.edits_count()
    lines = ["<b>✍️ Голос</b>",
             "Бот избегает штампов: «не X, а Y», афоризма в конце, «выверенный», «гармония», «диалог с контекстом» "
             "и других. Тексты с остатками штампов помечает ⚠️.",
             f"На твоих правках через «✏️ Свой текст» он учится: сохранено {n_edits}, в работе последние 5.", ""]
    if banned:
        lines.append("<b>Запрещено тобой:</b>")
        lines += [f"{i + 1}. {html.escape(b)}" for i, b in enumerate(banned)]
    else:
        lines.append("Своих запретов пока нет. Добавь фразу, которая режет глаз, — бот перестанет её писать.")
    dels = [btn(f"✕ {i + 1}", f"h:vdel:{i}") for i in range(len(banned))]
    rows = [dels[i:i + 5] for i in range(0, len(dels), 5)]
    rows.append([btn("➕ Запретить фразу", "h:vadd"), btn("🧠 Вкус", "h:taste")])
    rows.append([btn("← Пульт", "h:home")])
    return banner(), "\n".join(lines)[:1000], _kb(rows), arg


# ---------- просмотр постов (входящие, слоты, запас, один пост) ----------

MODE_HEAD = {"inbox": "📥 Входящие", "sched": "🗓 В слотах", "stock": "📦 Запас", "one": ""}


async def _list_ids(mode: str, cat: str | None, pid: int | None) -> list[int]:
    if mode == "inbox":
        return [p["id"] for p in await db.inbox_posts()]
    if mode == "sched":
        return [p["id"] for p in await db.scheduled_posts(slots.key_of(slots._now()))]
    if mode == "stock":
        return [p["id"] for p in await db.ready_posts(None, cat)]
    return [pid] if pid and await db.get_post(pid) else []


def _status_line(post) -> str:
    st, key = post["status"], post["slot_key"]
    if st == "sent":
        return f"предложен на {slots.human_key(key)}" if key else "ждёт решения, без слота"
    if st == "approved":
        return f"🟡 выйдет {slots.human_key(key)}" if key else "🟡 одобрен, ждёт слота"
    if st == "announced":
        return f"🤖 выйдет сам {slots.human_key(key)}" if key else "🤖 автопост"
    if st == "ready":
        return "в запасе" + (f", предлагался {post['offers']} раз" if post["offers"] else ", ещё не показывался")
    if st == "published":
        return "✅ вышел в канале"
    return f"❌ снят: {post['reject_reason'] or ''}"


def _post_caption(post, mode: str, idx: int, n: int, note: str | None) -> tuple[str, bool]:
    data = json.loads(post["data"])
    fmt = post["format"]
    head = MODE_HEAD.get(mode, "")
    pos = f" {idx + 1}/{n}" if n > 1 else ""
    first = (f"<b>{head}{pos}</b> · " if head else "") + _status_line(post)
    info = f"{cards.FORMAT_LABEL.get(fmt, fmt)} · {cards.cat_label(post['category'])}"
    if fmt != "notes":
        info += f" · {post['score']}/10"
    info += f" · {html.escape(post['source'] or '')}"
    if post["url"]:
        info += f' · <a href="{html.escape(post["url"])}">источник</a>'
    must = [first, info]
    if note:
        must.append(f"<b>{html.escape(note)}</b>")
    if fmt == "std" and not formatter.has_body(data):
        must.append("✍️ <i>Текст ещё не написан — напишу, когда выберешь</i>")
    optional = []
    if post["reason"]:
        optional.append(f"<i>{html.escape(post['reason'][:200])}</i>")
    flags = [str(f) for f in (data.get("flags") or [])]
    if flags:
        optional.append("⚠️ " + html.escape("; ".join(flags))[:250])
    sep = "\n┈┈┈┈┈┈┈┈\n"
    body = post["caption"]
    for k in range(len(optional), -1, -1):
        meta = "\n".join(must + optional[:k])
        if formatter.visible_len(meta + sep + body) <= config.CAPTION_LIMIT:
            return meta + sep + body, False
    meta = "\n".join(must)
    budget = config.CAPTION_LIMIT - formatter.visible_len(meta + sep) - 20
    clipped, cut = formatter.clip_blocks(body, budget)
    return meta + sep + clipped + ("\n<i>…дальше — «📄 Весь текст»</i>" if cut else ""), cut


async def _post_kb(post, mode: str, idx: int, n: int, clipped: bool, sub: str | None) -> InlineKeyboardMarkup:
    pid, st, fmt = post["id"], post["status"], post["format"]
    images = json.loads(post["images"])
    if sub in ("photos", "cover"):
        data = json.loads(post["data"])
        excluded, cover = set(data.get("_excluded") or []), data.get("_cover")
        nums = [btn(f"{i + 1}" + ("★" if i == cover else "") + (" ✕" if i in excluded else ""),
                    f"v:{'pcs' if sub == 'cover' else 'px'}:{pid}:{i}") for i in range(len(images))]
        rows = [nums[i:i + 5] for i in range(0, len(nums), 5)]
        if sub == "photos" and st in ("ready", "sent", "approved", "announced"):
            rows.append([btn("🔄 Ещё кадры", f"rq:more:{pid}")])
        rows.append([btn("← К фото", f"v:ph:{pid}")] if sub == "cover"
                    else [btn("⭐ Обложка", f"v:pc:{pid}"), btn("Готово", f"v:back:{pid}")])
        return _kb(rows)
    if sub == "reject":
        rows = [[btn(v, f"v:rr:{pid}:{k}")] for k, v in REJECT_REASONS.items()]
        return _kb(rows + [[btn("← Назад", f"v:back:{pid}")]])
    if sub == "confirm":
        return _kb([[btn("🚀 Да, опубликовать сейчас", f"v:nowok:{pid}")], [btn("← Назад", f"v:back:{pid}")]])
    if sub == "pick":
        rows = []
        for key, f in await slots.free_slots(None, limit=8):
            mark = "" if f == fmt else " ≠"
            rows.append([btn(f"{slots.human_key(key)} · {cards.SHORT[f]}{mark}", f"v:ps:{pid}:{slots.enc(key)}")])
        if st == "approved":
            for other in await db.scheduled_posts(slots.key_of(slots._now())):
                if other["id"] != pid:
                    rows.append([btn(f"⇄ {slots.human_key(other['slot_key'])} · {slots.headline(other, 22)}",
                                     f"v:ps:{pid}:{slots.enc(other['slot_key'])}")])
        return _kb(await _bump_rows(post) + rows + [[btn("← Назад", f"v:back:{pid}")]])

    rows = []
    if post["source"] in niche.FINDS:        # метка видна только в боте, в канале её нет
        rows.append([btn(f"🔍 Находка · {niche.LABELS.get(post['source'], post['source'])}", "v:noop")])
    needs_text = fmt == "std" and not formatter.has_body(json.loads(post["data"]))
    if st == "sent":
        if needs_text:
            rows.append([btn("✍️ Написать текст", f"v:write:{pid}")])
        key = post["slot_key"]
        label = f"✅ В слот {slots.human_key(key)}" if key and await slots.is_free(key) else "⏱ В ближайший слот"
        rows.append([btn(label, f"v:slot:{pid}")])
        rows.append([btn("🗓 Другой слот", f"v:pick:{pid}")] + ([btn("🔄 Заменить", f"v:swap:{pid}")] if key else []))
    elif st == "approved":
        rows.append([btn("🔀 Перенести", f"v:pick:{pid}"), btn("↩️ Убрать из слота", f"v:unslot:{pid}")])
    elif st == "announced":
        rows.append([btn("✅ Оставить", f"v:keep:{pid}"), btn("🚫 Отменить автопост", f"v:unslot:{pid}")])
    elif st == "ready":
        rows.append([btn("📥 Во входящие", f"v:toin:{pid}"), btn("⏱ В ближайший слот", f"v:slot:{pid}")])
        rows.append([btn("🗓 Выбрать слот", f"v:pick:{pid}")])
    elif st == "published":
        link = cards.post_link(post)
        rows.append([btn("📸 Пакет для Instagram", f"v:ig:{pid}"), btn("📖 Фон для сторис", f"v:sbg:{pid}")])
        rows.append([]
                    + ([InlineKeyboardButton(text="🔗 В канале", url=link)] if link else []))
    elif st in ("rejected", "auto_rejected"):
        rows.append([btn("↩️ Вернуть в запас", f"v:restore:{pid}")])
    if st in ("sent", "approved", "announced", "ready"):
        rows.append([btn("🚀 Опубликовать сейчас", f"v:now:{pid}")])
        rows.append([btn("✏️ Свой текст", f"v:edit:{pid}"), btn("🔁 Переписать", f"v:rw:{pid}"),
                     btn("🚫 Фраза", f"v:ban:{pid}")])
        extra = []
        if len(images) > 1:
            extra.append(btn(f"🖼 Фото · {len(images)}", f"v:ph:{pid}"))
        if fmt != "notes":
            extra.append(btn("↔️ В большой" if fmt == "mini" else "↔️ В мини", f"v:fm:{pid}"))
        if clipped:
            extra.append(btn("📄 Весь текст", f"v:txt:{pid}"))
        rows.append(extra)
        rows.append([btn("🗑 Убрать из запаса" if st == "ready" else "❌ Отклонить", f"v:rej:{pid}")])
    if n > 1:
        rows.append([btn("◀", f"v:nav:{pid}:-1"), btn(f"{idx + 1} / {n}", "v:noop"), btn("▶", f"v:nav:{pid}:1")])
    rows.append(([btn("← Запас", "h:stock")] if mode == "stock" else []) + [btn("🏠 Пульт", "h:home")])
    return _kb(rows)


async def _bump_rows(post) -> list[list]:
    """Выбор слота: первой строкой ⚡️ ближайший слот, даже занятый; для поста без слота — ещё и занятые слоты
    (у одобренного они уже есть строками «⇄ поменять»). Занявший слот пост сдвигается, см. slots.place."""
    pid, st = post["id"], post["status"]
    if st not in ("ready", "sent", "approved"):
        return []
    rows = []
    key = await slots.nearest_key()
    if key and not (st == "approved" and post["slot_key"] == key):
        rows.append([btn(f"⚡️ {slots.human_key(key)} · даже если занят", f"rq:bump:{pid}")])
    if st in ("ready", "sent"):
        busy = [c for c in await slots.slot_choices() if c["post"] and c["key"] != key]
        rows += [[btn(slots.slot_label(c, short=True), f"rq:put:{pid}:{slots.enc(c['key'])}")] for c in busy[:6]]
    return rows


async def _list(arg: dict):
    mode, cat = arg.get("mode", "inbox"), arg.get("cat")
    ids = await _list_ids(mode, cat, arg.get("pid"))
    if not ids:
        arg.update(pid=None, idx=0, kb=None)
        if mode == "inbox":
            text = ("<b>📥 Входящие пусты</b> — всё разобрано.\n\n"
                    "Следующий пост можно взять из запаса или собрать план на свободные слоты.")
            rows = [[btn("▶️ Следующий пост", "h:next:mini"), btn("🗓 План", "h:plan")], [btn("🏠 Пульт", "h:home")]]
        elif mode == "stock":
            text = f"<b>📦 {config.CATEGORIES.get(cat, 'Запас')}</b>: в запасе пусто."
            rows = [[btn("← Запас", "h:stock"), btn("🏠 Пульт", "h:home")]]
        else:
            text = "<b>🗓 В слотах пока ничего.</b>" if mode == "sched" else "Пост не найден."
            rows = [[btn("🏠 Пульт", "h:home")]]
        return banner(), text, _kb(rows), arg
    pid = arg.get("pid")
    if pid in ids:
        idx = ids.index(pid)
    else:
        idx = min(int(arg.get("idx") or 0), len(ids) - 1)
        pid = ids[idx]
        arg["kb"] = None
    post = await db.get_post(pid)
    caption, clipped = _post_caption(post, mode, idx, len(ids), arg.get("note"))
    kb = await _post_kb(post, mode, idx, len(ids), clipped, arg.get("kb"))
    arg.update(pid=pid, idx=idx)
    return cards.cover_path(post) or banner(), caption, kb, arg


VIEWS = {"home": _home, "slot": _slot, "next": _next, "stockmenu": _stockmenu, "plan": _plan,
         "stats": _stats, "digest": _digest, "src": _sources, "voice": _voice, "list": _list}


# ======================= управление сообщением экрана =======================

async def _deliver(bot: Bot, st: dict, photo: str, caption: str, kb, new_message: bool) -> None:
    """Правит экран на месте или присылает новый. Сам справляется со «старым» file_id и ошибкой разметки."""
    for attempt in range(3):
        try:
            msg_id = st.get("msg")
            if msg_id and not new_message:
                if photo == st.get("photo"):
                    await bot.edit_message_caption(chat_id=config.ADMIN_ID, message_id=msg_id,
                                                   caption=caption, reply_markup=kb)
                else:
                    res = await bot.edit_message_media(
                        chat_id=config.ADMIN_ID, message_id=msg_id, reply_markup=kb,
                        media=InputMediaPhoto(media=_input(photo), caption=caption, parse_mode="HTML"))
                    _learn(photo, res)
                return
            m = await bot.send_photo(config.ADMIN_ID, _input(photo), caption=caption, reply_markup=kb)
            _learn(photo, m)
            await _pin(bot, m.message_id, st.get("msg"))
            st["msg"] = m.message_id
            return
        except TelegramBadRequest as exc:
            s = str(exc).lower()
            if "not modified" in s:
                return
            if "parse" in s or "entit" in s:
                caption = html.escape(formatter.plain_text(caption))[:1000]
            elif "file" in s and photo in _fid:
                _fid.pop(photo, None)
            elif not new_message:
                new_message = True    # старое сообщение удалено или его нельзя править — присылаем экран заново
            else:
                raise


async def _pin(bot: Bot, new_id: int, old_id: int | None) -> None:
    try:
        await bot.pin_chat_message(config.ADMIN_ID, new_id, disable_notification=True)
    except Exception:
        log.warning("Не закрепил экран", exc_info=True)
    if old_id and old_id != new_id:
        try:
            await bot.delete_message(config.ADMIN_ID, old_id)
        except Exception:
            try:
                await bot.unpin_chat_message(config.ADMIN_ID, message_id=old_id)
                await bot.edit_message_caption(chat_id=config.ADMIN_ID, message_id=old_id,
                                               caption="Экран переехал вниз ↓", reply_markup=None)
            except Exception:
                pass


async def _clear_temp(bot: Bot, st: dict) -> None:
    for mid in st.get("temp") or []:
        try:
            await bot.delete_message(config.ADMIN_ID, mid)
        except Exception:
            pass
    st["temp"] = []


async def _show(bot: Bot, view: str, arg: dict, new_message: bool = False) -> None:
    st = await _st()
    if arg.get("kb") not in ("photos", "cover"):
        await _clear_temp(bot, st)
    photo, caption, kb, arg = await VIEWS.get(view, _home)(dict(arg))
    await _deliver(bot, st, photo, caption, kb, new_message)
    arg.pop("note", None)
    st.update(view=view, arg=arg, photo=photo)
    await _put(st)


async def show(bot: Bot, view: str = "home", **arg) -> None:
    """Перерисовать экран на месте."""
    async with LOCK:
        await _show(bot, view, arg)


async def move_down(bot: Bot, view: str = "home", **arg) -> None:
    """Прислать экран заново внизу чата, закрепить, старый убрать."""
    async with LOCK:
        await _show(bot, view, arg, new_message=True)


async def current() -> tuple[str, dict]:
    st = await _st()
    return st.get("view", "home"), dict(st.get("arg") or {})


async def adopt(message) -> None:
    """Нажали кнопку на другом экранном сообщении (например, старом) — теперь экран — оно."""
    if not message or not getattr(message, "photo", None):
        return
    st = await _st()
    if st.get("msg") != message.message_id:
        st.update(msg=message.message_id, photo=None)
        await _put(st)


async def add_temp(ids: list[int]) -> None:
    st = await _st()
    st["temp"] = (st.get("temp") or []) + list(ids)
    await _put(st)


async def refresh(bot: Bot) -> None:
    """Перерисовка после фоновых событий. Не мешает, пока автор выбирает слот или причину отказа."""
    st = await _st()
    if not st.get("msg"):
        return
    view, arg = st.get("view", "home"), dict(st.get("arg") or {})
    if view not in ("home", "list", "slot", "stockmenu", "plan") or arg.get("kb") in ("pick", "reject", "confirm"):
        return
    async with LOCK:
        try:
            await _show(bot, view, arg)
        except Exception:
            log.warning("Экран не обновился", exc_info=True)


def refresh_soon(bot: Bot, delay: float = 2.0) -> None:
    global _pending
    if _pending and not _pending.done():
        return

    async def later():
        await asyncio.sleep(delay)
        await refresh(bot)

    _pending = asyncio.create_task(later())


async def notify(bot: Bot, text: str, buttons: list[tuple[str, str]] | None = None) -> None:
    """Отдельное сообщение со звуком. Кнопка «Открыть» перенесёт экран вниз, а само сообщение уберёт."""
    kb = _kb([[btn(t, d) for t, d in buttons]]) if buttons else None
    await bot.send_message(config.ADMIN_ID, text, reply_markup=kb, disable_web_page_preview=True)
