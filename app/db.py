import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite

from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE NOT NULL,
    source TEXT NOT NULL,
    title TEXT,
    payload TEXT,              -- json: данные источника (RSS-текст, музейные метаданные)
    status TEXT NOT NULL DEFAULT 'new',   -- new | triaged | batched | processed | skipped | error
    note TEXT,
    created_at TEXT NOT NULL,
    tcat TEXT,                 -- рубрика по первичному фильтру
    tprio INTEGER NOT NULL DEFAULT 0,     -- 2 — «да», 1 — «может быть»
    prep TEXT,                 -- json: заголовок, текст, фото — подготовлено к оценке
    batch_id TEXT
);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER REFERENCES candidates(id),
    source TEXT,
    url TEXT,
    category TEXT,
    data TEXT NOT NULL,        -- json: заголовок, текст, кредиты, теги, служебные поля
    caption TEXT NOT NULL,     -- готовый HTML
    score INTEGER,
    reason TEXT,
    images TEXT NOT NULL,      -- json: пути к файлам
    file_ids TEXT,             -- json: {индекс фото: file_id} после первой загрузки в Telegram
    status TEXT NOT NULL,      -- ready | sent | approved | announced | published | rejected | auto_rejected
    reject_reason TEXT,
    card_chat_id INTEGER,
    card_msg_id INTEGER,
    album_msg_id INTEGER,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    decided_at TEXT,
    format TEXT NOT NULL DEFAULT 'std',   -- std | mini | notes
    slot_key TEXT,             -- 'YYYY-MM-DD HH:MM'
    offers INTEGER NOT NULL DEFAULT 0,
    channel_msg_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);
CREATE INDEX IF NOT EXISTS idx_cand_status ON candidates(status);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL        -- json
);

