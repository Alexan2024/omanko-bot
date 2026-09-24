"""Отметки в Instagram: аккаунты бюро или архитектора, художника, фотографа и издания-источника.
Где ищем, по убыванию надёжности:
1. ссылка в кредитах поста сама ведёт на Instagram;
2. сайты из кредитов и из статьи (ссылки, чей текст совпадает с именами) — их Instagram из шапки и подвала;
3. главная страница издания-источника — её Instagram идёт в «via»;
4. если по имени ничего не нашлось — поиск в интернете (Claude Haiku, до двух запросов), только аккаунты,
   которые он увидел в результатах как ссылки instagram.com.
Сопоставление имён и найденных аккаунтов делает Claude и выбирает только из найденного, не придумывая.
Проверить аккаунт через API с входом через Instagram нельзя, поэтому метка на фото, которую Instagram
не принял, снимается, а пост уходит без неё."""
import json
import logging
import os
import re
import time
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app import config, curator, db, formatter

log = logging.getLogger(__name__)

SEARCH = os.getenv("IG_TAG_SEARCH", "1") == "1"
IG_RE = re.compile(r"instagram\.com/([A-Za-z0-9._]{2,30})(?![A-Za-z0-9._])", re.I)
NOT_USERS = {"p", "reel", "reels", "explore", "stories", "accounts", "tv", "share", "direct", "about",
             "developer", "legal", "web", "instagram", "static", "embed", "sharer", "privacy"}
HEADERS = {"User-Agent": config.USER_AGENT, "Accept-Language": "en"}
SITE_TTL = 30 * 86400
ROLES = ("author", "photographer")


def _clean(h: str) -> str | None:
    h = h.strip().strip(".").lower()
    if not h or h in NOT_USERS or not re.fullmatch(r"[a-z0-9._]{2,30}", h):
        return None
    return h


def _from_html(html: str) -> list[str]:
    return list(dict.fromkeys(h for h in (_clean(m) for m in IG_RE.findall(html or "")) if h))


