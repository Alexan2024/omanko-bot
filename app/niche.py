"""Нишевые источники и чтение архивов вглубь.

  архитектура — Hidden Architecture, Drawing Matter, Afasia, Inigo (исторические дома),
                Library of Congress HABS/HAER (обмеры и фото, public domain);
  архив       — The Public Domain Review, Wellcome Collection, Europeana (сотни европейских
                коллекций, в том числе Rijksmuseum и Deutsche Fotothek), Are.na;
  фотография  — American Suburb X;
  кино        — Film-Grab (кадры, только мини), MUBI Notebook.

Вглубь: у сайтов на WordPress каждый сбор берёт свежие записи и одну случайную страницу из всего
архива (через их открытый API), у Public Domain Review — случайные записи из последней сотни.

Сборщики отсюда стоят в sources.COLLECTORS наравне с RSS и музеями. Правила «находок» (слот,
запас) — в pipeline.pick_next и slots, экран и /arena — в finds.py."""
import html
import logging
import os
import random
import re
import urllib.parse

import httpx

from app import config, db

log = logging.getLogger(__name__)

API_UA = "AHMAG-bot/4.0 (+https://t.me/ahmag; curation bot)"   # loc.gov не пускает браузерные UA без JS

# ======================= источники =======================

WP_SITES = {   # имя → адрес сайта на WordPress: свежие записи + случайная страница архива
    "hidden": "https://hiddenarchitecture.net",
    "drawingmatter": "https://drawingmatter.org",
    "afasia": "https://afasiaarchzine.com",
    "asx": "https://americansuburbx.com",
    "socks": "https://socks-studio.com",      # лента socks уже есть — здесь только архив
}
NEW_FEEDS = {
    "pdr": "https://publicdomainreview.org/rss.xml",
    "inigo": "https://inigo.com/feed/",
    "mubi": "https://mubi.com/notebook/posts.rss",
}
API_SOURCES = ("filmgrab", "habs", "wellcome", "europeana", "arena")

LABELS = {
    "hidden": "Hidden Architecture", "drawingmatter": "Drawing Matter", "afasia": "Afasia",
    "asx": "American Suburb X", "socks": "Socks Studio", "pdr": "The Public Domain Review",
    "inigo": "Inigo", "mubi": "MUBI Notebook", "filmgrab": "Film-Grab",
    "habs": "Library of Congress · HABS", "wellcome": "Wellcome Collection",
    "europeana": "Europeana", "arena": "Are.na",
}
HINTS = {
    "hidden": "архитектура, забытые здания", "drawingmatter": "архитектурные чертежи, история архитектуры",
    "afasia": "архитектура небольших бюро", "asx": "фотография, история фотографии",
    "pdr": "архив, старые изображения", "inigo": "исторические дома и интерьеры", "mubi": "кино",
    "filmgrab": "кино, кадры из фильма", "habs": "архитектура, обмеры и фото исторических зданий",
    "wellcome": "архив, научная графика", "europeana": "музейный и архивный объект",
    "arena": "находка из подборки Are.na",
}
FINDS = {"hidden", "drawingmatter", "afasia", "asx", "socks", "pdr", "inigo",
         "filmgrab", "habs", "wellcome", "europeana", "arena"}
MINI_ONLY = {"filmgrab"}          # плюс блоки Are.na без страницы-источника
META_INTRO = {
    "filmgrab": "Кадры из фильма, архив Film-Grab.",
    "habs": "Документация Historic American Buildings Survey / HAER, Библиотека Конгресса США (public domain).",
    "wellcome": "Изображение из открытой коллекции Wellcome Collection (Лондон).",
    "europeana": "Объект из открытых коллекций Europeana.",
    "arena": "Изображение из подборки на Are.na. Кроме названия и описания ниже фактов нет — "
             "не додумывай автора, место и дату.",
}

