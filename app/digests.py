"""Подборки из уже вышедших постов — то, что пересылают и сохраняют.
«Неделя в AHMAG» собирается раз в неделю сама, без Claude: обложки постов альбомом и список ссылок на них.
Тематические подборки (страна, материал, приём, эпоха) предлагает Claude по архиву вышедших постов, автор выбирает.
Готовая подборка — обычный пост (формат notes, источник digest): она ложится во входящие, одобряется в слот
и публикуется как всё остальное. Текст правится через «✏️ Свой текст»."""
import html
import json
import logging
import shutil
import time
from datetime import timedelta
from pathlib import Path

from aiogram import Bot

from app import cards, config, curator, db, formatter, slots, voice

log = logging.getLogger(__name__)

WEEK_TAG = "#ahmagweek"
THEME_TAG = "#ahmagselection"
MAX_ITEMS = 10          # альбом в Telegram — до 10 фото
MIN_ITEMS = 4
MIN_POOL = 12           # меньше вышедших постов — тематические подборки не из чего собирать
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря"]
FLAG = "подборка: текст правь через «✏️ Свой текст», «🔁 Переписать» для неё не нужен"


# ---------- материал ----------

async def published(days: int | None = None, limit: int = 400) -> list:
    """Вышедшие посты со ссылкой на канал, без самих подборок."""
    q = ("SELECT * FROM posts WHERE status='published' AND channel_msg_id IS NOT NULL "
         "AND COALESCE(source,'')!='digest'")
    args: list = []
    if days:
        q += " AND decided_at>=?"
        args.append(db.days_ago(days))
    q += " ORDER BY decided_at DESC LIMIT ?"
    args.append(limit)
    async with db.connect() as c:
        cur = await c.execute(q, tuple(args))
        return await cur.fetchall()


def short(post, limit: int = 70) -> str:
    """Название и автор — без города и года, чтобы строка списка не расползалась."""
    data = json.loads(post["data"])
    parts = [str(p).strip() for p in (data.get("headline_parts") or []) if p and str(p).strip().lower() != "null"]
    h = " // ".join(parts[:2]) or data.get("headline") or "без заголовка"
    return h if len(h) <= limit else h[: limit - 1] + "…"


async def _cover(bot: Bot, post, dest: Path) -> tuple[bool, str | None]:
    """Обложку поста — в папку подборки: копией с диска или скачав из Telegram по file_id.
    → (файл на месте, file_id)"""
    plan = cards.photo_plan(post)
    if not plan:
        return False, None
    i = plan[0]
    images = json.loads(post["images"] or "[]")
    fid = cards._fids(post).get(str(i))
    src = Path(images[i]) if i < len(images) else None
    try:
        if src and src.exists():
            shutil.copyfile(src, dest)
            return True, fid
        if fid:
            await bot.download(fid, destination=dest)
            return dest.exists(), fid
    except Exception:
        log.warning("Подборка: не взял обложку поста %s", post["id"], exc_info=True)
    return False, fid


async def _make(bot: Bot, title: str, intro: str, posts: list, tag: str, kind: str) -> int | None:
    """Собирает пост-подборку и кладёт его во входящие. → id поста или None, если набралось меньше MIN_ITEMS."""
    folder = config.IMG_DIR / f"g{int(time.time())}"
    folder.mkdir(parents=True, exist_ok=True)
    head = [f"<b>{html.escape(title, quote=False)}</b>"] + ([html.escape(intro, quote=False)] if intro else [])
    lines, images, fids, ids = [], [], {}, []
    for p in posts:
        link = cards.post_link(p)
        if not link:
            continue
        line = f'{len(lines) + 1}. <a href="{html.escape(link)}">{html.escape(short(p), quote=False)}</a>'
        candidate = "\n\n".join(head + ["\n".join(lines + [line]), tag])
        if len(lines) >= MAX_ITEMS or formatter.visible_len(candidate) > config.CAPTION_LIMIT:
            break
        lines.append(line)
        ids.append(p["id"])
        dest = folder / f"{len(images):02d}.jpg"
        ok, fid = await _cover(bot, p, dest)
        if ok or fid:
            images.append(str(dest))
            if fid:
                fids[str(len(images) - 1)] = fid
    if len(lines) < MIN_ITEMS:
        shutil.rmtree(folder, ignore_errors=True)
        return None
    caption = "\n\n".join(head + ["\n".join(lines), tag])
    flags = [FLAG]
    hits = voice.check(intro, await voice.banned()) if intro else []
    if hits:
        flags.append("штамп во вступлении: " + ", ".join(hits))
    data = {"headline": title, "headline_parts": [title], "flags": flags,
            "_digest": kind, "_items": ids, "_source_text": ""}
    pid = await db.add_post(candidate_id=None, source="digest", url="", category="notes", format="notes",
                            data=data, caption=caption, score=0, reason=intro or title,
                            images=images, status="ready")
    if fids:
        await db.update_post(pid, file_ids=fids)
    await slots.propose(pid, None)
    log.info("Подборка %s: пост %s, пунктов %s", kind, pid, len(lines))
    return pid


