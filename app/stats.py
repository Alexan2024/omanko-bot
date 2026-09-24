"""Статистика постов: как заходят вышедшие посты.

Telegram — просмотры с публичной страницы канала t.me/s/<канал> (как у партнёров, без API и без затрат).
Instagram — охват, лайки, сохранения, комментарии, репосты через Graph API. Для этого у токена нужно
разрешение instagram_business_manage_insights; без него Instagram-часть молчит, а на экране — подсказка.

Каждые 3 часа бот обновляет цифры постов, вышедших за последние 8 дней. Через 7 дней после выхода
цифры «замораживаются» — посты сравниваются честно, по одной и той же неделе жизни.
Экран «🏆 Что заходит» — из «📊 Статистики»: лучшие посты за 30 дней, рубрики, форматы, время, источники."""
import logging
import re
from datetime import datetime

import httpx
from bs4 import BeautifulSoup

from app import config, db, niche, screen
from app.screen import btn

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS post_stats (
    post_id INTEGER PRIMARY KEY,
    tg_views INTEGER,           -- последние снятые просмотры
    tg_7d INTEGER,              -- просмотры через неделю после выхода
    ig_reach INTEGER, ig_likes INTEGER, ig_saves INTEGER, ig_comments INTEGER, ig_shares INTEGER,
    ig_final INTEGER NOT NULL DEFAULT 0,   -- цифры Instagram через неделю — больше не обновляются
    updated_at TEXT NOT NULL
);
"""
IG_METRICS = {"reach": "ig_reach", "likes": "ig_likes", "saved": "ig_saves", "comments": "ig_comments",
              "shares": "ig_shares"}
_bot = None


async def init() -> None:
    async with db.connect() as c:
        await c.executescript(SCHEMA)
        await c.commit()


def install(bot) -> None:
    global _bot
    _bot = bot
    screen.VIEWS["perf"] = _v_perf


def schedule(sched, bot, guarded) -> None:
    sched.add_job(guarded(bot, "статистика постов", refresh, bot), "interval", hours=3, id="post_stats",
                  max_instances=1)


# ======================= сбор =======================

def _num(text: str) -> int | None:
    m = re.search(r"([\d.,]+)\s*([KkMm])?", (text or "").replace("\xa0", ""))
    if not m:
        return None
    x = float(m.group(1).replace(",", "."))
    return int(x * {"K": 1_000, "M": 1_000_000}.get((m.group(2) or "").upper(), 1))


async def _channel(bot) -> str | None:
    ch = str(config.CHANNEL_ID)
    if ch.startswith("@"):
        return ch[1:]
    try:
        return (await bot.get_chat(config.CHANNEL_ID)).username
    except Exception:
        return None


async def tg_views(client: httpx.AsyncClient, username: str, ids: list[int]) -> dict[int, int]:
    """Просмотры сообщений канала с публичной страницы. Альбом — одна запись с номером первого сообщения."""
    want, out = set(ids), {}
    before = max(ids) + 1
    for _ in range(12):                       # страница — ~20 записей; 12 страниц с запасом хватает на неделю
        r = await client.get(f"https://t.me/s/{username}", params={"before": before}, timeout=20)
        if r.status_code != 200:
            break
        seen = []
        for m in BeautifulSoup(r.text, "lxml").select(".tgme_widget_message[data-post]"):
            try:
                mid = int(m["data-post"].rsplit("/", 1)[1])
            except ValueError:
                continue
            seen.append(mid)
            v = m.select_one(".tgme_widget_message_views")
            if mid in want and v:
                out[mid] = _num(v.get_text()) or 0
        if not seen or min(seen) <= min(want) or want <= set(out):
            break
        before = min(seen)
    return out


async def _ig(client: httpx.AsyncClient, media_id: str) -> dict:
    from app import instagram
    data = (await instagram._get(client, f"{media_id}/insights", metric=",".join(IG_METRICS)))["data"]
    out = {}
    for d in data:
        val = (d.get("values") or [{}])[0].get("value") if d.get("values") else (d.get("total_value") or {}).get("value")
        if d.get("name") in IG_METRICS:
            out[IG_METRICS[d["name"]]] = int(val or 0)
    return out


async def refresh(bot=None) -> int:
    """Цифры для постов, вышедших за 8 дней, и финальные — для тех, кому исполнилась неделя."""
    bot = bot or _bot
    from app import instagram
    async with db.connect() as c:
        rows = await (await c.execute(
            "SELECT p.id, p.channel_msg_id, p.decided_at, s.tg_7d, s.ig_final, i.media_id FROM posts p "
            "LEFT JOIN post_stats s ON s.post_id=p.id LEFT JOIN ig_posts i ON i.post_id=p.id AND i.status='done' "
            "WHERE p.status='published' AND p.decided_at>=? AND (s.tg_7d IS NULL OR "
            "(i.media_id IS NOT NULL AND s.ig_final=0))", (db.days_ago(9),))).fetchall()
    if not rows:
        return 0
    now = datetime.fromisoformat(db.now())
    upd: dict[int, dict] = {r["id"]: {} for r in rows}
    async with httpx.AsyncClient(headers={"User-Agent": config.USER_AGENT}) as client:
        user = await _channel(bot) if bot else None
        ids = [r["channel_msg_id"] for r in rows if r["channel_msg_id"] and r["tg_7d"] is None]
        if user and ids:
            try:
                views = await tg_views(client, user, ids)
            except Exception:
                log.warning("Статистика: страница канала не прочиталась", exc_info=True)
                views = {}
            for r in rows:
                if r["channel_msg_id"] in views:
                    upd[r["id"]]["tg_views"] = views[r["channel_msg_id"]]
        ig_err = None
        if instagram.configured():
            for r in rows:
                if r["media_id"] and not r["ig_final"]:
                    try:
                        upd[r["id"]].update(await _ig(client, r["media_id"]))
                    except Exception as exc:
                        ig_err = str(exc)[:200]
                        break
        await db.set_setting("stats_ig_error", ig_err)
    for r in rows:
        f = upd[r["id"]]
        week = (now - datetime.fromisoformat(r["decided_at"])).days >= 7
        if week and "tg_views" in f:
            f["tg_7d"] = f["tg_views"]
        if week and any(k.startswith("ig_") for k in f):
            f["ig_final"] = 1
        if f:
            await _save(r["id"], f)
    return len(rows)


async def _save(pid: int, f: dict) -> None:
    f["updated_at"] = db.now()
    cols = ",".join(f)
    async with db.connect() as c:
        await c.execute(f"INSERT INTO post_stats(post_id,{cols}) VALUES (?,{','.join('?' * len(f))}) "
                        f"ON CONFLICT(post_id) DO UPDATE SET " + ",".join(f"{k}=excluded.{k}" for k in f),
                        (pid, *f.values()))
        await c.commit()


# ======================= отчёт =======================

async def rows(days: int = 30) -> list:
    """Вышедшие посты за days с цифрами: views — просмотры через неделю или последние снятые."""
    async with db.connect() as c:
        return await (await c.execute(
            "SELECT p.id, p.source, p.category, p.format, p.slot_key, p.data, p.decided_at, "
            "COALESCE(s.tg_7d, s.tg_views) views, s.tg_7d IS NOT NULL final, s.ig_reach, s.ig_saves, s.ig_likes "
            "FROM posts p JOIN post_stats s ON s.post_id=p.id WHERE p.status='published' AND p.decided_at>=? "
            "AND COALESCE(s.tg_7d, s.tg_views) IS NOT NULL", (db.days_ago(days),))).fetchall()


def _avg(xs: list) -> int:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs)) if xs else 0


def _groups(data: list, key, min_n: int = 2) -> list[tuple[str, int, int]]:
    g: dict[str, list] = {}
    for r in data:
        g.setdefault(key(r), []).append(r["views"])
    return sorted(((k, _avg(v), len(v)) for k, v in g.items() if len(v) >= min_n), key=lambda x: -x[1])


async def _v_perf(arg: dict):
    import html
    import json
    from app import cards
    data = await rows(30)
    lines = ["<b>🏆 Что заходит · 30 дней</b>"]
    if len(data) < 3:
        lines.append("Пока мало данных: цифры появятся через сутки после первых постов на v4.1, "
                     "честное сравнение — через неделю.")
    else:
        final = sum(1 for r in data if r["final"])
        ig = [r["ig_reach"] for r in data if r["ig_reach"] is not None]
        lines.append(f"Постов: {len(data)} (неделя прошла у {final}) · в среднем 👁 {_avg([r['views'] for r in data])}"
                     + (f" · охват в Instagram {_avg(ig)}" if ig else ""))
        lines += ["", "<b>Лучшие</b>"]
        for r in sorted(data, key=lambda r: -r["views"])[:5]:
            head = json.loads(r["data"]).get("headline") or "—"
            lines.append(f"👁 {r['views']} · {html.escape(head[:48])}")
        parts = [("Рубрики", _groups(data, lambda r: cards.cat_label(r["category"]))),
                 ("Формат", _groups(data, lambda r: cards.FORMAT_LABEL.get(r["format"], r["format"]))),
                 ("Время", _groups(data, lambda r: (r["slot_key"] or "")[-5:] or "вне слота")),
                 ("Находки", _groups(data, lambda r: "находки" if r["source"] in niche.FINDS else "остальное"))]
        lines.append("")
        for title, g in parts:
            if g:
                lines.append(f"<b>{title}:</b> " + " · ".join(f"{k} {v}" for k, v, _ in g))
        src = _groups(data, lambda r: r["source"] or "?", min_n=3)
        if len(src) >= 2:
            lines.append(f"<b>Источники</b> (от 3 постов): лучше всех {src[0][0]} {src[0][1]}, "
                         f"слабее всех {src[-1][0]} {src[-1][1]}")
        saves = [r for r in data if r["ig_saves"]]
        if saves:
            best = max(saves, key=lambda r: r["ig_saves"])
            lines.append(f"<b>Больше всего сохранений в Instagram:</b> {best['ig_saves']} · "
                         f"{html.escape((json.loads(best['data']).get('headline') or '')[:40])}")
    err = await db.get_setting("stats_ig_error")
    if err:
        lines += ["", "⚠️ Instagram не отдаёт статистику. Нужно разрешение instagram_business_manage_insights: "
                      "Meta → приложение → права, затем новый токен в IG_ACCESS_TOKEN."]
    rows_kb = [[btn("🔄 Обновить цифры", "h:perfr"), btn("← Статистика", "h:stats")]]
    return screen.banner(), "\n".join(lines)[:1020], screen._kb(rows_kb), arg
