"""Сбор кандидатов из источников. Каждый источник возвращает список словарей
{url, title, source, payload}. Добавить RSS можно из меню бота (📡 Источники)
или строкой в FEEDS; другой тип источника — функцией в COLLECTORS.
Нишевые источники и чтение архивов вглубь — в app/niche.py, их сборщики стоят здесь же, в COLLECTORS."""
import asyncio
import logging
import random
import re

import feedparser
import httpx

from app import config, db, niche

log = logging.getLogger(__name__)

# Имя → адрес ленты. Все проверены: отдают текст и фото не меньше 1200 px.
FEEDS = {
    # архитектура и интерьеры
    "archdaily": "https://feeds.feedburner.com/Archdaily",
    "dezeen": "https://www.dezeen.com/architecture/feed/",
    "designboom": "https://www.designboom.com/architecture/feed/",
    "leibal": "https://leibal.com/feed/",
    # искусство
    "colossal": "https://www.thisiscolossal.com/feed/",
    "booooooom": "https://www.booooooom.com/feed/",
    "hyperallergic": "https://hyperallergic.com/feed/",
    "ignant": "https://www.ignant.com/feed/",
    # фотография
    "aperture": "https://aperture.org/feed/",
    "featureshoot": "https://www.featureshoot.com/feed/",
    # архив и визуальная культура
    "flashbak": "https://flashbak.com/feed/",
    "messynessy": "https://www.messynessychic.com/feed/",
    "socks": "https://socks-studio.com/feed/",
    # кино
    "cinephilia": "https://cinephiliabeyond.org/feed/",
    # нишевые ленты (остальные нишевые источники читаются через API — app/niche.py)
    "inigo": niche.NEW_FEEDS["inigo"],
    "mubi": niche.NEW_FEEDS["mubi"],
}

# Подсказка для первичного фильтра: о чём обычно пишет источник
HINTS = {
    "archdaily": "архитектура", "dezeen": "архитектура", "designboom": "архитектура", "leibal": "архитектура",
    "colossal": "искусство", "booooooom": "искусство и фото", "hyperallergic": "искусство, много новостей",
    "ignant": "искусство, фото, дизайн", "aperture": "фотография", "featureshoot": "фотография",
    "flashbak": "архив, старые фото", "messynessy": "архив, истории", "socks": "архив, история архитектуры",
    "cinephilia": "кино", "met": "музейный предмет", "cma": "музейный предмет",
    **niche.HINTS,
}

# Темы для музейного open access — под профиль канала
MUSEUM_QUERIES = [
    "architecture photograph", "architectural drawing", "Japanese woodblock print",
    "ukiyo-e landscape", "surrealism", "street photograph", "Walker Evans",
    "Eugène Atget", "Berenice Abbott", "illuminated manuscript", "book of hours",
    "Hiroshige", "Hokusai", "Giorgio de Chirico", "Bauhaus", "modernist design",
    "cat", "Egyptian temple", "Persian tile", "Roman ruins photograph",
    "Charles Marville", "Linnaeus Tripe", "temple photograph", "Kuniyoshi",
]

# Явно не наше — отсекается по заголовку, без обращения к Claude
STOP_TITLE = re.compile(
    r"\b(render(?:s|ing)?|visuali[sz]ation|competition|shortlist(?:ed)?|masterplan|proposal|proposed|"
    r"unveils? (?:plans|design|designs|proposal)|skyscraper|supertall|high-rise|office tower|"
    r"podcast|webinar|sponsored|partner content|job|jobs|vacanc\w*|call for (?:entries|submissions)|"
    r"dezeen (?:agenda|debate|weekly|awards)|newsletter|giveaway|sale|deal|deals|"
    r"review:? .*season|trailer|box office)\b", re.I)
LISTICLE = re.compile(r"^(?:top\s+)?\d{1,3}\s+\S", re.I)  # «10 домов…», «13 Things…» — подборки, а не одна работа


def prefilter(title: str) -> str | None:
    """Причина отсева по заголовку или None."""
    t = (title or "").strip()
    if not t:
        return None
    if niche.PROTECTED.search(t):
        return "запись закрыта паролем"
    if LISTICLE.search(t):
        return "подборка"
    m = STOP_TITLE.search(t)
    return f"стоп-слово «{m.group(1)}»" if m else None


async def all_feeds() -> dict[str, str]:
    """Встроенные RSS + добавленные из бота."""
    return {**FEEDS, **(await db.get_setting("custom_feeds", {}))}


async def disabled() -> set[str]:
    return set(await db.get_setting("disabled_sources", []))


async def source_names() -> list[str]:
    names = [*(await all_feeds()), *db.MUSEUMS]
    return names + [n for n in niche.SOURCE_NAMES if n not in names]


async def _get(client: httpx.AsyncClient, url: str, **kw) -> httpx.Response:
    r = await client.get(url, timeout=30, follow_redirects=True, **kw)
    r.raise_for_status()
    return r


