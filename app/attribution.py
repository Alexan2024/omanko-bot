"""Откуда приходят подписчики.
Бот создаёт именные пригласительные ссылки (по одной на площадку: Instagram, VK, посев в таком-то канале…)
и видит, кто по какой ссылке вступил в канал и кто потом ушёл. Раз в день запоминает число подписчиков.
Нужно право администратора канала «Приглашать пользователей» (Invite users via link)."""
import html
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Router
from aiogram.types import ChatMemberUpdated

from app import config, db

log = logging.getLogger(__name__)
router = Router(name="attribution")

SCHEMA = """
CREATE TABLE IF NOT EXISTS g_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    url TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS g_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    event TEXT NOT NULL,       -- join | leave
    source TEXT NOT NULL,      -- L<id ссылки> | direct | folder | request | other | unknown
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gm_at ON g_members(at);
CREATE INDEX IF NOT EXISTS idx_gm_user ON g_members(user_id);
CREATE TABLE IF NOT EXISTS g_counts (
    day TEXT PRIMARY KEY,
    n INTEGER NOT NULL
);
"""

NAMES = {
    "direct": "напрямую (поиск, @username, репосты, похожие каналы)",
    "folder": "папки",
    "request": "заявки на вступление",
    "other": "чужие ссылки",
    "unknown": "подписаны до начала учёта",
}
INSIDE = {"member", "administrator", "creator"}


async def init() -> None:
    async with db.connect() as c:
        await c.executescript(SCHEMA)
        await c.commit()


def _today():
    return datetime.now(ZoneInfo(config.TZ_NAME)).date()


def _is_channel(chat) -> bool:
    ch = str(config.CHANNEL_ID)
    if ch.startswith("@"):
        return (chat.username or "").lower() == ch[1:].lower()
    return str(chat.id) == ch


def _inside(member) -> bool:
    st = getattr(member.status, "value", member.status)
    return st in INSIDE or (st == "restricted" and bool(getattr(member, "is_member", False)))


# ---------- вступления и выходы ----------

@router.chat_member()
async def on_member(ev: ChatMemberUpdated):
    if not _is_channel(ev.chat):
        return
    was, now = _inside(ev.old_chat_member), _inside(ev.new_chat_member)
    if was == now:
        return
    uid = ev.new_chat_member.user.id
    if now:
        event, source = "join", await _join_source(ev)
    else:
        event, source = "leave", await _last_source(uid)
    async with db.connect() as c:
        await c.execute("INSERT INTO g_members(user_id, event, source, at) VALUES (?,?,?,?)",
                        (uid, event, source, db.now()))
        await c.commit()


async def _join_source(ev: ChatMemberUpdated) -> str:
    if ev.invite_link:
        async with db.connect() as c:
            cur = await c.execute("SELECT id FROM g_links WHERE url=?", (ev.invite_link.invite_link,))
            row = await cur.fetchone()
        return f"L{row['id']}" if row else "other"
    if getattr(ev, "via_chat_folder_invite_link", False):
        return "folder"
    if getattr(ev, "via_join_request", False):
        return "request"
    return "direct"


async def _last_source(uid: int) -> str:
    """Ушедшего относим к тому источнику, откуда он пришёл, — так видно, какие площадки дают живых людей."""
    async with db.connect() as c:
        cur = await c.execute("SELECT source FROM g_members WHERE user_id=? AND event='join' ORDER BY id DESC LIMIT 1",
                              (uid,))
        row = await cur.fetchone()
    return row["source"] if row else "unknown"


# ---------- ссылки ----------

async def links() -> list:
    """Действующие ссылки со счётчиками: сколько пришло и сколько из них ушло."""
    async with db.connect() as c:
        cur = await c.execute(
            "SELECT l.*, "
            "(SELECT COUNT(*) FROM g_members m WHERE m.source='L'||l.id AND m.event='join') joins, "
            "(SELECT COUNT(*) FROM g_members m WHERE m.source='L'||l.id AND m.event='leave') leaves "
            "FROM g_links l WHERE revoked=0 ORDER BY id")
        return await cur.fetchall()


async def create(bot: Bot, label: str) -> str:
    link = await bot.create_chat_invite_link(config.CHANNEL_ID, name=label[:32])
    async with db.connect() as c:
        await c.execute("INSERT INTO g_links(label, url, created_at) VALUES (?,?,?)",
                        (label[:32], link.invite_link, db.now()))
        await c.commit()
    return link.invite_link


