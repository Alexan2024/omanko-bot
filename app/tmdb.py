"""Кадры из фильмов и сериалов — The Movie Database (themoviedb.org).
Ключ — переменная Railway TMDB_API_KEY: подходит и короткий «API Key», и длинный «API Read Access Token».
Без ключа модуль молчит, и кадры для кино берутся со страниц-источников."""
import logging
import os

import httpx

log = logging.getLogger(__name__)

API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/original"
KEY = (os.getenv("TMDB_API_KEY") or os.getenv("TMDB_TOKEN") or "").strip()


def configured() -> bool:
    return bool(KEY)


def _auth() -> tuple[dict, dict]:
    """→ (заголовки, параметры). Длинный токен идёт заголовком, короткий ключ — параметром."""
    if len(KEY) > 40:
        return {"Authorization": f"Bearer {KEY}", "accept": "application/json"}, {}
    return {"accept": "application/json"}, {"api_key": KEY}


async def _get(client: httpx.AsyncClient, path: str, **params) -> dict:
    headers, auth = _auth()
    r = await client.get(API + path, params={**auth, **params}, headers=headers, timeout=20)
    r.raise_for_status()
    return r.json()


def _year(item: dict) -> int | None:
    d = item.get("release_date") or item.get("first_air_date") or ""
    return int(d[:4]) if d[:4].isdigit() else None


async def find(client: httpx.AsyncClient, title: str, year: int | None = None, kind: str = "film") -> dict | None:
    """→ {"id", "type": "movie"|"tv", "title", "year", "url"} или None."""
    if not configured() or not title:
        return None
    order = ["tv", "movie"] if kind == "tv" else ["movie", "tv"]
    for typ in order:
        year_key = "first_air_date_year" if typ == "tv" else "year"
        tries = [{year_key: year}] if year else []
        tries.append({})
        for extra in tries:
            try:
                res = (await _get(client, f"/search/{typ}", query=title, include_adult="false", **extra)).get("results") or []
            except Exception as exc:
                log.warning("TMDB поиск «%s»: %s", title, exc)
                return None
            if not res:
                continue
            if year:   # сначала совпадение по году (±1), потом по популярности
                res.sort(key=lambda x: (abs((_year(x) or 0) - year) > 1, -(x.get("popularity") or 0)))
            best = res[0]
            return {"id": best["id"], "type": typ, "title": best.get("title") or best.get("name") or title,
                    "year": _year(best), "url": f"https://www.themoviedb.org/{typ}/{best['id']}"}
    return None


async def images(client: httpx.AsyncClient, item: dict, limit: int = 14) -> list[str]:
    """Кадры (backdrops) в полном размере: сначала без надписей, потом лучшие по оценке и размеру."""
    if not configured() or not item:
        return []
    try:
        data = await _get(client, f"/{item['type']}/{item['id']}/images", include_image_language="null,en,it,fr,de,es,ja,ru")
    except Exception as exc:
        log.warning("TMDB кадры %s: %s", item, exc)
        return []
    shots = [b for b in data.get("backdrops") or [] if (b.get("width") or 0) >= 1280]
    shots.sort(key=lambda b: (b.get("iso_639_1") is not None, -(b.get("vote_average") or 0), -(b.get("width") or 0)))
    return [IMG + b["file_path"] for b in shots[:limit] if b.get("file_path")]