async def collect_rss(client: httpx.AsyncClient) -> list[dict]:
    off = await disabled()

    async def one(name: str, url: str) -> list[dict]:
        if name in off:
            return []
        try:
            r = await _get(client, url)
            feed = feedparser.parse(r.content)
        except Exception as exc:  # источник упал — не роняем весь сбор
            log.warning("RSS %s: %s", name, exc)
            return []
        items = []
        for e in feed.entries[:25]:
            link = e.get("link")
            if link:
                content = e.get("content") or []
                body = content[0].get("value", "") if content else e.get("summary", "")
                items.append({"url": link, "title": e.get("title", ""), "source": name,
                              "payload": {"content_html": body[:200_000]}})
        return items

    groups = await asyncio.gather(*(one(n, u) for n, u in (await all_feeds()).items()))
    return [it for g in groups for it in g]


async def check_feed(url: str) -> int:
    """Сколько записей отдаёт RSS — проверка перед добавлением."""
    async with httpx.AsyncClient(headers={"User-Agent": config.USER_AGENT}) as client:
        r = await _get(client, url)
    return len(feedparser.parse(r.content).entries)


async def collect_met(client: httpx.AsyncClient) -> list[dict]:
    if "met" in await disabled() or config.MUSEUM_PER_RUN <= 0:
        return []
    base = "https://collectionapi.metmuseum.org/public/collection/v1"
    items = []
    for q in random.sample(MUSEUM_QUERIES, 2):
        try:
            r = await _get(client, f"{base}/search", params={"q": q, "hasImages": "true"})
            ids = (r.json().get("objectIDs") or [])[:300]
            for oid in random.sample(ids, min(config.MUSEUM_PER_RUN * 3, len(ids))):
                o = (await _get(client, f"{base}/objects/{oid}")).json()
                if not (o.get("isPublicDomain") and o.get("primaryImage")):
                    continue
                images = [o["primaryImage"], *o.get("additionalImages", [])][: config.EVAL_PHOTOS]
                items.append({
                    "url": o.get("objectURL") or f"https://www.metmuseum.org/art/collection/search/{oid}",
                    "title": o.get("title", ""),
                    "source": "met",
                    "payload": {
                        "images": images,
                        "meta": {k: o.get(k) for k in (
                            "title", "artistDisplayName", "artistDisplayBio", "objectDate",
                            "medium", "culture", "country", "city", "department",
                            "classification", "creditLine", "objectName",
                        )},
                    },
                })
                if len(items) >= config.MUSEUM_PER_RUN:
                    return items
                await asyncio.sleep(0.3)
        except Exception as exc:
            log.warning("Met %s: %s", q, exc)
    return items


async def collect_cma(client: httpx.AsyncClient) -> list[dict]:
    """Cleveland Museum of Art, открытая коллекция (CC0), фото до ~3400 px."""
    if "cma" in await disabled() or config.MUSEUM_PER_RUN <= 0:
        return []
    items = []
    for q in random.sample(MUSEUM_QUERIES, 2):
        try:
            r = await _get(client, "https://openaccess-api.clevelandart.org/api/artworks/",
                           params={"q": q, "cc0": "", "has_image": 1, "limit": 40})
            rows = r.json().get("data") or []
            random.shuffle(rows)
            for o in rows:
                img = ((o.get("images") or {}).get("print") or {}).get("url")
                if not img:
                    continue
                creators = "; ".join(c.get("description", "") for c in (o.get("creators") or []) if c.get("description"))
                items.append({
                    "url": o.get("url") or f"https://www.clevelandart.org/art/{o.get('accession_number', o.get('id'))}",
                    "title": o.get("title", ""),
                    "source": "cma",
                    "payload": {
                        "images": [img],
                        "meta": {
                            "title": o.get("title"), "creators": creators, "date": o.get("creation_date"),
                            "culture": ", ".join(o.get("culture") or []), "technique": o.get("technique"),
                            "type": o.get("type"), "department": o.get("department"),
                            "description": (o.get("description") or "")[:1200],
                            "did_you_know": o.get("did_you_know"),
                        },
                    },
                })
                if len(items) >= config.MUSEUM_PER_RUN:
                    return items
        except Exception as exc:
            log.warning("CMA %s: %s", q, exc)
    return items


COLLECTORS = [collect_rss, collect_met, collect_cma, *niche.COLLECTORS]


async def collect_all() -> tuple[int, int]:
    """→ (новых кандидатов, из них отсеяно по заголовку)."""
    added = dropped = 0
    async with httpx.AsyncClient(headers={"User-Agent": config.USER_AGENT}) as client:
        for fn in COLLECTORS:
            try:
                found = await fn(client)
            except Exception as exc:
                log.warning("Сборщик %s: %s", fn.__name__, exc)
                continue
            for it in found:
                cid = await db.add_candidate(it["url"], it["source"], it["title"], it["payload"])
                if not cid:
                    continue
                added += 1
                why = prefilter(it["title"])
                if why:
                    await db.mark_candidate(cid, "skipped", f"по заголовку: {why}")
                    dropped += 1
    log.info("Новых кандидатов: %s, отсеяно по заголовку: %s", added, dropped)
    return added, dropped