WP_LATEST, WP_DEEP = 4, 5          # записей за сбор: свежих и из случайной страницы архива
PDR_LATEST, PDR_DEEP = 3, 6
API_PER_RUN = int(os.getenv("FINDS_API_PER_RUN", "2"))   # объектов за сбор у каждого API
ARENA_PER_RUN = int(os.getenv("ARENA_PER_RUN", "6"))
EUROPEANA_KEY = os.getenv("EUROPEANA_KEY", "api2demo")
CINEMA_SOURCES = {"filmgrab", "cinephilia"}      # у кадров из фильмов пропорции шире
CINEMA_MAX_RATIO = float(os.getenv("CINEMA_MAX_RATIO", "2.45"))   # обычная проверка — 2.2
CINEMA_MIN_SHORT = int(os.getenv("CINEMA_MIN_SHORT", "500"))      # 1280×536 проходит; обычная проверка — 700
FIND_RESERVE = 2                   # столько находок держим для слота находки
FIND_STOCK = 4                     # меньше — при оценке добираем материалы из нишевых источников

HABS_QUERIES = ["Frank Lloyd Wright", "Schindler", "Neutra", "Mies van der Rohe", "Louis Kahn", "Eames",
                "Shaker", "lighthouse", "grain elevator", "observatory", "adobe", "mission church",
                "covered bridge", "barn", "courthouse", "library", "synagogue", "water tower", "factory",
                "greenhouse", "pueblo", "plantation house", "Victorian house", "round barn", "bank",
                "theater", "chapel", "mill", "fort", "Greek Revival"]
WELLCOME_QUERIES = ["architectural drawing", "botanical illustration", "astronomy", "celestial map",
                    "Japanese woodblock", "alchemy", "Persian manuscript", "garden", "ornament", "diagram",
                    "crystal", "shells", "costume", "Chinese painting", "observatory", "cosmology"]
EUROPEANA_QUERIES = ["Architekturfotografie", "architectural photograph", "Bauhaus", "modernism building",
                     "interior photograph", "Werkbund", "brutalism", "architectural drawing",
                     "Hans Finsler", "Albert Renger-Patzsch", "Kurt Hielscher", "Carl Blossfeldt",
                     "villa", "staircase", "church interior", "factory building", "bridge photograph",
                     "street photograph", "Japanese print", "still life photograph"]
ARENA_DEFAULT = ["architecture-drawings-and-speculations", "architecture-drawing-i-like",
                 "interior-architecture-art-product", "type-monastery", "tropical-modernism-geoffrey-bawa",
                 "ruins-archive", "cold-ruins", "heavy-focus", "pastoral-brutalism", "interiors-studios",
                 "photobook-5rge5873ke0", "exhibition-design-8ztvkq3su_u"]




