"""Партнёры для взаимного пиара и совместных папок.
Автор добавляет несколько каналов, близких по духу («семена»). Раз в неделю бот читает их публичные страницы
t.me/s/…, выписывает каналы, которые они упоминают и репостят, открывает эти каналы и отсеивает неподходящие
по размеру, живости и честности просмотров. Тему оставшихся проверяет Claude (Haiku). Лучших по вовлечённости
и близости по размеру бот предлагает автору. Черновик письма пишет Claude, отправляет автор сам:
рассылка от бота или от аккаунта автора — прямой путь к блокировке."""
import asyncio
import os
import html
import json
import logging
import math
import re
import statistics
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import httpx
from aiogram import Bot
from bs4 import BeautifulSoup

from app import config, curator, db, voice

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS g_partners (
    username TEXT PRIMARY KEY,
    title TEXT,
    description TEXT,
    subs INTEGER,
    avg_views INTEGER,
    posts_30d INTEGER,
    last_post TEXT,
    sample TEXT,               -- json: начало последних постов, для проверки темы
    fit INTEGER,               -- 1 подходит, 0 нет, NULL ещё не проверен
    why TEXT,
    contact TEXT,
    status TEXT NOT NULL DEFAULT 'new',   -- new | proposed | contacted | agreed | declined | skip
    seed INTEGER NOT NULL DEFAULT 0,      -- 1 — по его упоминаниям ищем новых
    found_via TEXT,
    mentions INTEGER NOT NULL DEFAULT 0,
    checked_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_gp_status ON g_partners(status);
"""

STATUS = {"new": "найден", "proposed": "предложен", "contacted": "✉️ написал", "agreed": "🤝 договорились",
          "declined": "не вышло", "skip": "не подходит"}
WORK = ("contacted", "agreed")

MIN_SUBS = int(os.getenv("G_PARTNER_MIN_SUBS", "300"))
MIN_ER = 0.04          # просмотров на пост к подписчикам: ниже — похоже на накрутку или мёртвую аудиторию
MIN_POSTS_30D = 4      # канал живой
FETCH_NEW = 40         # сколько новых каналов открывать за один проход
PAUSE = 1.2            # между запросами к t.me, секунд

MENTION = re.compile(r"(?<![\w.])@([A-Za-z][A-Za-z0-9_]{4,31})\b")
LINK = re.compile(r"(?:https?://)?(?:t|telegram)\.me/(?:s/)?([A-Za-z][A-Za-z0-9_]{4,31})(?![A-Za-z0-9_])")
SKIP = {"joinchat", "addlist", "addstickers", "addemoji", "share", "proxy", "socks", "setlanguage",
        "telegram", "telegraph", "iv", "boost", "login", "contact", "durov", "premium", "stickers"}
HEADERS = {"User-Agent": config.USER_AGENT, "Accept-Language": "en"}


async def init() -> None:
    async with db.connect() as c:
        await c.executescript(SCHEMA)
        await c.commit()


# ---------- чтение t.me ----------

def _num(text: str | None) -> int | None:
    """«10.7M», «12.3K», «12 345 subscribers» → число."""
    m = re.search(r"(\d[\d\s.,]*)\s*([KkMm])?", (text or "").replace("\xa0", " "))
    if not m:
        return None
    raw, suf = m.group(1).replace(" ", ""), (m.group(2) or "").upper()
    try:
        if suf:
            return int(float(raw.replace(",", ".")) * (1000 if suf == "K" else 1_000_000))
        return int(re.sub(r"[.,]", "", raw) or 0)
    except ValueError:
        return None


def _names(text: str) -> list[str]:
    return [u.lower() for u in MENTION.findall(text or "") + LINK.findall(text or "")]


async def fetch(client: httpx.AsyncClient, username: str) -> dict | None:
    """Публичная страница канала → {title, description, subs, avg_views, posts_30d, sample, mentions, contact}.
    None — не канал, закрыт или не открылся."""
    username = username.lower()
    try:
        r = await client.get(f"https://t.me/s/{username}", follow_redirects=True, timeout=20)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    soup = BeautifulSoup(r.text, "lxml")
    info = {"username": username, "mentions": [], "sample": [], "avg_views": None, "posts_30d": 0, "last_post": None}
    if soup.select_one(".tgme_channel_info"):
        t = soup.select_one(".tgme_channel_info_header_title")
        d = soup.select_one(".tgme_channel_info_description")
        info["title"] = t.get_text(" ", strip=True) if t else username
        info["description"] = d.get_text(" ", strip=True) if d else ""
        info["subs"] = None
        for c in soup.select(".tgme_channel_info_counter"):
            kind, val = c.select_one(".counter_type"), c.select_one(".counter_value")
            if kind and val and "subscriber" in kind.get_text().lower():
                info["subs"] = _num(val.get_text())
        views, dates = [], []
        for m in soup.select(".tgme_widget_message"):
            v = m.select_one(".tgme_widget_message_views")
            if v and _num(v.get_text()) is not None:
                views.append(_num(v.get_text()))
            tm = m.select_one("time[datetime]")
            if tm:
                try:
                    dates.append(datetime.fromisoformat(tm["datetime"]))
                except ValueError:
                    pass
            tx = m.select_one(".tgme_widget_message_text")
            if tx:
                text = tx.get_text(" ", strip=True)
                info["sample"].append(text[:220])
                info["mentions"] += _names(text)
            for a in m.select("a[href]"):
                info["mentions"] += [u.lower() for u in LINK.findall(a["href"])]
        # у свежих постов просмотры ещё набираются — два последних не считаем
        base = views[:-2] if len(views) > 4 else views
        info["avg_views"] = int(statistics.median(base)) if base else None
        now = datetime.now(timezone.utc)
        info["posts_30d"] = sum(1 for x in dates if now - x <= timedelta(days=30))
        info["last_post"] = max(dates).isoformat() if dates else None
        info["sample"] = info["sample"][-5:]
    else:
        # без веб-превью остаётся только карточка t.me/<имя>: название, описание, подписчики
        try:
            r = await client.get(f"https://t.me/{username}", follow_redirects=True, timeout=20)
        except Exception:
            return None
        soup = BeautifulSoup(r.text, "lxml")
        extra = soup.select_one(".tgme_page_extra")
        if not extra or "subscriber" not in extra.get_text().lower():
            return None   # человек, группа или бот
        t, d = soup.select_one(".tgme_page_title"), soup.select_one(".tgme_page_description")
        info.update(title=t.get_text(" ", strip=True) if t else username,
                    description=d.get_text(" ", strip=True) if d else "", subs=_num(extra.get_text()))
    contacts = [u for u in _names(info["description"]) if u != username]
    info["contact"] = "@" + contacts[0] if contacts else None
    info["mentions"] = [u for u in info["mentions"] if u != username]
    return info


# ---------- база ----------

async def get(username: str):
    async with db.connect() as c:
        cur = await c.execute("SELECT * FROM g_partners WHERE username=?", (username.lower(),))
        return await cur.fetchone()


async def _save(info: dict, **extra) -> None:
    """Вставка или обновление цифр. Статус, семя и оценку темы меняют только явные поля в extra."""
    f = {"title": info.get("title"), "description": (info.get("description") or "")[:600],
         "subs": info.get("subs"), "avg_views": info.get("avg_views"), "posts_30d": info.get("posts_30d"),
         "last_post": info.get("last_post"), "sample": json.dumps(info.get("sample") or [], ensure_ascii=False),
         "contact": info.get("contact"), "checked_at": db.now(), "updated_at": db.now(), **extra}
    cols = ",".join(f)
    upd = ",".join(f"{k}=excluded.{k}" for k in f)
    async with db.connect() as c:
        await c.execute(f"INSERT INTO g_partners(username,{cols}) VALUES (?,{','.join('?' * len(f))}) "
                        f"ON CONFLICT(username) DO UPDATE SET {upd}", (info["username"], *f.values()))
        await c.commit()


async def set_status(username: str, status: str) -> None:
    async with db.connect() as c:
        await c.execute("UPDATE g_partners SET status=?, updated_at=? WHERE username=?",
                        (status, db.now(), username.lower()))
        if status in WORK:   # с кем работаем — по их упоминаниям тоже ищем
            await c.execute("UPDATE g_partners SET seed=1 WHERE username=?", (username.lower(),))
        await c.commit()


async def listing(tab: str) -> list:
    statuses = ("proposed",) if tab == "prop" else WORK
    q = ",".join("?" * len(statuses))
    async with db.connect() as c:
        cur = await c.execute(f"SELECT * FROM g_partners WHERE status IN ({q}) "
                              "ORDER BY status DESC, updated_at DESC", statuses)
        return await cur.fetchall()


async def counts() -> dict[str, int]:
    async with db.connect() as c:
        cur = await c.execute("SELECT status, COUNT(*) n FROM g_partners GROUP BY status")
        out = {r["status"]: r["n"] for r in await cur.fetchall()}
        cur = await c.execute("SELECT COUNT(*) n FROM g_partners WHERE seed=1")
        out["_seeds"] = (await cur.fetchone())["n"]
    return out


# ---------- свой канал ----------

_ours: dict = {}


async def ours(bot: Bot) -> dict:
    """Название, @имя, описание и подписчики AHMAG — кэш на час."""
    if _ours and time.time() - _ours.get("_at", 0) < 3600:
        return _ours
    try:
        chat = await bot.get_chat(config.CHANNEL_ID)
        subs = await bot.get_chat_member_count(config.CHANNEL_ID)
        _ours.update(title=chat.title or "AHMAG", username=(chat.username or "").lower(),
                     description=chat.description or "", subs=subs, _at=time.time())
    except Exception:
        log.warning("Не прочитал данные своего канала", exc_info=True)
        _ours.setdefault("title", "AHMAG")
        _ours.setdefault("username", str(config.CHANNEL_ID).lstrip("@").lower())
        _ours.setdefault("description", "")
        _ours.setdefault("subs", 0)
    return _ours


# ---------- отбор ----------

def _band(our_subs: int) -> tuple[int, int]:
    """Кто согласится на обмен: от трети нашего размера до пятикратного (у маленького канала — до 3000)."""
    return max(MIN_SUBS, int(our_subs * 0.3)), max(3000, our_subs * 5)


def _er(p) -> float | None:
    return p["avg_views"] / p["subs"] if p["subs"] and p["avg_views"] else None


def _screen(p, our_subs: int) -> str | None:
    """Причина отсева по цифрам или None, если цифры в порядке."""
    lo, hi = _band(our_subs)
    if not p["subs"]:
        return "не видно подписчиков"
    if p["subs"] < lo:
        return f"мелкий ({p['subs']})"
    if p["subs"] > hi:
        return f"крупный ({p['subs']})"
    if p["avg_views"] is not None and p["posts_30d"] is not None and p["posts_30d"] < MIN_POSTS_30D:
        return "почти не пишет"
    er = _er(p)
    if er is not None and er < MIN_ER:
        return f"мало просмотров ({er:.0%})"
    return None


def rank(p, our_subs: int) -> float:
    er = _er(p) or 0.08
    close = 1 / (1 + abs(math.log(max(p["subs"] or 1, 1) / max(our_subs, 1))))
    return er * close * (1 + 0.2 * min(p["mentions"] or 0, 5))


JUDGE_SYSTEM = """Ты помогаешь автору Telegram-канала AHMAG найти каналы для взаимного пиара и совместных папок. AHMAG — личный архив автора: построенная архитектура (модернизм, частные дома, сакральное, руины), искусство, документальная и архивная фотография, визуальная культура, авторское кино. Пишет для людей со вкусом.

Для каждого канала реши, пересекается ли его аудитория с AHMAG.
fit = yes: архитектура, интерьеры, урбанистика, дизайн, искусство, фотография, визуальная культура, история, кино, книги — с авторским взглядом.
fit = no: новости, агрегаторы без автора, реклама, магазины, курсы и школы, девелоперы и недвижимость, мемы, политика, крипта, бизнес, личные блоги не по теме.
why — 3–8 слов по-русски, по делу.

Верни ТОЛЬКО JSON: {"r": [{"u": "username", "fit": "yes|no", "why": "..."}]}"""


async def _judge(our_subs: int) -> int:
    """Отсев по цифрам, потом проверка темы у Claude. → сколько новых подошло."""
    async with db.connect() as c:
        cur = await c.execute("SELECT * FROM g_partners WHERE status='new' AND fit IS NULL AND seed=0")
        rows = await cur.fetchall()
    todo = []
    for p in rows:
        why = _screen(p, our_subs)
        if why:
            await _mark(p["username"], 0, why)
        else:
            todo.append(p)
    good = 0
    for i in range(0, len(todo), 20):
        chunk = todo[i:i + 20]
        text = "\n\n".join(
            f"u: {p['username']}\nназвание: {p['title']}\nописание: {(p['description'] or '')[:300]}\n"
            f"последние посты: " + " | ".join(s[:160] for s in json.loads(p["sample"] or "[]"))
            for p in chunk)
        data = await curator._call(text, system=JUDGE_SYSTEM, model=config.TRIAGE_MODEL,
                                   max_tokens=80 * len(chunk) + 200, background=True)
        verdicts = {str(r.get("u", "")).lower(): r for r in data.get("r") or []}
        for p in chunk:
            v = verdicts.get(p["username"])
            if not v:
                continue
            fit = 1 if str(v.get("fit")).lower() == "yes" else 0
            await _mark(p["username"], fit, str(v.get("why") or "")[:80])
            good += fit
    return good


async def _mark(username: str, fit: int, why: str) -> None:
    async with db.connect() as c:
        await c.execute("UPDATE g_partners SET fit=?, why=?, status=CASE WHEN ?=0 THEN 'skip' ELSE status END "
                        "WHERE username=?", (fit, why, fit, username))
        await c.commit()


# ---------- проходы ----------

async def crawl(bot: Bot) -> dict:
    """Семена → их упоминания и репосты → новые каналы → отсев. → сводка для уведомления."""
    me = await ours(bot)
    async with db.connect() as c:
        cur = await c.execute("SELECT username FROM g_partners WHERE seed=1")
        seeds = [r["username"] for r in await cur.fetchall()]
        cur = await c.execute("SELECT username FROM g_partners")
        known = {r["username"] for r in await cur.fetchall()}
    stats = {"seeds": len(seeds), "seen": 0, "opened": 0, "fit": 0}
    if not seeds:
        return stats
    found: Counter = Counter()
    via: dict[str, str] = {}
    async with httpx.AsyncClient(headers=HEADERS) as client:
        for s in seeds:
            info = await fetch(client, s)
            await asyncio.sleep(PAUSE)
            if not info:
                continue
            await _save(info)
            for u in set(info["mentions"]):
                if u in SKIP or u == me["username"] or u.endswith("bot") or u in seeds:
                    continue
                found[u] += 1
                via.setdefault(u, s)
        stats["seen"] = len(found)
        fresh = [u for u, _ in found.most_common() if u not in known][:FETCH_NEW]
        for u in fresh:
            info = await fetch(client, u)
            await asyncio.sleep(PAUSE)
            if not info:
                continue
            stats["opened"] += 1
            await _save(info, status="new", found_via=via[u], mentions=found[u])
    stats["fit"] = await _judge(me["subs"] or 0)
    return stats


async def propose(bot: Bot, n: int = 5) -> int:
    """Лучших из подошедших — в список «Новые». → сколько добавлено."""
    me = await ours(bot)
    async with db.connect() as c:
        cur = await c.execute("SELECT * FROM g_partners WHERE status='new' AND fit=1")
        rows = await cur.fetchall()
    best = sorted(rows, key=lambda p: -rank(p, me["subs"] or 0))[:n]
    for p in best:
        await set_status(p["username"], "proposed")
    return len(best)


async def add_manual(text: str) -> tuple[list[str], list[str]]:
    """Каналы, которые назвал автор: сразу в «Новые» и в семена. → (добавлены, не открылись)"""
    names = list(dict.fromkeys(_names(text) + [w.lower() for w in re.findall(r"\b[A-Za-z][A-Za-z0-9_]{4,31}\b", text)
                                               if not w.lower().startswith(("http", "https"))]))
    names = [n for n in names if n not in SKIP and n not in ("tme", "telegram")][:15]
    added, failed = [], []
    async with httpx.AsyncClient(headers=HEADERS) as client:
        for u in names:
            info = await fetch(client, u)
            await asyncio.sleep(0.5)
            if not info:
                failed.append(u)
                continue
            old = await get(u)
            status = old["status"] if old and old["status"] in WORK else "proposed"
            await _save(info, seed=1, fit=1, why="добавлен тобой", status=status)
            added.append(u)
    return added, failed


# ---------- письмо ----------

KINDS = {
    "m": "Взаимный пиар: я пишу пост о вашем канале у себя, вы — о моём у себя, в согласованные дни.",
    "f": "Совместная папка Telegram: 8–15 близких по духу каналов в одной папке, каждый публикует ссылку на неё.",
}

DRAFT_SYSTEM = f"""Ты пишешь от лица автора Telegram-канала AHMAG первое сообщение админу другого канала с предложением сотрудничества. Автор — архитектор по образованию, креативный директор. Пишет спокойно и прямо, как человек человеку.

Сообщение: 4–7 предложений, на «вы».
1. Кто пишет и какой у него канал — одной фразой, с числом подписчиков.
2. Чем их канал близок — конкретно, по их постам и описанию. Не выдумывай того, чего в материале нет.
3. Что предлагаешь и как это будет выглядеть.
4. Лёгкий следующий шаг: например, прислать пару постов на выбор или обсудить даты.
Без лести, без «надеюсь на плодотворное сотрудничество», без восклицательных знаков.

{voice.RULES}

Верни ТОЛЬКО JSON: {{"text": "..."}}"""


async def draft(bot: Bot, username: str, kind: str) -> str:
    p = await get(username)
    if not p:
        raise RuntimeError(f"канала @{username} нет в списке")
    me = await ours(bot)
    sample = "\n".join("— " + s[:200] for s in json.loads(p["sample"] or "[]"))
    content = (
        f"# Мой канал\n{me['title']} (@{me['username']}), подписчиков: {me['subs']}\n"
        f"Описание: {me['description'] or '—'}\n"
        "О чём: личный архив — построенная архитектура, искусство, фотография, архив, кино; "
        "в постах несколько фото и абзац-два текста.\n\n"
        f"# Их канал\n{p['title']} (@{p['username']}), подписчиков: {p['subs'] or '?'}, "
        f"просмотров на пост ~{p['avg_views'] or '?'}\nОписание: {p['description'] or '—'}\n"
        f"Последние посты:\n{sample or '—'}\n\n# Что предлагаю\n{KINDS[kind]}\n\nНапиши первое сообщение.")
    data = await curator._call(content, system=DRAFT_SYSTEM, model=config.CLAUDE_MODEL, max_tokens=900)
    text = str(data.get("text") or "").strip()
    if not text:
        raise RuntimeError("Claude вернул пустое письмо")
    return text


def card(p, our_subs: int) -> str:
    """Карточка канала для экрана (HTML)."""
    er = _er(p)
    nums = [f"👥 {p['subs']:,}".replace(",", " ") if p["subs"] else "👥 ?"]
    if p["avg_views"]:
        nums.append(f"👁 ~{p['avg_views']:,}".replace(",", " ") + (f" ({er:.0%})" if er else ""))
    if p["posts_30d"] is not None and p["avg_views"] is not None:
        nums.append(f"постов за 30 дн.: {p['posts_30d']}{'+' if p['posts_30d'] >= 18 else ''}")
    lines = [f"<b>{html.escape(p['title'] or p['username'])}</b> · @{html.escape(p['username'])}", " · ".join(nums)]
    if p["why"]:
        lines.append(f"Почему: {html.escape(p['why'])}")
    if p["contact"]:
        lines.append(f"Контакт из описания: {html.escape(p['contact'])}")
    if p["found_via"]:
        lines.append(f"Нашёл через @{html.escape(p['found_via'])}" + (f", упоминаний {p['mentions']}" if p["mentions"] > 1 else ""))
    lines.append(f"Статус: {STATUS.get(p['status'], p['status'])}")
    if p["description"]:
        lines.append(f"<i>{html.escape(p['description'][:220])}</i>")
    return "\n".join(lines)