async def _get(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        r = await client.get(url, follow_redirects=True, timeout=15)
        if r.status_code == 200 and "html" in r.headers.get("content-type", "html"):
            return r.text[:1_500_000]
    except Exception:
        log.info("отметки: не открылось %s", url)
    return None


def _root(url: str) -> str | None:
    p = urlparse(url or "")
    return f"{p.scheme}://{p.netloc}/" if p.scheme in ("http", "https") and p.netloc else None


async def site_handles(client: httpx.AsyncClient, url: str) -> list[str]:
    """Instagram-аккаунты с главной страницы сайта. Кэш на месяц: подвал сайтов меняется редко."""
    root = _root(url)
    if not root:
        return []
    cache = await db.get_setting("ig_sites", {}) or {}
    hit = cache.get(root)
    if hit and time.time() - hit["at"] < SITE_TTL:
        return hit["h"]
    handles = _from_html(await _get(client, root) or "")
    if not handles and url.rstrip("/") != root.rstrip("/"):
        handles = _from_html(await _get(client, url) or "")
    cache[root] = {"h": handles[:5], "at": time.time()}
    if len(cache) > 400:   # старые записи — вон
        cache = dict(sorted(cache.items(), key=lambda kv: kv[1]["at"])[-300:])
    await db.set_setting("ig_sites", cache)
    return handles[:5]


def _names(data: dict) -> dict:
    c = data.get("credits") or {}
    parts = formatter.headline_parts(data)
    def ok(x):
        return str(x).strip() if x and str(x).strip().lower() != "null" else ""
    return {"author": ok(c.get("pr")) or (parts[1] if len(parts) > 1 else ""),
            "author_alt": parts[1] if len(parts) > 1 else "",
            "photographer": ok(c.get("ph")),
            "pr_url": ok(c.get("pr_url")), "ph_url": ok(c.get("ph_url")),
            "work": parts[0] if parts else ""}


def _compact(s: str) -> str:
    return re.sub(r"[^a-zа-яё0-9]", "", (s or "").lower())


def _same(anchor: str, names: list[str]) -> bool:
    """Текст ссылки — это одно из имён? IF_DO = «IF_DO», «Killian O'Sullivan» ≈ «Kilian O'Sullivan»."""
    a = _compact(anchor)
    aw = {w for w in re.findall(r"[a-zа-яё0-9]{4,}", anchor.lower())}
    for n in names:
        c = _compact(n)
        if len(c) >= 3 and len(a) >= 3 and (a == c or (len(c) >= 5 and (c in a or a in c))):
            return True
        if aw & {w for w in re.findall(r"[a-zа-яё0-9]{4,}", n.lower())}:
            return True
    return False


MATCH_SYSTEM = """You help an Instagram editor tag the right accounts on a post about architecture, art or photography.
You get the names from the post credits and a list of Instagram handles that were found on related web pages, each with where it was found.

For each role pick the handle that is the official account of that exact person or studio. Pick ONLY from the list. If no handle clearly belongs to the name, return null. A publication, gallery shop, a different studio or a fan page is not a match.
""" + ("""If a name has no fitting handle in the list, you may use web search (at most 2 searches in total) to find its official Instagram. Use a handle from search only if you saw it in the results as an instagram.com link next to that exact name. Do not search for people who died before 2005, for anonymous or historical works, or for museums' old collection items.
""" if SEARCH else "") + """
Return ONLY JSON: {"author": "handle or null", "photographer": "handle or null", "note": "few words"}"""


async def find(post) -> dict:
    """→ {"author": handle|None, "photographer": handle|None, "via": handle|None, "found": [...]}"""
    data = json.loads(post["data"])
    names = _names(data)
    out = {"author": None, "photographer": None, "via": None, "found": []}
    if not (names["author"] or names["photographer"]) and not post["url"]:
        return out
    cands: list[dict] = []

    def add(h, where):
        h = _clean(h)
        if h and h not in {c["h"] for c in cands} and h != out["via"]:
            cands.append({"h": h, "where": where})

    async with httpx.AsyncClient(headers=HEADERS) as client:
        pub: set[str] = set()
        article = None
        if post["url"]:
            pub_list = await site_handles(client, post["url"])
            pub = set(pub_list)
            out["via"] = pub_list[0] if pub_list else None
            article = await _get(client, post["url"])
        for role, url in (("author", names["pr_url"]), ("photographer", names["ph_url"])):
            if not url:
                continue
            m = IG_RE.search(url)
            if m:
                add(m.group(1), f"credit link for {role}")
            else:
                for h in await site_handles(client, url):
                    add(h, f"website of {role} ({urlparse(url).netloc})")
        if article:
            for h in _from_html(article):
                if h not in pub:
                    add(h, "mentioned in the source article")
            # сайты, на которые статья ссылается по имени автора или фотографа
            soup = BeautifulSoup(article, "lxml")
            pub_host = urlparse(post["url"]).netloc.replace("www.", "")
            want = {"author": [n for n in (names["author"], names["author_alt"]) if n],
                    "photographer": [names["photographer"]] if names["photographer"] else []}
            seen, visited = 0, set()
            for a in soup.find_all("a", href=True):
                href = urljoin(post["url"], a["href"])
                host = urlparse(href).netloc.replace("www.", "")
                if not host or pub_host in host or "instagram.com" in host or host in visited or seen >= 4:
                    continue
                text = a.get_text(" ", strip=True)
                role = next((r for r, n in want.items() if n and text and _same(text, n)), None)
                if role:
                    seen += 1
                    visited.add(host)
                    for h in await site_handles(client, href):
                        add(h, f"website linked as '{a.get_text(' ', strip=True)[:40]}' ({host})")
    out["found"] = cands[:15]
    if not (names["author"] or names["photographer"]):
        return out
    content = (f"Post: {names['work']}\nAuthor / studio / artist: {names['author'] or '—'}"
               + (f" (also written as {names['author_alt']})" if names["author_alt"] and names["author_alt"] != names["author"] else "")
               + f"\nPhotographer: {names['photographer'] or '—'}\n\nFound handles:\n"
               + ("\n".join(f"- @{c['h']} — {c['where']}" for c in cands[:15]) or "(none)"))
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}] if SEARCH else None
    try:
        res = await curator._call(content, system=MATCH_SYSTEM, model=config.TRIAGE_MODEL, max_tokens=600, tools=tools)
    except Exception as exc:
        log.warning("отметки: сопоставление не вышло (%r), без поиска", exc)
        try:
            res = await curator._call(content, system=MATCH_SYSTEM, model=config.TRIAGE_MODEL, max_tokens=400)
        except Exception:
            return out
    if not isinstance(res, dict):
        return out
    found = {c["h"] for c in cands}
    for role in ROLES:
        h = _clean(str(res.get(role) or "").lstrip("@"))
        if h and (h in found or SEARCH):
            out[role] = h
    if (out["author"] and out["author"] == out["photographer"]
            and names["photographer"] and names["photographer"] != names["author"]):
        out["photographer"] = None   # разные люди в кредитах не могут делить один аккаунт
    return out


def photo_tags(tags: dict) -> list[dict]:
    """Метки на первом фото: автор и фотограф, внизу кадра. Издание — только упоминанием в подписи."""
    hs = [tags.get(r) for r in ROLES if tags.get(r)]
    xs = {1: [0.5], 2: [0.3, 0.7]}.get(len(hs), [])
    return [{"username": h, "x": x, "y": 0.9} for h, x in zip(hs, xs)]