def _clean(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", text or "")).strip()


async def _off() -> set[str]:
    return set(await db.get_setting("disabled_sources", []))


# ---------- WordPress: свежее + случайная страница архива ----------

async def _wp_page(client: httpx.AsyncClient, base: str, page: int, per: int) -> tuple[list[dict], int]:
    r = await client.get(f"{base}/wp-json/wp/v2/posts", timeout=40,
                         params={"per_page": per, "page": page, "_fields": "link,title,content"})
    r.raise_for_status()
    return r.json(), int(r.headers.get("x-wp-totalpages") or 1)


async def _wp_posts(client: httpx.AsyncClient, base: str, latest: int, deep: int) -> list[dict]:
    first, pages = await _wp_page(client, base, 1, latest)
    out = list(first)
    deep_pages = pages * latest // deep            # число страниц при размере deep
    if deep_pages > 1:
        more, _ = await _wp_page(client, base, random.randint(2, deep_pages), deep)
        out += more
    return out


async def collect_wp(client: httpx.AsyncClient) -> list[dict]:
    off, items = await _off(), []
    for name, base in WP_SITES.items():
        if name in off:
            continue
        try:
            posts = await (_wp_socks(client, base) if name == "socks" else _wp_posts(client, base, WP_LATEST, WP_DEEP))
        except Exception as exc:
            log.warning("WP %s: %s", name, exc)
            continue
        for p in posts:
            title = _clean((p.get("title") or {}).get("rendered", ""))
            if p.get("link") and title and len((p.get("content") or {}).get("rendered") or "") > 200:
                items.append({"url": p["link"], "title": title, "source": name,
                              "payload": {"content_html": ((p.get("content") or {}).get("rendered") or "")[:200_000]}})
    return items


async def _wp_socks(client: httpx.AsyncClient, base: str) -> list[dict]:
    _, pages = await _wp_page(client, base, 1, WP_DEEP)
    if pages < 2:
        return []
    posts, _ = await _wp_page(client, base, random.randint(2, pages), WP_DEEP)
    return posts


# ---------- Film-Grab: кадры из фильмов ----------

def _filmgrab_item(p: dict) -> dict | None:
    c = (p.get("content") or {}).get("rendered") or ""
    frames = [u for u in dict.fromkeys(re.findall(r"https?://film-grab\.com/wp-content/uploads/photo-gallery/"
                                                   r"[^\"'\s?]+\.jpe?g", c)) if "/thumb/" not in u]
    if len(frames) < 4:
        return None
    step = max(1, len(frames) // 16)
    frames = frames[::step][:16]
    meta = {"title": _clean((p.get("title") or {}).get("rendered", ""))}
    for label, key in (("Director", "director"), ("Director of Photography", "cinematography"),
                       ("Production Design", "production_design"), ("Year", "year")):
        m = re.search(rf"<p>\s*{label}:\s*(.+?)</p>", c, re.S)
        if m:
            meta[key] = _clean(m.group(1))
    return {"url": p["link"], "title": meta["title"], "source": "filmgrab",
            "payload": {"images": frames, "meta": meta}}


async def collect_filmgrab(client: httpx.AsyncClient) -> list[dict]:
    if "filmgrab" in await _off():
        return []
    try:
        posts = await _wp_posts(client, "https://film-grab.com", 2, 3)
    except Exception as exc:
        log.warning("Film-Grab: %s", exc)
        return []
    return [it for it in map(_filmgrab_item, posts) if it]


# ---------- The Public Domain Review: вся сотня записей ленты ----------

async def collect_pdr(client: httpx.AsyncClient) -> list[dict]:
    if "pdr" in await _off():
        return []
    import feedparser
    try:
        r = await client.get(NEW_FEEDS["pdr"], timeout=40)
        r.raise_for_status()
        entries = feedparser.parse(r.content).entries
    except Exception as exc:
        log.warning("PDR: %s", exc)
        return []
    pick = entries[:PDR_LATEST] + random.sample(entries[PDR_LATEST:], min(PDR_DEEP, max(0, len(entries) - PDR_LATEST)))
    out = []
    for e in pick:
        body = (e.get("content") or [{}])[0].get("value", "") or e.get("summary", "")
        if e.get("link"):
            out.append({"url": e["link"], "title": e.get("title", ""), "source": "pdr",
                        "payload": {"content_html": body[:200_000]}})
    return out


# ---------- Library of Congress: HABS/HAER ----------

def _loc_iiif(tif_url: str) -> str | None:
    m = re.search(r"/storage-services/master/(.+)\.tif$", tif_url or "")
    return (f"https://tile.loc.gov/image-services/iiif/master:{m.group(1).replace('/', ':')}/full/full/0/default.jpg"
            if m else None)


async def collect_habs(client: httpx.AsyncClient) -> list[dict]:
    if "habs" in await _off() or API_PER_RUN <= 0:
        return []
    items = []
    async with httpx.AsyncClient(headers={"User-Agent": API_UA}, follow_redirects=True, timeout=40) as loc:
        for q in random.sample(HABS_QUERIES, 2):
            try:
                r = await loc.get("https://www.loc.gov/collections/historic-american-buildings-landscapes-"
                                  "and-engineering-records/", params={"q": q, "fo": "json", "c": 25})
                r.raise_for_status()
                results = [x for x in r.json().get("results") or [] if _habs_photos(x) >= 3]
                random.shuffle(results)
                for x in results[:3]:
                    it = await _habs_item(loc, x)
                    if it:
                        items.append(it)
                    if len(items) >= API_PER_RUN:
                        return items
            except Exception as exc:
                log.warning("HABS %s: %s", q, exc)
    return items


def _habs_photos(x: dict) -> int:
    m = re.search(r"Photo\(s\):\s*(\d+)", " ".join(x.get("description") or []))
    return int(m.group(1)) if m else 0


async def _habs_item(loc: httpx.AsyncClient, x: dict) -> dict | None:
    url = x.get("url") or x.get("id")
    if not url:
        return None
    d = (await loc.get(url.replace("http://", "https://"), params={"fo": "json"})).json()
    photos, sheets = [], []
    for res in d.get("resources") or []:
        cap = (res.get("caption") or "").lower()
        for f in res.get("files") or []:
            tif = next((v.get("url") for v in f if (v.get("mimetype") or "").endswith("tiff")), None)
            iiif = _loc_iiif(tif)
            if iiif:
                (photos if "photo" in cap else sheets if "drawing" in cap else []).append(iiif)
    if len(photos) < 1:
        return None
    it = d.get("item") or {}
    date = it.get("created_published") or it.get("date") or ""
    meta = {"title": it.get("title") or x.get("title"), "date": "; ".join(date) if isinstance(date, list) else date,
            "notes": "; ".join((it.get("notes") or [])[:6])[:900], "summary": "; ".join(it.get("summary") or [])[:900],
            "location": ", ".join((x.get("location") or [])[:4]), "survey": "HABS/HAER, Library of Congress"}
    return {"url": url.replace("http://", "https://"), "title": meta["title"] or "", "source": "habs",
            "payload": {"images": (photos[:8] + sheets[:2])[:config.EVAL_PHOTOS], "meta": meta}}


# ---------- Wellcome Collection ----------

async def collect_wellcome(client: httpx.AsyncClient) -> list[dict]:
    if "wellcome" in await _off() or API_PER_RUN <= 0:
        return []
    items = []
    for q in random.sample(WELLCOME_QUERIES, 2):
        try:
            r = await client.get("https://api.wellcomecollection.org/catalogue/v2/images",
                                 params={"query": q, "pageSize": 30}, timeout=40)
            rows = r.json().get("results") or []
            random.shuffle(rows)
            for im in rows:
                loc = next((l for l in im.get("locations") or [] if "iiif" in (l.get("url") or "")), None)
                src = im.get("source") or {}
                if not loc or not src.get("id"):
                    continue
                w = (await client.get(f"https://api.wellcomecollection.org/catalogue/v2/works/{src['id']}",
                                      params={"include": "contributors,production,notes"}, timeout=40)).json()
                meta = {"title": w.get("title") or src.get("title"),
                        "contributors": "; ".join((c.get("agent") or {}).get("label", "") for c in w.get("contributors") or []),
                        "date": "; ".join(p.get("label", "") for pr in w.get("production") or [] for p in pr.get("dates") or []),
                        "description": _clean(w.get("description") or "")[:1200],
                        "license": ((loc.get("license") or {}).get("label")), "credit": loc.get("credit")}
                items.append({"url": f"https://wellcomecollection.org/works/{src['id']}", "title": meta["title"] or "",
                              "source": "wellcome",
                              "payload": {"images": [loc["url"].replace("/info.json", "/full/!2400,2400/0/default.jpg")],
                                          "meta": meta}})
                if len(items) >= API_PER_RUN:
                    return items
        except Exception as exc:
            log.warning("Wellcome %s: %s", q, exc)
    return items


# ---------- Europeana ----------

async def collect_europeana(client: httpx.AsyncClient) -> list[dict]:
    if "europeana" in await _off() or API_PER_RUN <= 0:
        return []
    items = []
    for q in random.sample(EUROPEANA_QUERIES, 2):
        try:
            r = await client.get("https://api.europeana.eu/record/v2/search.json", timeout=40, params={
                "wskey": EUROPEANA_KEY, "query": q, "rows": 40, "start": 1,
                "reusability": "open", "media": "true", "profile": "rich",
                "qf": ["TYPE:IMAGE", "IMAGE_SIZE:extra_large"]})
            rows = r.json().get("items") or []
            random.shuffle(rows)
            for x in rows:
                img = (x.get("edmIsShownBy") or [None])[0]
                if not img or not x.get("guid"):
                    continue
                first = lambda k: ((x.get(k) or [""])[0] or "")
                meta = {"title": first("title"), "creator": first("dcCreator"), "year": first("year"),
                        "provider": first("dataProvider"), "country": first("country"),
                        "description": _clean(first("dcDescription"))[:1200], "rights": first("rights")}
                items.append({"url": x["guid"].split("?")[0], "title": meta["title"], "source": "europeana",
                              "payload": {"images": [img], "meta": meta}})
                if len(items) >= API_PER_RUN:
                    return items
        except Exception as exc:
            log.warning("Europeana %s: %s", q, exc)
    return items


# ---------- Are.na ----------

BAD_DOM = re.compile(r"instagram|facebook|pinterest|twimg|twitter|x\.com|tumblr|gstatic|google\.|blogspot|"
                     r"amazonaws|cloudfront|imgur|reddit|youtube|vimeo|are\.na|wp\.com|squarespace-cdn|cdn", re.I)
FILEISH = re.compile(r"^(img|dsc|photo|image|screen ?shot|bildschirmfoto|untitled|download|[0-9a-f_\-]{8,})|"
                     r"\.(jpe?g|png|webp|gif)\b|^@|^\(\d+\)", re.I)


def _good_title(t: str) -> bool:
    t = (t or "").strip()
    return len(t) >= 6 and not FILEISH.search(t) and len(re.findall(r"[^\W\d_]{3,}", t)) >= 2


def _page_src(b: dict) -> str:
    u = ((b.get("source") or {}).get("url") or "").strip()
    dom = urllib.parse.urlparse(u).netloc
    if not u.startswith("http") or not dom or BAD_DOM.search(dom) or re.search(r"\.(jpe?g|png|webp|gif)(\?|$)", u, re.I):
        return ""
    return u


async def arena_channels() -> list[str]:
    return list(await db.get_setting("arena_channels", ARENA_DEFAULT))


def _arena_item(b: dict, slug: str) -> dict | None:
    img = ((b.get("image") or {}).get("original") or {}).get("url")
    if not img:
        return None
    title, desc = _clean(b.get("title") or ""), _clean(b.get("description") or "")[:800]
    page = _page_src(b)
    if page:
        return {"url": page, "title": title or page, "source": "arena",
                "payload": {"content_html": f"<p>{html.escape(title)}. {html.escape(desc)}</p><img src=\"{img}\">"}}
    if not _good_title(title):
        return None
    meta = {"title": title, "description": desc, "arena_channel": slug,
            "connected_by": ((b.get("connected_by_username") or (b.get("user") or {}).get("full_name")) or "")}
    return {"url": f"https://www.are.na/block/{b['id']}", "title": title, "source": "arena",
            "payload": {"images": [img], "meta": meta}}


async def collect_arena(client: httpx.AsyncClient) -> list[dict]:
    if "arena" in await _off() or ARENA_PER_RUN <= 0:
        return []
    chans = await arena_channels()
    items = []
    for slug in random.sample(chans, min(3, len(chans))):
        try:
            head = (await client.get(f"https://api.are.na/v2/channels/{slug}/thumb", timeout=30)).json()
            n = int(head.get("length") or 0)
            page = random.randint(1, max(1, n // 40))
            r = await client.get(f"https://api.are.na/v2/channels/{slug}/contents", timeout=40,
                                 params={"per": 40, "page": page, "direction": "desc", "sort": "position"})
            blocks = [b for b in r.json().get("contents") or [] if b.get("class") == "Image"]
            random.shuffle(blocks)
            got = [it for it in (_arena_item(b, slug) for b in blocks) if it][:max(1, ARENA_PER_RUN // 3)]
            items += got
        except Exception as exc:
            log.warning("Are.na %s: %s", slug, exc)
    return items[:ARENA_PER_RUN]


SOURCE_NAMES = [*WP_SITES, *NEW_FEEDS, *API_SOURCES]   # для экрана «Источники»

COLLECTORS = [collect_wp, collect_filmgrab, collect_pdr, collect_habs, collect_wellcome,
              collect_europeana, collect_arena]


# записи WordPress, закрытые паролем, — мимо (sources.prefilter)
PROTECTED = re.compile(r"^\s*(protected|private)\s*:", re.I)
