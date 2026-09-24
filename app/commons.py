"""Поиск фотографий в Wikimedia Commons — для заметок #ahmagnotes."""
import logging

import httpx

log = logging.getLogger(__name__)
API = "https://commons.wikimedia.org/w/api.php"


async def search(client: httpx.AsyncClient, query: str, limit: int = 8) -> list[str]:
    params = {
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": f"{query} filetype:bitmap", "gsrnamespace": "6", "gsrlimit": str(limit),
        "prop": "imageinfo", "iiprop": "url|size|mime", "iiurlwidth": "2560",
    }
    try:
        r = await client.get(API, params=params, timeout=30)
        r.raise_for_status()
        pages = (r.json().get("query") or {}).get("pages") or {}
    except Exception as exc:
        log.warning("Commons «%s»: %s", query, exc)
        return []
    urls = []
    for p in sorted(pages.values(), key=lambda x: x.get("index", 0)):
        info = (p.get("imageinfo") or [{}])[0]
        if info.get("mime") != "image/jpeg" or (info.get("width") or 0) < 1200:
            continue
        url = info.get("thumburl") or info.get("url")
        if url:
            urls.append(url)
    return urls