async def revoke(bot: Bot, lid: int) -> str | None:
    async with db.connect() as c:
        cur = await c.execute("SELECT * FROM g_links WHERE id=?", (lid,))
        row = await cur.fetchone()
    if not row:
        return None
    try:
        await bot.revoke_chat_invite_link(config.CHANNEL_ID, row["url"])
    except Exception:
        log.warning("Ссылка %s не отозвалась в Telegram — убираю только из списка", row["url"], exc_info=True)
    async with db.connect() as c:
        await c.execute("UPDATE g_links SET revoked=1 WHERE id=?", (lid,))
        await c.commit()
    return row["label"]


async def can_invite(bot: Bot) -> bool | None:
    """Есть ли у бота право создавать ссылки. None — не удалось проверить."""
    try:
        me = await bot.get_chat_member(config.CHANNEL_ID, bot.id)
    except Exception:
        return None
    st = getattr(me.status, "value", me.status)
    if st == "creator":
        return True
    return bool(getattr(me, "can_invite_users", False))


# ---------- счёт ----------

async def subscribers(bot: Bot) -> int | None:
    try:
        return await bot.get_chat_member_count(config.CHANNEL_ID)
    except Exception:
        log.warning("Не узнал число подписчиков", exc_info=True)
        return None


async def snapshot(bot: Bot) -> None:
    """Раз в день запоминаем число подписчиков — из этого складывается кривая роста."""
    n = await subscribers(bot)
    if n is None:
        return
    async with db.connect() as c:
        await c.execute("INSERT INTO g_counts(day, n) VALUES (?,?) ON CONFLICT(day) DO UPDATE SET n=excluded.n",
                        (_today().isoformat(), n))
        await c.commit()


async def count_days_ago(days: int) -> int | None:
    day = (_today() - timedelta(days=days)).isoformat()
    async with db.connect() as c:
        cur = await c.execute("SELECT n FROM g_counts WHERE day<=? ORDER BY day DESC LIMIT 1", (day,))
        row = await cur.fetchone()
    return row["n"] if row else None


async def by_source(days: int | None = 7) -> list[tuple[str, int, int]]:
    """[(источник, пришло, ушло)] за последние дни, самые крупные сверху."""
    q, args = "SELECT source, event, COUNT(*) c FROM g_members", ()
    if days:
        q += " WHERE at>=?"
        args = (db.days_ago(days),)
    async with db.connect() as c:
        cur = await c.execute(q + " GROUP BY source, event", args)
        rows = await cur.fetchall()
        cur = await c.execute("SELECT id, label FROM g_links")
        labels = {f"L{r['id']}": r["label"] for r in await cur.fetchall()}
    out: dict[str, list[int]] = {}
    for r in rows:
        d = out.setdefault(r["source"], [0, 0])
        d[0 if r["event"] == "join" else 1] += r["c"]
    ordered = sorted(out.items(), key=lambda kv: (-kv[1][0], kv[1][1]))
    return [(labels.get(src) or NAMES.get(src, src), j, l) for src, (j, l) in ordered]


async def totals(days: int) -> tuple[int, int]:
    async with db.connect() as c:
        cur = await c.execute("SELECT event, COUNT(*) c FROM g_members WHERE at>=? GROUP BY event",
                              (db.days_ago(days),))
        d = {r["event"]: r["c"] for r in await cur.fetchall()}
    return d.get("join", 0), d.get("leave", 0)


async def summary(bot: Bot, days: int = 7) -> str:
    """Короткий отчёт для уведомления раз в неделю."""
    now = await subscribers(bot)
    was = await count_days_ago(days)
    joins, leaves = await totals(days)
    head = f"<b>📈 Подписчики за {days} дней</b>\n"
    head += f"Сейчас {now:,}".replace(",", " ") if now is not None else "Сейчас — не удалось узнать"
    if now is not None and was is not None:
        head += f" · {now - was:+d}"
    lines = [head, f"Пришли {joins} · ушли {leaves}"]
    src = await by_source(days)
    if src:
        lines.append("")
        lines += [f"{html.escape(name)} — {j}" + (f", ушли {l}" if l else "") for name, j, l in src[:8]]
    return "\n".join(lines)