# ---------- неделя ----------

def _span(start, end) -> str:
    if start.month == end.month:
        return f"{start.day}–{end.day} {MONTHS[end.month - 1]}"
    return f"{start.day} {MONTHS[start.month - 1]} – {end.day} {MONTHS[end.month - 1]}"


async def weekly(bot: Bot) -> tuple[int | None, str]:
    """Сначала большие посты и заметки, потом мини с лучшей оценкой; в списке — по порядку выхода."""
    posts = await published(days=7)
    if len(posts) < MIN_ITEMS:
        return None, f"за неделю вышло {len(posts)} постов — для подборки мало"
    big = [p for p in posts if p["format"] in ("std", "notes")]
    mini = sorted((p for p in posts if p["format"] == "mini"), key=lambda p: -(p["score"] or 0))
    chosen = sorted((big + mini)[:MAX_ITEMS], key=lambda p: p["decided_at"] or "")
    now = slots._now()
    title = f"AHMAG // неделя // {_span(now - timedelta(days=6), now)}"
    pid = await _make(bot, title, "", chosen, WEEK_TAG, "week")
    return pid, ("ok" if pid else "не набралось постов со ссылкой на канал")


# ---------- темы ----------

THEMES_SYSTEM = f"""Ты помогаешь автору Telegram-канала AHMAG (архитектура, искусство, фотография, архив, кино) собрать тематические подборки из уже вышедших постов. Подборка — пост со списком ссылок на старые посты, объединённые одной темой.

Хорошая тема конкретная, и её не видно сразу: страна или город, материал, эпоха, тип здания, приём, мотив, один автор. «Дерево и бумага в японских домах», «советский модернизм в Средней Азии», «кирпич, который держит здание». Плохая тема — рубрика целиком: «архитектура», «красивые здания», «искусство».

{voice.RULES}

Верни ТОЛЬКО JSON без пояснений:
{{"themes": [{{"title": "...", "intro": "...", "ids": [12, 40, 41]}}]}}"""


async def propose_themes() -> tuple[list[dict], str]:
    pool = await published(limit=300)
    if len(pool) < MIN_POOL:
        return [], f"вышло {len(pool)} постов, нужно хотя бы {MIN_POOL}"
    rows = []
    for p in pool:
        tags = " ".join(json.loads(p["data"]).get("tags") or [])
        rows.append(f"{p['id']} | {json.loads(p['data']).get('headline') or short(p)} | {p['category']} | {tags}")
    used = await db.get_setting("g_themes_used", [])
    content = ("# Вышедшие посты: id | заголовок | рубрика | теги\n" + "\n".join(rows) + "\n\n"
               + ("# Такие подборки уже были, не повторяй\n" + "\n".join(used[-30:]) + "\n\n" if used else "")
               + f"Предложи до 4 тем. В каждой от {MIN_ITEMS} до {MAX_ITEMS} постов, id — только из списка. "
               "title — до 50 знаков, строчными, как заголовок подборки в канале. "
               "intro — одна простая фраза до 140 знаков о том, что объединяет посты, или пустая строка.")
    data = await curator._call(content, system=THEMES_SYSTEM, model=config.CLAUDE_MODEL, max_tokens=2000)
    valid = {p["id"] for p in pool}
    themes = []
    for t in data.get("themes") or []:
        ids = []
        for raw in t.get("ids") or []:
            s = str(raw).strip()
            if s.isdigit() and int(s) in valid and int(s) not in ids:
                ids.append(int(s))
        title = str(t.get("title") or "").strip()
        if title and len(ids) >= MIN_ITEMS:
            themes.append({"title": title[:60], "intro": str(t.get("intro") or "").strip()[:160],
                           "ids": ids[:MAX_ITEMS]})
    await db.set_setting("g_themes", themes)
    return themes, ("ok" if themes else "Claude не нашёл тем, где набирается хотя бы 4 поста")


async def build_theme(bot: Bot, i: int) -> tuple[int | None, str]:
    themes = await db.get_setting("g_themes", [])
    if not 0 <= i < len(themes):
        return None, "этой темы уже нет в списке"
    t = themes[i]
    posts = [p for p in [await db.get_post(pid) for pid in t["ids"]] if p and p["status"] == "published"]
    pid = await _make(bot, f"Подборка // {t['title']}", t.get("intro", ""), posts, THEME_TAG, "theme")
    if not pid:
        return None, "постов со ссылкой на канал набралось меньше четырёх"
    themes.pop(i)
    await db.set_setting("g_themes", themes)
    await db.set_setting("g_themes_used", (await db.get_setting("g_themes_used", []) + [t["title"]])[-60:])
    return pid, "ok"