CREATE TABLE IF NOT EXISTS usage (
    day TEXT PRIMARY KEY,
    calls INTEGER NOT NULL DEFAULT 0,
    in_tok INTEGER NOT NULL DEFAULT 0,
    out_tok INTEGER NOT NULL DEFAULT 0,
    cost REAL NOT NULL DEFAULT 0,
    bg_cost REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL,
    brief TEXT,
    status TEXT NOT NULL,      -- research | plan | written | cancelled
    post_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,      -- open | done
    n INTEGER NOT NULL DEFAULT 0,
    manual INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS edits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER,
    format TEXT,
    before TEXT NOT NULL,      -- версия бота
    after TEXT NOT NULL,       -- версия автора
    created_at TEXT NOT NULL
);
"""

# колонки, которых нет в базах прошлых версий: {таблица: {колонка: DDL}}
MIGRATIONS = {
    "posts": {
        "album_msg_id": "ALTER TABLE posts ADD COLUMN album_msg_id INTEGER",
        "format": "ALTER TABLE posts ADD COLUMN format TEXT NOT NULL DEFAULT 'std'",
        "slot_key": "ALTER TABLE posts ADD COLUMN slot_key TEXT",
        "offers": "ALTER TABLE posts ADD COLUMN offers INTEGER NOT NULL DEFAULT 0",
        "channel_msg_id": "ALTER TABLE posts ADD COLUMN channel_msg_id INTEGER",
    },
    "candidates": {
        "tcat": "ALTER TABLE candidates ADD COLUMN tcat TEXT",
        "tprio": "ALTER TABLE candidates ADD COLUMN tprio INTEGER NOT NULL DEFAULT 0",
        "prep": "ALTER TABLE candidates ADD COLUMN prep TEXT",
        "batch_id": "ALTER TABLE candidates ADD COLUMN batch_id TEXT",
    },
    "usage": {
        "cost": "ALTER TABLE usage ADD COLUMN cost REAL NOT NULL DEFAULT 0",
        "bg_cost": "ALTER TABLE usage ADD COLUMN bg_cost REAL NOT NULL DEFAULT 0",
    },
}

MUSEUMS = ("met", "cma")


def _now_dt() -> datetime:
    return datetime.now(ZoneInfo(config.TZ_NAME))


def now() -> str:
    return _now_dt().isoformat(timespec="seconds")


def today_start() -> str:
    return _now_dt().replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")


def days_ago(days: float) -> str:
    return (_now_dt() - timedelta(days=days)).isoformat(timespec="seconds")


def hours_ago(hours: float) -> str:
    return (_now_dt() - timedelta(hours=hours)).isoformat(timespec="seconds")


@asynccontextmanager
async def connect():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(config.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        yield conn


async def init() -> None:
    async with connect() as db:
        added_cost = False
        for table, cols in MIGRATIONS.items():
            cur = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
            if not await cur.fetchone():
                continue
            cur = await db.execute(f"PRAGMA table_info({table})")
            have = {r["name"] for r in await cur.fetchall()}
            for col, ddl in cols.items():
                if col not in have:
                    await db.execute(ddl)
                    added_cost = added_cost or (table == "usage" and col == "cost")
        await db.executescript(SCHEMA)
        if added_cost:  # расход прошлых дней в долларах по тарифу Sonnet 5 — чтобы итоги недели не начинались с нуля
            await db.execute("UPDATE usage SET cost = in_tok * 2e-6 + out_tok * 1e-5, "
                             "bg_cost = in_tok * 2e-6 + out_tok * 1e-5")
        # рубрики сведены к пяти: интерьеры — в архитектуру, скульптура, инсталляции, выставки — в искусство
        for old, new in config.CATEGORY_ALIASES.items():
            await db.execute("UPDATE posts SET category=? WHERE category=?", (new, old))
        await db.commit()


# ---------- settings ----------

async def get_setting(key: str, default=None):
    async with connect() as db:
        cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
    return json.loads(row["value"]) if row else default


async def set_setting(key: str, value) -> None:
    async with connect() as db:
        await db.execute(
            "INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        await db.commit()


# ---------- usage ----------

async def add_usage(in_tok: int, out_tok: int, cost: float = 0.0, background: bool = False) -> None:
    day = _now_dt().date().isoformat()
    async with connect() as db:
        await db.execute(
            "INSERT INTO usage(day, calls, in_tok, out_tok, cost, bg_cost) VALUES (?,1,?,?,?,?) "
            "ON CONFLICT(day) DO UPDATE SET calls=calls+1, in_tok=in_tok+excluded.in_tok, "
            "out_tok=out_tok+excluded.out_tok, cost=cost+excluded.cost, bg_cost=bg_cost+excluded.bg_cost",
            (day, in_tok, out_tok, cost, cost if background else 0.0),
        )
        await db.commit()


async def calls_today() -> int:
    day = _now_dt().date().isoformat()
    async with connect() as db:
        cur = await db.execute("SELECT calls FROM usage WHERE day=?", (day,))
        row = await cur.fetchone()
    return row["calls"] if row else 0


async def cost_today(background: bool = False) -> float:
    day = _now_dt().date().isoformat()
    col = "bg_cost" if background else "cost"
    async with connect() as db:
        cur = await db.execute(f"SELECT {col} c FROM usage WHERE day=?", (day,))
        row = await cur.fetchone()
    return float(row["c"]) if row else 0.0


async def cost_days(days: int) -> float:
    since = (_now_dt() - timedelta(days=days - 1)).date().isoformat()
    async with connect() as db:
        cur = await db.execute("SELECT COALESCE(SUM(cost),0) c FROM usage WHERE day>=?", (since,))
        row = await cur.fetchone()
    return float(row["c"])


# ---------- candidates ----------

async def add_candidate(url: str, source: str, title: str, payload: dict | None = None) -> int | None:
    """→ id нового кандидата или None, если такой адрес уже был."""
    async with connect() as db:
        cur = await db.execute(
            "INSERT OR IGNORE INTO candidates(url, source, title, payload, created_at) VALUES (?,?,?,?,?)",
            (url, source, title, json.dumps(payload or {}, ensure_ascii=False), now()),
        )
        await db.commit()
        return cur.lastrowid if cur.rowcount > 0 else None


async def get_candidate(cid: int) -> aiosqlite.Row | None:
    async with connect() as db:
        cur = await db.execute("SELECT * FROM candidates WHERE id=?", (cid,))
        return await cur.fetchone()


async def get_candidate_by_url(url: str) -> aiosqlite.Row | None:
    async with connect() as db:
        cur = await db.execute("SELECT * FROM candidates WHERE url=?", (url,))
        return await cur.fetchone()


async def candidates(status: str, limit: int = 500, newest_first: bool = True) -> list[aiosqlite.Row]:
    async with connect() as db:
        cur = await db.execute(
            f"SELECT * FROM candidates WHERE status=? ORDER BY id {'DESC' if newest_first else 'ASC'} LIMIT ?",
            (status, limit))
        return await cur.fetchall()


async def update_candidate(cid: int, **f) -> None:
    if "prep" in f and f["prep"] is not None and not isinstance(f["prep"], str):
        f["prep"] = json.dumps(f["prep"], ensure_ascii=False)
    if "note" in f and f["note"]:
        f["note"] = str(f["note"])[:500]
    sets = ",".join(f"{k}=?" for k in f)
    async with connect() as db:
        await db.execute(f"UPDATE candidates SET {sets} WHERE id=?", (*f.values(), cid))
        await db.commit()


async def mark_candidate(cid: int, status: str, note: str = "") -> None:
    await update_candidate(cid, status=status, note=note)


async def expire_candidates(days: int) -> int:
    """Отобранное, но так и не оценённое за N дней — устарело."""
    async with connect() as db:
        cur = await db.execute(
            "UPDATE candidates SET status='skipped', note='устарел, не дошла очередь' "
            "WHERE status IN ('new','triaged') AND created_at < ?", (days_ago(days),))
        await db.commit()
        return cur.rowcount


async def candidate_counts() -> dict[str, int]:
    async with connect() as db:
        cur = await db.execute("SELECT status, COUNT(*) c FROM candidates GROUP BY status")
        return {r["status"]: r["c"] for r in await cur.fetchall()}


async def recent_titles(days: int = 120) -> list[str]:
    """Заголовки источников у материалов, которые дошли до поста или стоят в очереди на оценку, — для поиска дублей."""
    async with connect() as db:
        cur = await db.execute(
            "SELECT title FROM candidates WHERE created_at >= ? AND title != '' AND "
            "(status IN ('triaged','batched') OR id IN (SELECT candidate_id FROM posts WHERE candidate_id IS NOT NULL))",
            (days_ago(days),))
        return [r["title"] for r in await cur.fetchall()]


# ---------- batches ----------

async def add_batch(bid: str, n: int, manual: bool = False) -> None:
    async with connect() as db:
        await db.execute("INSERT INTO batches(id, created_at, status, n, manual) VALUES (?,?,?,?,?)",
                         (bid, now(), "open", n, int(manual)))
        await db.commit()


async def open_batches() -> list[aiosqlite.Row]:
    async with connect() as db:
        cur = await db.execute("SELECT * FROM batches WHERE status='open' ORDER BY created_at")
        return await cur.fetchall()


async def close_batch(bid: str) -> None:
    async with connect() as db:
        await db.execute("UPDATE batches SET status='done' WHERE id=?", (bid,))
        await db.commit()


async def batched_count() -> int:
    async with connect() as db:
        cur = await db.execute("SELECT COUNT(*) c FROM candidates WHERE status='batched'")
        return (await cur.fetchone())["c"]


async def batched_categories() -> dict[str, int]:
    async with connect() as db:
        cur = await db.execute("SELECT tcat, COUNT(*) c FROM candidates WHERE status='batched' GROUP BY tcat")
        return {r["tcat"] or "architecture": r["c"] for r in await cur.fetchall()}


# ---------- posts ----------

async def add_post(**f) -> int:
    f.setdefault("created_at", now())
    for k in ("data", "images"):
        if not isinstance(f[k], str):
            f[k] = json.dumps(f[k], ensure_ascii=False)
    cols = ",".join(f)
    q = ",".join("?" * len(f))
    async with connect() as db:
        cur = await db.execute(f"INSERT INTO posts({cols}) VALUES ({q})", tuple(f.values()))
        await db.commit()
        return cur.lastrowid


async def get_post(pid: int) -> aiosqlite.Row | None:
    async with connect() as db:
        cur = await db.execute("SELECT * FROM posts WHERE id=?", (pid,))
        return await cur.fetchone()


async def post_by_candidate(cid: int) -> aiosqlite.Row | None:
    async with connect() as db:
        cur = await db.execute("SELECT * FROM posts WHERE candidate_id=? ORDER BY id DESC LIMIT 1", (cid,))
        return await cur.fetchone()


async def update_post(pid: int, **f) -> None:
    for k in ("data", "images", "file_ids"):
        if k in f and f[k] is not None and not isinstance(f[k], str):
            f[k] = json.dumps(f[k], ensure_ascii=False)
    sets = ",".join(f"{k}=?" for k in f)
    async with connect() as db:
        await db.execute(f"UPDATE posts SET {sets} WHERE id=?", (*f.values(), pid))
        await db.commit()


async def ready_posts(fmt: str | None = None, category: str | None = None) -> list[aiosqlite.Row]:
    q, args = "SELECT * FROM posts WHERE status='ready'", []
    if fmt:
        q += " AND format=?"
        args.append(fmt)
    if category:
        q += " AND category=?"
        args.append(category)
    async with connect() as db:
        cur = await db.execute(q + " ORDER BY score DESC, id DESC", tuple(args))
        return await cur.fetchall()


async def count_ready(fmt: str | None = None) -> int:
    return len(await ready_posts(fmt))


async def stock_counts() -> dict[str, dict[str, int]]:
    """{рубрика: {формат: число}} — запас: написано и оценено, но ещё не показано."""
    async with connect() as db:
        cur = await db.execute(
            "SELECT category, format, COUNT(*) c FROM posts WHERE status='ready' GROUP BY category, format")
        rows = await cur.fetchall()
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        cat = config.CATEGORY_ALIASES.get(r["category"], r["category"]) or "architecture"
        out.setdefault(cat, {}).setdefault(r["format"], 0)
        out[cat][r["format"]] += r["c"]
    return out


async def inbox_posts() -> list[aiosqlite.Row]:
    """Ждут решения: сначала предложенные к слотам (по времени слота), потом остальные."""
    async with connect() as db:
        cur = await db.execute(
            "SELECT * FROM posts WHERE status IN ('sent','announced') "
            "ORDER BY slot_key IS NULL, slot_key ASC, sent_at ASC, id ASC")
        return await cur.fetchall()


async def scheduled_posts(from_key: str) -> list[aiosqlite.Row]:
    async with connect() as db:
        cur = await db.execute(
            "SELECT * FROM posts WHERE status='approved' AND slot_key>=? ORDER BY slot_key ASC", (from_key,))
        return await cur.fetchall()


async def approved_posts(fmt: str | None = None) -> list[aiosqlite.Row]:
    q, args = "SELECT * FROM posts WHERE status='approved'", ()
    if fmt:
        q += " AND format=?"
        args = (fmt,)
    async with connect() as db:
        cur = await db.execute(q + " ORDER BY decided_at ASC, id ASC", args)
        return await cur.fetchall()


async def announced_posts(slot_key: str | None = None) -> list[aiosqlite.Row]:
    q, args = "SELECT * FROM posts WHERE status='announced'", ()
    if slot_key:
        q += " AND slot_key=?"
        args = (slot_key,)
    async with connect() as db:
        cur = await db.execute(q + " ORDER BY id ASC", args)
        return await cur.fetchall()


async def posts_in_slots(keys: list[str]) -> list[aiosqlite.Row]:
    if not keys:
        return []
    q = ",".join("?" * len(keys))
    async with connect() as db:
        cur = await db.execute(
            f"SELECT * FROM posts WHERE slot_key IN ({q}) "
            "AND status IN ('published','approved','announced','sent') ORDER BY id ASC", tuple(keys))
        return await cur.fetchall()


async def approved_in_slot(slot_key: str) -> aiosqlite.Row | None:
    async with connect() as db:
        cur = await db.execute(
            "SELECT * FROM posts WHERE status='approved' AND slot_key=? ORDER BY decided_at ASC LIMIT 1", (slot_key,))
        return await cur.fetchone()


async def overdue_approved(slot_key: str, fmt: str | None = None) -> list[aiosqlite.Row]:
    """Одобренные посты без слота или со слотом, который уже прошёл (бот лежал, стояла пауза)."""
    q, args = "SELECT * FROM posts WHERE status='approved' AND (slot_key IS NULL OR slot_key<?)", [slot_key]
    if fmt:
        q += " AND format=?"
        args.append(fmt)
    async with connect() as db:
        cur = await db.execute(q + " ORDER BY decided_at ASC, id ASC", tuple(args))
        return await cur.fetchall()


async def taken_keys(from_key: str) -> set[str]:
    async with connect() as db:
        cur = await db.execute(
            "SELECT slot_key FROM posts WHERE status IN ('approved','announced') AND slot_key>=?", (from_key,))
        return {r["slot_key"] for r in await cur.fetchall()}


async def slot_leftovers(slot_key: str) -> list[aiosqlite.Row]:
    """Предложенные к этому (или более раннему) слоту и так и не выбранные."""
    async with connect() as db:
        cur = await db.execute(
            "SELECT * FROM posts WHERE status IN ('sent','announced') AND slot_key IS NOT NULL AND slot_key<=?",
            (slot_key,))
        return await cur.fetchall()


async def stale_inbox(hours: int, now_key: str) -> list[aiosqlite.Row]:
    """Ждут решения дольше срока и не привязаны к будущему слоту."""
    async with connect() as db:
        cur = await db.execute(
            "SELECT * FROM posts WHERE status='sent' AND sent_at < ? AND (slot_key IS NULL OR slot_key < ?)",
            (hours_ago(hours), now_key))
        return await cur.fetchall()


async def sent_today() -> list[aiosqlite.Row]:
    async with connect() as db:
        cur = await db.execute("SELECT category, source FROM posts WHERE sent_at >= ?", (today_start(),))
        return await cur.fetchall()


async def recent_mix(days: int = 7) -> list[str]:
    """Рубрики того, что вышло, стоит в слотах или предложено за последние дни, — для баланса 50/50."""
    async with connect() as db:
        cur = await db.execute(
            "SELECT category FROM posts WHERE status IN ('published','approved','announced','sent') "
            "AND COALESCE(sent_at, decided_at, created_at) >= ?", (days_ago(days),))
        return [config.CATEGORY_ALIASES.get(r["category"], r["category"]) for r in await cur.fetchall()]


async def recent_rejections(limit: int = 10) -> list[aiosqlite.Row]:
    async with connect() as db:
        cur = await db.execute(
            "SELECT data, reject_reason FROM posts WHERE status='rejected' ORDER BY decided_at DESC LIMIT ?",
            (limit,))
        return await cur.fetchall()


async def published_posts(limit: int = 30, fmt: str | None = None) -> list[aiosqlite.Row]:
    q, args = "SELECT caption, data, category, format FROM posts WHERE status='published'", ()
    if fmt:
        q += " AND format=?"
        args = (fmt,)
    async with connect() as db:
        cur = await db.execute(q + " ORDER BY decided_at DESC LIMIT ?", (*args, limit))
        return await cur.fetchall()


async def recent_headlines(days: int = 45, limit: int = 80) -> list[str]:
    async with connect() as db:
        cur = await db.execute(
            "SELECT data FROM posts WHERE status IN ('ready','sent','approved','announced','published') "
            "AND created_at >= ? ORDER BY id DESC LIMIT ?", (days_ago(days), limit))
        rows = await cur.fetchall()
    return [h for h in (json.loads(r["data"]).get("headline", "") for r in rows) if h]


async def purge_ready(source: str) -> int:
    async with connect() as db:
        cur = await db.execute(
            "UPDATE posts SET status='auto_rejected', reject_reason='очищено вручную' "
            "WHERE status='ready' AND source=?", (source,))
        await db.commit()
        return cur.rowcount


async def finished_before(days: int) -> list[aiosqlite.Row]:
    """Решённые посты старше N дней — их файлы можно удалять."""
    async with connect() as db:
        cur = await db.execute(
            "SELECT id, images FROM posts WHERE status IN ('published','rejected','auto_rejected') "
            "AND COALESCE(decided_at, created_at) < ?", (days_ago(days),))
        return await cur.fetchall()


# ---------- правки автора (голос) ----------

async def add_edit(pid: int, fmt: str, before: str, after: str) -> None:
    async with connect() as db:
        await db.execute("INSERT INTO edits(post_id, format, before, after, created_at) VALUES (?,?,?,?,?)",
                         (pid, fmt, before, after, now()))
        await db.commit()


async def recent_edits(limit: int = 5) -> list[aiosqlite.Row]:
    async with connect() as db:
        cur = await db.execute("SELECT * FROM edits ORDER BY id DESC LIMIT ?", (limit,))
        return await cur.fetchall()


async def edits_count() -> int:
    async with connect() as db:
        cur = await db.execute("SELECT COUNT(*) c FROM edits")
        return (await cur.fetchone())["c"]


# ---------- статистика ----------

async def stats() -> dict:
    async with connect() as db:
        cur = await db.execute("SELECT source, COUNT(*) c FROM posts WHERE status='ready' GROUP BY source")
        ready_by_source = {r["source"]: r["c"] for r in await cur.fetchall()}
        cur = await db.execute("SELECT format, COUNT(*) c FROM posts WHERE status='ready' GROUP BY format")
        ready_by_format = {r["format"]: r["c"] for r in await cur.fetchall()}
        cur = await db.execute("SELECT source, COUNT(*) c FROM candidates WHERE status IN ('new','triaged') "
                               "GROUP BY source")
        new_by_source = {r["source"]: r["c"] for r in await cur.fetchall()}
        cur = await db.execute("SELECT status, COUNT(*) c FROM posts GROUP BY status")
        posts = {r["status"]: r["c"] for r in await cur.fetchall()}
        cur = await db.execute("SELECT status, COUNT(*) c FROM candidates GROUP BY status")
        cands = {r["status"]: r["c"] for r in await cur.fetchall()}
    return {"posts": posts, "candidates": cands, "ready_by_source": ready_by_source,
            "ready_by_format": ready_by_format, "new_by_source": new_by_source}


async def source_stats(days: int | None = None) -> dict[str, dict[str, int]]:
    """{источник: {'published': n, 'rejected': m}} — только решения автора."""
    q = ("SELECT source, status, COUNT(*) c FROM posts "
         "WHERE status IN ('published','rejected','approved')")
    args: tuple = ()
    if days:
        q += " AND decided_at >= ?"
        args = (days_ago(days),)
    async with connect() as db:
        cur = await db.execute(q + " GROUP BY source, status", args)
        rows = await cur.fetchall()
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        key = "rejected" if r["status"] == "rejected" else "published"
        d = out.setdefault(r["source"] or "?", {"published": 0, "rejected": 0})
        d[key] += r["c"]
    return out


async def digest(days: int = 7) -> dict:
    since = days_ago(days)
    async with connect() as db:
        cur = await db.execute(
            "SELECT format, COUNT(*) c FROM posts WHERE status='published' AND decided_at>=? GROUP BY format",
            (since,))
        by_format = {r["format"]: r["c"] for r in await cur.fetchall()}
        cur = await db.execute(
            "SELECT category, COUNT(*) c FROM posts WHERE status='published' AND decided_at>=? GROUP BY category",
            (since,))
        by_category = {r["category"]: r["c"] for r in await cur.fetchall()}
        cur = await db.execute(
            "SELECT reject_reason, COUNT(*) c FROM posts WHERE status='rejected' AND decided_at>=? "
            "GROUP BY reject_reason", (since,))
        reasons = {r["reject_reason"] or "?": r["c"] for r in await cur.fetchall()}
        cur = await db.execute(
            "SELECT COALESCE(SUM(calls),0) calls, COALESCE(SUM(in_tok),0) i, COALESCE(SUM(out_tok),0) o, "
            "COALESCE(SUM(cost),0) cost FROM usage WHERE day>=?", (since[:10],))
        u = await cur.fetchone()
    return {"by_format": by_format, "by_category": by_category, "reasons": reasons,
            "sources": await source_stats(days),
            "usage": {"calls": u["calls"], "in_tok": u["i"], "out_tok": u["o"], "cost": u["cost"]}}


# ---------- notes ----------

async def add_note(topic: str, brief: dict | None = None, status: str = "research") -> int:
    async with connect() as db:
        cur = await db.execute(
            "INSERT INTO notes(topic, brief, status, created_at) VALUES (?,?,?,?)",
            (topic, json.dumps(brief or {}, ensure_ascii=False), status, now()))
        await db.commit()
        return cur.lastrowid


async def get_note(nid: int) -> aiosqlite.Row | None:
    async with connect() as db:
        cur = await db.execute("SELECT * FROM notes WHERE id=?", (nid,))
        return await cur.fetchone()


async def update_note(nid: int, **f) -> None:
    if "brief" in f and not isinstance(f["brief"], str):
        f["brief"] = json.dumps(f["brief"], ensure_ascii=False)
    sets = ",".join(f"{k}=?" for k in f)
    async with connect() as db:
        await db.execute(f"UPDATE notes SET {sets} WHERE id=?", (*f.values(), nid))
        await db.commit()


async def note_topics(limit: int = 30) -> list[str]:
    async with connect() as db:
        cur = await db.execute("SELECT topic FROM notes ORDER BY id DESC LIMIT ?", (limit,))
        return [r["topic"] for r in await cur.fetchall()]
