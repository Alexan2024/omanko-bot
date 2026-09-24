"""Извлечение текста и фото из статьи, скачивание и фильтр качества."""
import asyncio
import base64
import io
import logging
import re
from pathlib import Path
import urllib.parse as urlparse_mod
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from PIL import Image

from app import config

log = logging.getLogger(__name__)

MAX_RATIO = 2.2          # обычный предел пропорций кадра

SKIP_IMG = re.compile(r"logo|avatar|icon|sprite|banner|(?<![a-z])ads?[_/-]|pixel|gravatar|placeholder|\.svg|\.gif", re.I)


def _best_from_srcset(srcset: str) -> str | None:
    best, best_w = None, 0
    for part in srcset.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        w = 0
        if len(bits) > 1 and bits[1].endswith("w"):
            try:
                w = int(bits[1][:-1])
            except ValueError:
                pass
        if w >= best_w:
            best, best_w = bits[0], w
    return best


def _upgrade(url: str) -> str:
    # ArchDaily отдаёт превью; подменяем на крупный размер
    url = re.sub(r"/(thumb_jpg|small_jpg|medium_jpg|newsletter|slideshow|square)/", "/large_jpg/", url)
    return url.split("?")[0] if "adsttc.com" in url else url


def parse_html(html_text: str, base_url: str) -> dict:
    soup = BeautifulSoup(html_text, "lxml")
    og = soup.find("meta", property="og:title")
    title = og["content"] if og and og.get("content") else (soup.title.string if soup.title else "")

    containers = soup.find_all(["article", "main"]) + [soup.body or soup]
    root = max(containers, key=lambda c: sum(len(p.get_text()) for p in c.find_all("p")))
    for bad in root.select("script, style, nav, footer, aside, form, .related, .comments"):
        bad.decompose()

    paragraphs = [p.get_text(" ", strip=True) for p in root.find_all(["p", "h2", "h3", "li", "figcaption"])]
    text = "\n".join(t for t in paragraphs if len(t) > 30)[:9000]

    urls: list[str] = []
    ogi = soup.find("meta", property="og:image")
    if ogi and ogi.get("content"):
        urls.append(_upgrade(ogi["content"]))
    img_nodes = root.find_all(["img", "source", "a"])
    if len(img_nodes) < 8:  # галерея часто вне основного текста
        for bad in soup.select("nav, footer, header, .related, .related-in-article, [class*=related]"):
            bad.decompose()
        img_nodes += [n for n in soup.find_all(["img", "source", "a"]) if n not in img_nodes]
    for img in img_nodes:
        cand = None
        if img.name == "a":
            href = img.get("href", "")
            if re.search(r"\.(jpe?g|png|webp)(\?|$)", href, re.I):
                cand = href
        else:
            for attr in ("data-srcset", "srcset"):
                if img.get(attr):
                    cand = _best_from_srcset(img[attr])
                    break
            cand = cand or img.get("data-src") or img.get("data-lazy-src") or img.get("src")
        if not cand or cand.startswith("data:") or SKIP_IMG.search(cand):
            continue
        urls.append(_upgrade(urljoin(base_url, cand)))
    return {"title": title or "", "text": text, "image_urls": urls}


def _dedupe(urls: list[str]) -> list[str]:
    seen, uniq = set(), []
    for u in urls:
        key = re.sub(r"[-_]\d{2,4}x\d{2,4}|-scaled|/(large_jpg|newsletter)/", "", u.split("?")[0])
        if key not in seen:
            seen.add(key)
            uniq.append(u)
    return uniq


async def extract_article(client: httpx.AsyncClient, url: str, feed_html: str = "") -> dict:
    """Текст и фото: полный текст из RSS + страница статьи, если она доступна."""
    parts = []
    if feed_html:
        parts.append(parse_html(feed_html, url))
    raw = ""
    try:
        r = await client.get(url, timeout=30, follow_redirects=True)
        r.raise_for_status()
        raw = r.text
        parts.append(parse_html(raw, url))
    except Exception as exc:
        log.info("Страница недоступна (%s), работаем по RSS: %s", exc, url)
    if not parts:
        raise RuntimeError("нет ни RSS-текста, ни страницы")
    text = max((p["text"] for p in parts), key=len)
    title = next((p["title"] for p in reversed(parts) if p["title"]), "")
    urls = _dedupe([u for p in parts for u in p["image_urls"]])
    urls = fix_images(url, urls, raw) or urls   # у некоторых сайтов свои правила, где лежат крупные фото
    return {"title": title, "text": text, "image_urls": urls[:30]}


# ======================= фото со страниц: правила по сайтам =======================

WP_SIZE = re.compile(r"-\d{2,4}x\d{2,4}(?=\.(?:jpe?g|png|webp)$)", re.I)


def _stem(u: str) -> str:
    name = urlparse_mod.urlparse(u).path.rsplit("/", 1)[-1]
    name = WP_SIZE.sub("", name)
    return re.sub(r"(-\d{1,3})?\.(jpe?g|png|webp)$", "", name, flags=re.I).lower()


def fix_images(url: str, urls: list[str], raw_html: str = "") -> list[str]:
    dom = urlparse_mod.urlparse(url).netloc
    if "afasiaarchzine.com" in dom:
        ups = [WP_SIZE.sub("", u.split("?")[0]) for u in urls if "/wp-content/uploads/" in u]
        base = next((_stem(u) for u in ups if "afasia" in _stem(u)), None)
        if base:
            ups = [u for u in ups if _stem(u) == base]
        return list(dict.fromkeys(ups))
    if "publicdomainreview.org" in dom:
        out = [u.split("?")[0] for u in urls if "pdr-assets" in u and "/sources/" not in u]
        return list(dict.fromkeys(out))
    if "inigo.com" in dom:
        found = re.findall(r"https://cdn\.themodernhouse\.com/[^\"'\\\s)]+?_webres\.jpg", raw_html)
        return list(dict.fromkeys(found + [u for u in urls if "themodernhouse" in u]))
    return urls


def _ahash(im: Image.Image) -> int:
    small = im.convert("L").resize((8, 8))
    px = list(small.getdata())
    avg = sum(px) / len(px)
    return sum(1 << i for i, p in enumerate(px) if p > avg)


async def download_images(client: httpx.AsyncClient, urls: list[str], dest: Path,
                          max_ratio: float = MAX_RATIO, min_short: int | None = None) -> list[Path]:
    """Качает, отбрасывает мелкие/дубли/странные пропорции, сохраняет JPEG.
    max_ratio и min_short — для кадров из фильмов мягче: широкий кадр 1280×536 — нормальный кадр."""
    min_short = config.MIN_SHORT_SIDE if min_short is None else min_short
    dest.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(6)

    async def fetch(i: int, u: str):
        async with sem:
            try:
                r = await client.get(u, timeout=40, follow_redirects=True)
                r.raise_for_status()
                im = Image.open(io.BytesIO(r.content))
                im.load()
                return i, im
            except Exception:
                return i, None

    results = sorted(await asyncio.gather(*(fetch(i, u) for i, u in enumerate(urls))))
    saved, hashes = [], []
    for i, im in results:
        if im is None:
            continue
        w, h = im.size
        if max(w, h) < config.MIN_LONG_SIDE or min(w, h) < min_short:
            continue
        if max(w, h) / min(w, h) > max_ratio:
            continue
        hsh = _ahash(im)
        if any(bin(hsh ^ x).count("1") <= 5 for x in hashes):
            continue
        hashes.append(hsh)
        im = im.convert("RGB")
        im.thumbnail((2560, 2560))
        p = dest / f"{len(saved):02d}.jpg"
        im.save(p, "JPEG", quality=90)
        saved.append(p)
        if len(saved) >= 14:  # запас, Claude выберет до 10
            break
    return saved


def thumb_b64(path: Path, size: int = 640) -> str:
    im = Image.open(path)
    im.thumbnail((size, size))
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode()
