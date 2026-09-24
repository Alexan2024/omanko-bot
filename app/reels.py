"""🎬 Рилсы — только для Instagram. Бот собирает видео, автор выкладывает его сам и кладёт музыку.

Два вида:
  подборка — тематическая, отдельная от ленты: Claude придумывает тему («The Art of Melancholy»,
             «Windows at night») и 6–8 работ разных авторов; каждая по несколько секунд с названием и автором.
  детали   — одна картина: общий план, затем камера по очереди подходит к деталям, на каждой — фраза;
             вместе фразы рассказывают историю картины. Факты Claude проверяет веб-поиском.

Картинки — из Wikimedia Commons в высоком разрешении. Какая из найденных — та самая работа, а не деталь,
копия или фото музейной стены, Claude проверяет глазами по превью.

Расписание: REELS_DAYS (пн, ср, пт) в REELS_TIME (18:30). Накануне в REELS_BUILD_TIME (13:00) бот собирает
рилс на завтра и присылает карточку: видео, работы, музыка. ✅ Беру / 🔁 Переделать / ❌ Не надо.
В день выхода в REELS_TIME приходит «Пора выкладывать»: видео файлом без сжатия, подпись одним блоком
(нажать — скопируется), треки. Выложил — «✅ Выложил». Рилс по запросу — экран «🎬 Рилсы» на пульте.

Рилсы в Telegram-канал не идут. Звука в видео нет."""
import asyncio
import base64
import hashlib
import html
import io
import json
import logging
import os
import re
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardMarkup, Message
from PIL import Image, ImageDraw, ImageOps

from app import config, db, reelplan, reelrender, screen, slots, tts, ui
from app.screen import btn

log = logging.getLogger(__name__)
router = Router()
router.message.filter(F.from_user.id == config.ADMIN_ID)
router.callback_query.filter(F.from_user.id == config.ADMIN_ID)


def _hm(raw: str) -> tuple[int, int]:
    h, _, m = raw.strip().partition(":")
    return int(h), int(m or 0)


ENABLED = os.getenv("REELS", "1").strip().lower() not in ("0", "off", "false", "no")
DOW = [d.strip().lower() for d in os.getenv("REELS_DAYS", "mon,wed,fri").split(",") if d.strip()]
TIME = _hm(os.getenv("REELS_TIME", "18:30"))
BUILD_TIME = _hm(os.getenv("REELS_BUILD_TIME", "13:00"))
TOPICS = [t.strip() for t in os.getenv("REEL_TOPICS", "art,architecture,photography,art,architecture,archive").split(",")
          if t.strip()]
MAX_ITEMS = int(os.getenv("REEL_MAX_ITEMS", "8"))
MIN_ITEMS = 5
REMIND_HOURS = 2
DOW_NUM = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
DOW_RU = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
KIND_RU = {"collection": "подборка", "details": "детали картины"}
KIND_ACC = {"collection": "подборку", "details": "детали картины"}
UA = {"User-Agent": f"AHMAG-bot/{config.VERSION} (+https://t.me/ahmag; curation bot)"}
COMMONS = "https://commons.wikimedia.org/w/api.php"
DIR = config.DATA_DIR / "reels"

SCHEMA = """
CREATE TABLE IF NOT EXISTS reels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,         -- collection | details
    day TEXT NOT NULL,          -- YYYY-MM-DD, когда выкладывать
    status TEXT NOT NULL,       -- building | ready | approved | sent | posted | rejected | expired | failed
    title TEXT,
    data TEXT NOT NULL DEFAULT '{}',
    video TEXT,
    card_msg INTEGER,
    pkg_msgs TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

ICON = {"building": "⏳", "ready": "📥", "approved": "🟡", "sent": "📤", "posted": "✅", "rejected": "✕",
        "expired": "⌛️", "failed": "⚠️"}
ACTIVE = ("building", "ready", "approved", "sent")


class ReelError(RuntimeError):
    """Понятная автору причина, почему рилс не собрался."""


class ReelEdit(StatesGroup):
    caption = State()
    theme = State()


# ======================= база =======================

async def init() -> None:
    async with db.connect() as c:
        await c.executescript(SCHEMA)
        await c.commit()
    DIR.mkdir(parents=True, exist_ok=True)


async def get(rid: int):
    async with db.connect() as c:
        return await (await c.execute("SELECT * FROM reels WHERE id=?", (rid,))).fetchone()


async def items(*statuses: str, limit: int = 50) -> list:
    q = "SELECT * FROM reels" + (f" WHERE status IN ({','.join('?' * len(statuses))})" if statuses else "")
    async with db.connect() as c:
        return await (await c.execute(q + " ORDER BY day DESC, id DESC LIMIT ?", (*statuses, limit))).fetchall()


async def _set(rid: int, **f) -> None:
    if "data" in f and not isinstance(f["data"], str):
        f["data"] = json.dumps(f["data"], ensure_ascii=False)
    f["updated_at"] = db.now()
    async with db.connect() as c:
        await c.execute(f"UPDATE reels SET {','.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), rid))
        await c.commit()


async def _add(kind: str, day: str, data: dict) -> int:
    async with db.connect() as c:
        cur = await c.execute("INSERT INTO reels(kind, day, status, data, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                              (kind, day, "building", json.dumps(data, ensure_ascii=False), db.now(), db.now()))
        await c.commit()
        return cur.lastrowid


def _data(r) -> dict:
    return json.loads(r["data"] or "{}")


# ======================= даты =======================

def _now() -> datetime:
    return slots._now()


def _post_dt(day: str) -> datetime:
    d = date.fromisoformat(day)
    return datetime(d.year, d.month, d.day, TIME[0], TIME[1], tzinfo=_now().tzinfo)


def human(day: str) -> str:
    d = date.fromisoformat(day)
    today = _now().date()
    if d == today:
        return "сегодня"
    if d == today + timedelta(days=1):
        return "завтра"
    return f"{DOW_RU[d.weekday()]} {d:%d.%m}"


def is_reel_day(d: date) -> bool:
    return any(DOW_NUM.get(x) == d.weekday() for x in DOW)


async def _taken_days() -> set[str]:
    return {r["day"] for r in await items(*ACTIVE, "posted")}


async def free_day() -> str:
    """Ближайший день без рилса: сегодня, если время ещё не прошло, иначе завтра и дальше."""
    taken = await _taken_days()
    d = _now().date()
    if _now() >= _post_dt(d.isoformat()) - timedelta(minutes=10):
        d += timedelta(days=1)
    while d.isoformat() in taken:
        d += timedelta(days=1)
    return d.isoformat()


# ======================= Wikimedia Commons =======================

def _strip(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html.unescape(s or ""))).strip()


async def commons_candidates(client: httpx.AsyncClient, query: str, n: int = 3) -> list[dict]:
    """Крупные картинки по запросу: [{title, w, h, thumb, artist, license}] — превью 500 px для проверки."""
    params = {"action": "query", "format": "json", "generator": "search", "gsrsearch": f"{query} filetype:bitmap",
              "gsrnamespace": "6", "gsrlimit": "10", "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata",
              "iiurlwidth": "500", "iiextmetadatafilter": "Artist|LicenseShortName"}
    try:
        r = await client.get(COMMONS, params=params, timeout=30)
        r.raise_for_status()
        pages = (r.json().get("query") or {}).get("pages") or {}
    except Exception as exc:
        log.warning("Commons «%s»: %s", query, exc)
        return []
    out = []
    for p in sorted(pages.values(), key=lambda x: x.get("index", 0)):
        i = (p.get("imageinfo") or [{}])[0]
        if i.get("mime") not in ("image/jpeg", "image/png", "image/tiff"):
            continue
        if max(i.get("width") or 0, i.get("height") or 0) < 1500 or not i.get("thumburl"):
            continue
        meta = i.get("extmetadata") or {}
        out.append({"title": p["title"], "w": i["width"], "h": i["height"], "thumb": i["thumburl"],
                    "artist": _strip((meta.get("Artist") or {}).get("value", ""))[:80],
                    "license": _strip((meta.get("LicenseShortName") or {}).get("value", ""))[:40]})
        if len(out) >= n:
            break
    return out


async def commons_download(client: httpx.AsyncClient, title: str, dest: Path) -> Path:
    """Файл Commons в крупном размере (до 3840 px по ширине) → dest (JPEG)."""
    r = await client.get(COMMONS, params={"action": "query", "format": "json", "titles": title, "prop": "imageinfo",
                                          "iiprop": "url|size|mime", "iiurlwidth": "3840"}, timeout=30)
    r.raise_for_status()
    page = next(iter(((r.json().get("query") or {}).get("pages") or {}).values()), {})
    i = (page.get("imageinfo") or [{}])[0]
    url = i.get("url") if i.get("mime") == "image/jpeg" and (i.get("width") or 0) <= 3840 else i.get("thumburl")
    if not url:
        raise ReelError(f"Commons не отдал файл {title}")
    r = await client.get(url, timeout=120)
    r.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)

    def save():
        with Image.open(io.BytesIO(r.content)) as im:
            im = trim_borders(ImageOps.exif_transpose(im).convert("RGB"))
            im.save(dest, "JPEG", quality=94)
    await asyncio.to_thread(save)
    return dest


def trim_borders(im: Image.Image, limit: float = 0.12) -> Image.Image:
    """Срезать ровные поля скана (белые, серые, чёрные — паспарту, фон фотостудии), не больше limit с каждой стороны.
    Поле — полоса, где цвет почти не меняется; живопись так ровно не бывает даже у тёмного фона."""
    import numpy as np
    small = im.copy()
    small.thumbnail((800, 800))
    a = np.asarray(small.convert("L"), dtype=np.float32)
    h, w = a.shape

    def edge(lines, ref):
        n = 0
        for line in lines:
            if line.std() < 4.0 and abs(float(line.mean()) - ref) < 10:
                n += 1
            else:
                break
        return n
    cut = [edge(a, float(a[0].mean())), edge(a[::-1], float(a[-1].mean())),
           edge(a.T, float(a[:, 0].mean())), edge(a.T[::-1], float(a[:, -1].mean()))]
    lim = [int(h * limit), int(h * limit), int(w * limit), int(w * limit)]
    if any(c >= l for c, l in zip(cut, lim)):          # поле шире предела — это часть картины, не трогаем
        cut = [c if c < l else 0 for c, l in zip(cut, lim)]
    top, bottom, left, right = cut
    if not any(cut):
        return im
    k = im.width / w
    pad = 2                                             # на пару пикселей глубже, чтобы не осталась кромка
    box = (int((left + pad) * k) if left else 0, int((top + pad) * k) if top else 0,
           im.width - (int((right + pad) * k) if right else 0), im.height - (int((bottom + pad) * k) if bottom else 0))
    log.info("Рилс: срезаю поля скана %s", box)
    return im.crop(box)


async def _thumbs(client: httpx.AsyncClient, cands: list[dict]) -> list[Image.Image | None]:
    async def one(c):
        try:
            r = await client.get(c["thumb"], timeout=30)
            r.raise_for_status()
            return Image.open(io.BytesIO(r.content)).convert("RGB")
        except Exception:
            return None
    return await asyncio.gather(*(one(c) for c in cands))


def _sheet(thumbs: list[Image.Image | None]) -> Image.Image:
    """Превью кандидатов в ряд, с номерами 1, 2, 3 — чтобы Claude выбрал нужный."""
    h = 320
    tiles = []
    for n, t in enumerate(thumbs, 1):
        if t is None:
            t = Image.new("RGB", (h, h), (60, 60, 60))
        t = t.copy()
        t.thumbnail((h * 2, h))
        tile = Image.new("RGB", (t.width, h + 40), (255, 255, 255))
        tile.paste(t, (0, 40))
        ImageDraw.Draw(tile).text((8, 6), str(n), fill=(0, 0, 0), font=reelrender.font(28))
        tiles.append(tile)
    sheet = Image.new("RGB", (sum(t.width for t in tiles) + 16 * (len(tiles) - 1), h + 40), (255, 255, 255))
    x = 0
    for t in tiles:
        sheet.paste(t, (x, 0))
        x += t.width + 16
    return sheet


def _img_block(im: Image.Image, max_side: int = 1568, quality: int = 85) -> dict:
    im = im.copy()
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.b64encode(buf.getvalue()).decode()}}


PICK_SYSTEM = """You check pictures for an Instagram reel. For each numbered sheet you get the work that should be shown and 1–3 candidate images from Wikimedia Commons, numbered on the sheet.

Pick the candidate that shows exactly this work, whole and clean: for a painting or print — the full picture, straight, without a frame or museum wall around it if another candidate has that, not a detail, sketch, copy or engraving after it (unless the work itself is a print); for a building — a clear, well-composed photo of this building; for a photograph — the photograph itself. Prefer the sharper, better-coloured one. If none fits, pick 0.

Also give the centre of the main subject in the picked image (a face, a figure, the building) as fractions of its width and height: fx, fy from 0 to 1 — the video slowly pushes in toward it.

Return ONLY JSON: {"picks": [{"i": 1, "pick": 2, "fx": 0.3, "fy": 0.5}]}"""


async def verify(client: httpx.AsyncClient, works: list[dict], background: bool, per: int = 3,
                 max_aspect: float | None = None) -> list[dict]:
    """works: [{title, author, year, commons}] → те, для которых нашлась верная картинка, с полем file.
    max_aspect — только картинки не шире этого (ширина / высота): для подборки вертикальных работ."""
    from app import curator
    sem = asyncio.Semaphore(4)

    async def cands(w):
        async with sem:
            c = await commons_candidates(client, w.get("commons") or f"{w.get('title')} {w.get('author')}", per + 2)
            if max_aspect:
                c = [x for x in c if x["w"] / max(1, x["h"]) <= max_aspect]
            return c[:per]
    found = await asyncio.gather(*(cands(w) for w in works))
    pairs = [(w, c) for w, c in zip(works, found) if c]
    if not pairs:
        return []
    thumbs = await asyncio.gather(*(_thumbs(client, c) for _, c in pairs))
    content: list = [{"type": "text", "text": "Sheets follow. Each: the expected work, then the candidates."}]
    for n, ((w, c), t) in enumerate(zip(pairs, thumbs), 1):
        content.append({"type": "text", "text": f"Sheet {n}: {w.get('title')} — {w.get('author')}, {w.get('year')}"})
        content.append(_img_block(_sheet(t), 1400, 80))
    data = await _ask(content, system=PICK_SYSTEM, max_tokens=1500, background=background)
    picks = {}
    for p in data.get("picks") or []:
        try:
            picks[int(p.get("i"))] = (int(p.get("pick") or 0),
                                      [min(1.0, max(0.0, float(p.get("fx", 0.5)))), min(1.0, max(0.0, float(p.get("fy", 0.5))))])
        except (TypeError, ValueError):
            continue
    out = []
    for n, (w, c) in enumerate(pairs, 1):
        k, focus = picks.get(n, (0, [0.5, 0.5]))
        if 1 <= k <= len(c):
            out.append({**w, "file": c[k - 1]["title"], "artist": c[k - 1]["artist"], "license": c[k - 1]["license"],
                        "focus": focus})
    return out


# ======================= тексты =======================

EN_RULES = """Style for every line of text: plain English, concrete, conversational; uneven sentence rhythm is fine. The account is run by an architect for people with taste, so no hype. Never use: stunning, breathtaking, masterpiece, iconic, timeless, captivating, haunting, testament to, nestled, boasts, seamlessly, harmonious, "a dialogue between", "not X but Y" constructions, rhetorical questions, aphoristic closing lines, exclamation marks, emoji."""

TASTE = """AHMAG taste: built architecture (modernism and post-war classics, private houses, sacred buildings, ruins, memorials, brick, concrete, stone, wood, light, landscape); art without kitsch (quiet metaphysics and surrealism, land art, prints, manuscripts, old visual culture, warm humour); documentary, street and archival photography, ethnography; historical series. Never: renders, parametric architecture, commercial towers, developer projects."""

MUSIC = """music — 3 tracks that fit the mood and are likely in Instagram's music library: real, well-known recordings (film scores, classical, ambient, jazz, indie). Artist and track exactly as published; mood — 2–4 words in Russian."""

TOPIC_HINT = {
    "art": "painting, prints, drawings, sculpture",
    "architecture": "buildings: houses, churches, ruins, modernist and older architecture, interiors",
    "photography": "historical and documentary photography (photographers who died before ~1955, archives)",
    "archive": "archive material: old prints, maps, manuscripts, posters, scientific illustration, early photos",
}

COLLECTION_SYSTEM = f"""You make Instagram reels for AHMAG, an account about architecture, art, photography and archives. This reel is a thematic compilation: a title card, then 6–8 works by different authors, each on screen for a few seconds with its title and author. Example of the genre: "The Art of Melancholy" — paintings where sadness is felt rather than shown.

A good theme is specific and has a mood or an idea that ties works from different countries and periods: "Rooms without people", "Windows at night", "Concrete that looks soft", "Painters and their mothers". Not a whole category ("Modern architecture"), not a list of famous things ("Greatest paintings").

{TASTE}

Every work must have a large image on Wikimedia Commons: paintings and prints by artists who died before about 1955, historical photographs, buildings with free-licensed photos. Pick specific works, one per author; mix famous and lesser-known.

format — all works in the reel are shown the same way. "vertical": every work fills the whole tall screen, so EVERY work must be a tall portrait-format image (height at least 1.4 times the width): full-length portraits, standing figures, towers, doorways, narrow streets, vertical photographs. Choose it only when the theme suits tall works. "framed": each work is shown whole on a dark wall, any proportions. When unsure — "framed". Give 10 works in viewing order (some may not be found, the reel uses up to 8); the first one opens the reel under the title, so it should be strong.

{EN_RULES}

{MUSIC}

Return ONLY JSON:
{{"format": "vertical|framed", "title": "on-screen title, up to 28 characters, sentence case; wrap the one key word in *asterisks* — it is set in italics (\"The art of *melancholy*\")", "theme_ru": "тема по-русски, коротко", "works": [{{"title": "title of the work in English or original", "author": "...", "year": "...", "commons": "query for Wikimedia Commons search: title and author, no year"}}], "caption": "1–3 short sentences for the Instagram caption: what ties these works together", "hashtags": ["5–8 lowercase words without #"], "music": [{{"artist": "...", "track": "...", "mood": "..."}}]}}"""

MORE_SYSTEM = f"""You add works to an AHMAG Instagram reel compilation. Same rules: a specific work by an author not yet in the reel, with a large image on Wikimedia Commons (artists who died before about 1955, historical photographs, buildings with free photos), fitting the theme.

{TASTE}

Return ONLY JSON: {{"works": [{{"title": "...", "author": "...", "year": "...", "commons": "query for Wikimedia Commons"}}]}}"""

PAINTING_SYSTEM = f"""You pick a painting for an AHMAG Instagram reel that walks through its details: the camera starts on the whole picture, then moves to 4–6 details one by one, with one short line on each, and the lines tell the painting's story. Example: Matejko's "Stańczyk" — a jester sits alone while the ball goes on next door; the letter on the table; the comet in the window; Poland has lost Smolensk.

Pick a painting (or a fresco, altarpiece, large print) that is in the public domain and has a large image on Wikimedia Commons; has several visible details that carry the story; has a documented story with tension, contrast or a twist that a stranger would care about in three seconds — someone is losing something, hiding something, doesn't know something, or the obvious reading is wrong. The story matters more than fame: a famous painting is fine if its real story is not the one everybody knows. Check the facts with web search. Prefer works that are not the most overexposed. Not from the avoid list.

{TASTE}

{MUSIC}

Return ONLY JSON:
{{"title": "common English title", "author": "...", "year": "...", "museum": "museum, city", "medium": "e.g. Oil on canvas, if known", "size": "e.g. 88 × 120 cm, if known", "commons": "query for Wikimedia Commons search: title and author", "facts": ["6–10 specific verified facts in English, about what is shown, details, context, what happened"], "music": [{{"artist": "...", "track": "...", "mood": "..."}}]}}"""

HOOK_TYPES = {"contradiction": "противоречие", "hidden": "скрытая деталь", "stakes": "ставки",
              "challenge": "вызов", "question": "вопрос"}

STORY_SYSTEM = """You write the narration for an AHMAG Instagram reel about the painting in the image. A narrator reads it aloud, the words appear on screen as they are spoken, and the camera moves between details. Coordinates are fractions of the image width and height from its top-left corner (0 to 1).

THE JOB: the viewer must not scroll away. They decide in the first two seconds, and they stay only while a question is open. So this is a story with tension, told by one person to a friend standing next to them in the museum — never a list of facts.

The engine of retention:
- The hook opens ONE main question. The story answers it only at the climax. Until then every line gives part of the answer and opens a smaller new question.
- Lines are joined by cause and contrast — "but", "so", "which is why", "and that's the problem" — never by "and also", "now look at", "there's more". If a line could be swapped with the next one without breaking anything, the story is a list: rewrite it.
- Order the details by the logic of the story (cause → contrast → consequence), not by where they sit in the picture.
- Stakes in human terms: who loses what, who knows and who doesn't, what someone is hiding or afraid of. No art-history vocabulary, no technique talk unless it IS the story.
- Specific beats general: a name, a number, an object the viewer can see.
- The final line turns the hook around: after it, the first frame reads differently, so the loop back to the start feels natural.

How it sounds:
- Present tense for the scene: "The queen is throwing a ball." Talk to the viewer and steer their eye: "Look at his hands."
- Short sentences, varied rhythm, some fragments. Plain spoken English.
- Let the people in the painting think and feel through what we see: where they look, what they hold, who is missing.

Example of the voice and structure (Matejko's "Stańczyk"; do not reuse its lines):
  hook: "The only man not laughing at this party is the jester."
  context: "Everyone else is at the queen's ball. He's sitting alone, in the dark."
  reveals: "Because of the ^letter on this table. Smolensk has fallen to Moscow. The war is *lost*." / "But next ^door, the court is still dancing. Nobody has read it." / "And in the window, a ^comet. Back then, that meant worse was *coming*."
  climax: "He's the fool. He's the only one who *sees* it."
  final: "Matejko painted this in 1862, when Poland no longer existed. He already knew how it *ended*."

Structure — 25 to 35 seconds, 70–90 words in total:

1. hooks — 4 alternative opening lines, each of a different type:
   contradiction: "The only man not laughing at this party is the jester."
   hidden: "There's a comet in this painting. Almost nobody sees it."
   stakes: "The letter on this table just cost a kingdom a city."
   challenge: "You've seen this jester before. You probably thought he was bored."
   question: "Why is the jester the saddest man at the party?"
   The first sentence is at most 10 words and lands in under three seconds; the whole hook at most 14 words. No names, dates or titles in the hook. It talks about what is on screen in the first frame, so its box is the close-up the reel opens on. True, specific, visual. No generic hooks ("This painting hides a dark secret", "You won't believe what's in this painting").
   Check each hook before you give it: understood in two seconds without sound? about what is on screen? opens a question? concrete? Give only hooks that pass.
   hook_pick — the index of the strongest: the one you would stop scrolling for and the one the story pays off best.
2. context — up to 14 words, shown over the whole painting (the viewer sees the whole scene for the first time). It RAISES the stakes of the hook, it does not explain. Who/when only as a half-clause if needed; never "X painted this in Y" here.
3. reveals — 3 details (4 only if the story truly needs it), each up to 18 words: one step of the story each (see the engine above). label — 1–3 words naming the detail for the on-screen caption ("The letter", "The comet"). Mark with ^ the one word at which the camera should arrive at this detail — the word that names or points at it ("the ^letter", "next ^door"); the ^ is not read aloud.
4. climax — up to 12 words: the answer to the hook's question, what it all means for the person in the painting. Short sentences. Here, and only here, one brief human note. Do not name the emotion with an adjective. box — the detail to hold on (often the face), or null for the whole painting.
5. final — up to 20 words: the last turn — what happened next, or why the painter told this story. It lands with weight and echoes the hook. Not a moral, not a slogan, not "and that's why it's a masterpiece".

Boxes — [x0, y0, x1, y1], tight around the thing itself, not around the area it is in (the letter, not the table). target — 2–6 words naming exactly what is inside the box ("the folded letter on the table"); a second pass uses it to refine the box, so be precise. Only things clearly visible in THIS image.

Every word of emphasis — the one word per sentence the narrator leans on — is wrapped in asterisks: "Nobody has *noticed* he's gone." At most one per sentence; the screen sets it in italics.

Use only the facts given; if the story needs a fact you don't have, change the angle instead of inventing one.
Never: insane, mind-blowing, crazy, iconic, masterpiece, stunning, breathtaking, haunting, heartbreaking, chilling, fascinating, intricate, secret, "dive in", "wait for the end", "follow for more", "let that sink in", "not X but Y" constructions, exclamation marks, emoji, parentheses, abbreviations, lists.

delivery — every hook, reveal, the climax, and the context and final lines (context_delivery, final_delivery) get a short direction for the narrator, in English, 6–15 words: tone, emotion, pace, where to pause. Follow the arc: hook — no warm-up, first word straight away, quiet intrigue, a little quicker; context — lower, the stakes sinking in; reveals — curiosity that builds; climax — slower, softer, heavier, a real pause between sentences; final — calm and weighty. Directions are alive and specific, like notes from a director to an actor ("lean on 'only'", "a wry smile here", "let it hang"), but never theatrical: no shouting, no whispering, no trailer voice.

voice_direction — one or two sentences in English for the narrator about this particular story: its mood, where it turns, what to savour.

caption — the Instagram caption WITHOUT the hook (the hook is put above it automatically): first line "Title (year), Author"; then 2–3 short paragraphs with the story and one or two facts that did not fit the video; last line — museum and city.
hashtags — 5–8 lowercase words without #.

Return ONLY JSON:
{"hooks": [{"type": "contradiction|hidden|stakes|challenge|question", "text": "...", "box": [0.1, 0.2, 0.3, 0.5], "target": "...", "delivery": "..."}], "hook_pick": 0, "context": "...", "context_delivery": "...", "reveals": [{"box": [0.1, 0.2, 0.3, 0.5], "target": "...", "label": "...", "text": "...", "delivery": "..."}], "climax": {"text": "...", "box": [0.1, 0.2, 0.3, 0.5], "target": "...", "delivery": "..."}, "final": "...", "final_delivery": "...", "voice_direction": "...", "caption": "...", "hashtags": ["..."]}"""

REFINE_SYSTEM = """You refine bounding boxes for a video camera. Each image is a crop from a painting with a grid drawn over it: lines every 1/10 of the crop, numbered 0–10 along the top (x) and the left side (y). For each crop you get the thing the camera must frame. Give its tight box in grid units: x0, y0 (top-left) and x1, y1 (bottom-right), decimals allowed, 0 to 10. Tight around the thing itself, not its surroundings. If the thing is not in the crop, or you are not sure which one it is, return null for that crop.

Return ONLY JSON: {"boxes": [{"i": 1, "box": [x0, y0, x1, y1]}, {"i": 2, "box": null}]}"""


def _box(b) -> list | None:
    try:
        b = [float(x) for x in b]
        return b if len(b) == 4 and b[2] > b[0] and b[3] > b[1] else None
    except (TypeError, ValueError):
        return None


def beats(d: dict) -> list[dict]:
    """Сюжет рилса по порядку: [{kind, text, box}], box None — вся картина.
    Рилсы, собранные до v4.7 (intro + frames), превращаются в тот же вид."""
    st = d.get("story")
    if not st:
        out = [{"kind": "context", "text": d.get("intro") or "", "box": None}]
        return out + [{"kind": "reveal", "text": f.get("text") or "", "box": _box(f.get("box"))}
                      for f in d.get("frames") or []]
    hooks = st.get("hooks") or []
    h = hooks[st.get("hook_i", 0) % len(hooks)] if hooks else None
    out = [{"kind": "hook", "text": h["text"], "box": _box(h.get("box")), "how": h.get("delivery")}] if h else []
    out.append({"kind": "context", "text": st.get("context") or "", "box": None, "how": st.get("context_delivery")})
    out += [{"kind": "reveal", "text": r.get("text") or "", "box": _box(r.get("box")), "how": r.get("delivery"),
             "label": (r.get("label") or "").strip()[:28]} for r in st.get("reveals") or []]
    cl = st.get("climax") or {}
    if cl.get("text"):
        out.append({"kind": "climax", "text": cl["text"], "box": _box(cl.get("box")), "how": cl.get("delivery")})
    if st.get("final"):
        out.append({"kind": "final", "text": st["final"], "box": None, "how": st.get("final_delivery")})
    # *слово* — акцент (курсив), ^слово — на нём камера приезжает к детали. Оба знака остаются в "raw" для Remotion,
    # голос, карточка и старый рендер получают чистый текст
    for b in out:
        b["raw"] = b["text"]
        b["text"] = b["text"].replace("*", "").replace("^", "")
    return [b for b in out if b["text"].strip()]


def hook_text(d: dict) -> str:
    b = beats(d)
    return b[0]["text"] if b and b[0]["kind"] == "hook" else ""


async def _ask(content, *, system: str, max_tokens: int, background: bool, tools: list | None = None) -> dict:
    """Вызов Claude для рилса. Пустой ответ (весь запас токенов ушёл на размышления или поиск) — ещё раз
    с запасом втрое больше: так было с «Claude вернул не JSON: ''»."""
    from app import curator
    for n in range(2):
        try:
            return await curator._call(content, system=system, model=config.CLAUDE_MODEL,
                                       max_tokens=max_tokens * (3 if n else 1), tools=tools, background=background)
        except ValueError as exc:
            if n or "не JSON" not in str(exc):
                raise ReelError("Claude ответил не по формату — попробуй ещё раз") from exc
            log.warning("Рилс: пустой или битый ответ Claude, повторяю с большим запасом: %s", exc)
    return {}


def _grid_crop(im: Image.Image, box: list[float]) -> tuple[Image.Image, tuple[float, float, float, float]]:
    """Участок вокруг грубой рамки (с запасом) с сеткой 10×10 и подписями → (картинка, участок в долях)."""
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    # участок примерно квадратный на картине: в 2,2 раза больше рамки, но не меньше 22% короткой стороны
    px = max((x1 - x0) * 2.2 * im.width, (y1 - y0) * 2.2 * im.height, 0.22 * min(im.width, im.height))
    side_w, side_h = min(1.0, px / im.width), min(1.0, px / im.height)
    cx = min(max(cx, side_w / 2), 1 - side_w / 2)
    cy = min(max(cy, side_h / 2), 1 - side_h / 2)
    area = (cx - side_w / 2, cy - side_h / 2, cx + side_w / 2, cy + side_h / 2)
    crop = im.crop((int(area[0] * im.width), int(area[1] * im.height), int(area[2] * im.width), int(area[3] * im.height)))
    crop.thumbnail((900, 900))
    pad = 34
    out = Image.new("RGB", (crop.width + pad + 22, crop.height + pad + 22), (255, 255, 255))
    out.paste(crop, (pad, pad))
    dr = ImageDraw.Draw(out)
    f = reelrender.font(18)
    for k in range(11):
        x = pad + crop.width * k / 10
        y = pad + crop.height * k / 10
        for dx, col in ((1, (0, 0, 0)), (0, (255, 235, 0))):
            dr.line([(x + dx, pad), (x + dx, pad + crop.height)], fill=col, width=1)
            dr.line([(pad, y + dx), (pad + crop.width, y + dx)], fill=col, width=1)
        dr.text((x - (10 if k == 10 else 5), 6), str(k), fill=(0, 0, 0), font=f)
        dr.text((4, y - 10), str(k), fill=(0, 0, 0), font=f)
    return out, area


async def refine_boxes(path: Path, st: dict, background: bool) -> int:
    """Второй проход по рамкам: Claude смотрит на каждую деталь крупно, с сеткой, и уточняет рамку.
    Первый проход (по всей картине в 1568 px) ошибается на 5–10% размера картины — для мелкой детали это мимо.
    Меняет рамки в st на месте. → сколько рамок уточнено."""
    items = [h for h in st.get("hooks") or []] + list(st.get("reveals") or []) + \
        ([st["climax"]] if (st.get("climax") or {}).get("box") else [])
    items = [x for x in items if _box(x.get("box"))]
    if not items:
        return 0
    with Image.open(path) as im:
        im = im.convert("RGB")
        crops = [_grid_crop(im, _box(x["box"])) for x in items]
    content: list = [{"type": "text", "text": f"{len(items)} crops follow."}]
    for n, (x, (img, _)) in enumerate(zip(items, crops), 1):
        what = x.get("target") or x.get("label") or x.get("text") or ""
        content.append({"type": "text", "text": f"Crop {n}: frame {what}"})
        content.append(_img_block(img, 900, 85))
    try:
        data = await _ask(content, system=REFINE_SYSTEM, max_tokens=1500, background=background)
    except ReelError:
        log.warning("Рилс: уточнение рамок не удалось, оставляю первые", exc_info=True)
        return 0
    done = 0
    for r in data.get("boxes") or []:
        try:
            n = int(r.get("i")) - 1
            b = [min(10.0, max(0.0, float(v))) / 10 for v in r["box"]]
        except (TypeError, ValueError, KeyError):
            continue
        if not 0 <= n < len(items) or b[2] - b[0] < 0.02 or b[3] - b[1] < 0.02:
            continue
        ax0, ay0, ax1, ay1 = crops[n][1]
        aw, ah = ax1 - ax0, ay1 - ay0
        items[n]["box"] = [round(ax0 + b[0] * aw, 4), round(ay0 + b[1] * ah, 4),
                           round(ax0 + b[2] * aw, 4), round(ay0 + b[3] * ah, 4)]
        done += 1
    return done


async def sfx_on() -> bool:
    return bool(await db.get_setting("reel_sfx", True))


async def _avoid(kind: str) -> list[str]:
    rows = [r for r in await items(limit=200) if r["kind"] == kind and r["title"]]
    return [r["title"] for r in rows][:60]


async def _next_topic() -> str:
    i = int(await db.get_setting("reel_topic_i", 0) or 0)
    await db.set_setting("reel_topic_i", i + 1)
    return TOPICS[i % len(TOPICS)] if TOPICS else "art"


def _credits(items: list[dict]) -> str:
    """Авторы фото из Commons, если лицензия требует указать автора (не общественное достояние)."""
    need = []
    for it in items:
        lic = (it.get("license") or "").lower()
        if lic and "public domain" not in lic and "pd" not in lic.split() and "cc0" not in lic and it.get("artist"):
            need.append(f"{it['artist']} ({it['license']})")
    base = "Images: Wikimedia Commons"
    return base + (". Photos: " + "; ".join(dict.fromkeys(need)) if need else "")


def _tags(tags: list, extra: str) -> str:
    clean = [re.sub(r"[^a-z0-9_]", "", str(t).lower()) for t in tags or []]
    out = ["ahmag", extra] + [t for t in clean if t and t not in ("ahmag", extra)]
    return " ".join(f"#{t}" for t in list(dict.fromkeys(out))[:10])


def build_caption(r_kind: str, d: dict) -> str:
    if d.get("caption_override"):
        return d["caption_override"]
    if r_kind == "collection":
        lines = [d.get("title", ""), "", d.get("caption", "").strip(), ""]
        for n, it in enumerate(d.get("items") or [], 1):
            lines.append(f"{n}. {it.get('title')} — {it.get('author')}" + (f", {it['year']}" if it.get("year") else ""))
        lines += ["", _credits(d.get("items") or []), "", _tags(d.get("hashtags"), "ahmagreels")]
    else:
        hook = hook_text(d)
        lines = ([hook, ""] if hook else []) + [d.get("caption", "").strip(), "", _credits([d.get("painting") or {}]), "",
                 _tags(d.get("hashtags"), "ahmagreels")]
    return "\n".join(lines).strip()[:2150]


# ======================= сборка =======================

_gen_lock = asyncio.Lock()


async def _collection(rid: int, d: dict, background: bool, client: httpx.AsyncClient) -> dict:
    from app import curator
    folder = DIR / str(rid)
    if not d.get("items"):
        topic = d.get("topic") or await _next_topic()
        req = d.get("request")
        prompt = ((f"Theme requested by the author: {req}\n" if req else
                   f"Area for this reel: {topic} — {TOPIC_HINT.get(topic, topic)}.\n")
                  + "Avoid these earlier reel titles:\n" + ("\n".join(await _avoid("collection")) or "—"))
        plan = await _ask(prompt, system=COLLECTION_SYSTEM, max_tokens=4000, background=background)
        works = [w for w in plan.get("works") or [] if w.get("title")]
        if not works:
            raise ReelError("Claude не предложил работ")
        vertical = str(plan.get("format") or "").lower() == "vertical"
        tall = reelplan.BLEED + 0.03 if vertical else None
        good = await verify(client, works[:12], background, max_aspect=tall)
        if len(good) < MIN_ITEMS:
            more = await _ask(
                f"Theme: {plan.get('title')}\nFormat: {'vertical — only tall portrait-format works' if vertical else 'framed'}"
                + "\nAlready in the reel: " + "; ".join(f"{w['title']} — {w['author']}" for w in good)
                + "\nNot found on Commons: " + "; ".join(w["title"] for w in works
                                                           if w["title"] not in {g["title"] for g in good})
                + f"\nGive {MAX_ITEMS} more works.", system=MORE_SYSTEM, max_tokens=2500, background=background)
            good += await verify(client, (more.get("works") or [])[:MAX_ITEMS], background, max_aspect=tall)
        if vertical and len(good) < MIN_ITEMS:
            # вертикальных не хватило — подборка станет «целиком», добираем любые
            log.info("Рилс: вертикальных работ %s — собираю подборку целиком", len(good))
            have = {g["title"] for g in good}
            good += await verify(client, [w for w in works[:12] if w["title"] not in have], background)
        if len(good) < MIN_ITEMS:
            raise ReelError(f"хороших картинок нашлось только {len(good)} из {MIN_ITEMS} нужных — попробуй другую тему")
        title_em = str(plan.get("title") or "")[:44]
        d.update(title=title_em.replace("*", ""), title_em=title_em, theme_ru=plan.get("theme_ru") or "",
                 caption=plan.get("caption") or "", hashtags=plan.get("hashtags") or [],
                 music=(plan.get("music") or [])[:3], topic=topic, items=good[:MAX_ITEMS], spare=good[MAX_ITEMS:])
    for n, it in enumerate(d["items"]):
        path = folder / f"{n:02d}_{hashlib.md5(it['file'].encode()).hexdigest()[:8]}.jpg"
        if not path.exists():
            await commons_download(client, it["file"], path)
        it["path"] = str(path)
    render_items = [{"path": it["path"], "label": it["title"], "sub": f"By {it['author']}",
                     "focus": it.get("focus") or [0.5, 0.5]} for it in d["items"]]
    video = folder / f"reel_{int(datetime.now().timestamp())}.mp4"
    d.pop("render_note", None)
    if reelplan.available():
        try:
            props = reelplan.collection_props(folder, d, await sfx_on())
            d["duration"] = await asyncio.to_thread(reelplan.render, "Collection", props, folder, video)
            d["video"] = str(video)
            return d
        except Exception as exc:
            log.warning("Рилс: Remotion не собрал подборку, беру запасную вёрстку", exc_info=True)
            d["render_note"] = f"новая вёрстка не собралась ({str(exc)[:80]}) — запасная"
    else:
        d["render_note"] = f"новая вёрстка недоступна: {reelplan.why_not()} — запасная"
    d["duration"] = await asyncio.to_thread(reelrender.collection, d["title"], render_items, video)
    d["video"] = str(video)
    return d


async def _details(rid: int, d: dict, background: bool, client: httpx.AsyncClient) -> dict:
    from app import curator
    folder = DIR / str(rid)
    if not d.get("painting"):
        req = d.get("request")
        prompt = ((f"The author asks for: {req}\n" if req else "")
                  + "Avoid these paintings (already done):\n" + ("\n".join(await _avoid("details")) or "—"))
        tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
        p = await _ask(prompt, system=PAINTING_SYSTEM, max_tokens=4000, tools=tools, background=background)
        if not p.get("title"):
            raise ReelError("Claude не выбрал картину")
        good = await verify(client, [p], background, per=4)
        if not good:
            raise ReelError(f"на Commons не нашлось хорошей картинки «{p['title']}» — попробуй другую картину")
        d.update(painting=good[0], title=f"{p['title']}", music=(p.get("music") or [])[:3],
                 facts=p.get("facts") or [])
        d.pop("frames", None)
        d.pop("story", None)
    pt = d["painting"]
    path = Path(pt.get("path") or folder / "painting.jpg")
    if not path.exists():
        await commons_download(client, pt["file"], path)
    pt["path"] = str(path)
    if not d.get("story") and not d.get("frames"):
        with Image.open(path) as im:
            block = _img_block(im.convert("RGB"))
        avoid = d.get("avoid_frames") or []
        text = (f"Painting: {pt['title']} — {pt['author']}, {pt.get('year')}. {pt.get('museum') or ''}\n\nFacts:\n"
                + "\n".join(f"- {f}" for f in d.get("facts") or [])
                + ("\n\nThe previous version used these details and lines, choose others where possible:\n"
                   + "\n".join(avoid) if avoid else ""))
        st = await _ask([block, {"type": "text", "text": text}], system=STORY_SYSTEM, max_tokens=5000,
                        background=background)
        hooks = [h for h in st.get("hooks") or [] if h.get("text")]
        reveals = [r for r in st.get("reveals") or [] if r.get("text") and _box(r.get("box"))][:4]
        if not hooks or len(reveals) < 2:
            raise ReelError("Claude не собрал сюжет по картине — попробуй «Другая картина»")
        try:
            pick = int(st.get("hook_pick") or 0)
        except (TypeError, ValueError):
            pick = 0
        # выбранный хук — первым, остальные — для кнопки «Другой хук»
        pick = pick if 0 <= pick < len(hooks) else 0
        hooks = [hooks[pick]] + [h for i, h in enumerate(hooks) if i != pick]
        d["story"] = {"hooks": hooks, "hook_i": 0, "context": st.get("context") or "", "reveals": reveals,
                      "climax": st.get("climax") or {}, "final": st.get("final") or "",
                      "context_delivery": st.get("context_delivery") or "", "final_delivery": st.get("final_delivery") or "",
                      "voice_direction": st.get("voice_direction") or ""}
        d.update(caption=st.get("caption") or "", hashtags=st.get("hashtags") or [])
        n = await refine_boxes(path, d["story"], background)
        log.info("Рилс: уточнил рамок %s", n)
    end = [f"{pt['title']}" + (f", {pt['year']}" if pt.get("year") else ""), pt.get("author") or "",
           pt.get("museum") or ""]
    bs = beats(d)
    voice = await _voice(folder, d, bs)
    video = folder / f"reel_{int(datetime.now().timestamp())}.mp4"
    d.pop("render_note", None)
    if reelplan.available():
        try:
            props = await asyncio.to_thread(reelplan.story_props, folder, path, bs, voice, pt, await sfx_on())
            d["duration"] = await asyncio.to_thread(reelplan.render, "Story", props, folder, video)
            d["video"] = str(video)
            return d
        except Exception as exc:
            log.warning("Рилс: Remotion не собрал детали, беру запасную вёрстку", exc_info=True)
            d["render_note"] = f"новая вёрстка не собралась ({str(exc)[:80]}) — запасная"
    else:
        d["render_note"] = f"новая вёрстка недоступна: {reelplan.why_not()} — запасная"
    d["duration"] = await asyncio.to_thread(reelrender.story, path, bs, end, video, voice)
    d["video"] = str(video)
    return d


async def _voice(folder: Path, d: dict, bs: list[dict]) -> list | dict | None:
    """Озвучить вступление и фразы деталей. Уже озвученное (тот же текст, тот же голос) не синтезируется заново.
    Не вышло — рилс собирается без звука, а в d["voice_note"] — почему."""
    d.pop("voice_note", None)
    if await tts.provider() == "off":
        return None
    vkey = await tts.current_key()
    if tts.is_one_take(vkey):
        parts = [(b["kind"], b["text"], b.get("how") or "") for b in bs]
        extra = (d.get("story") or {}).get("voice_direction") or ""
        # «v2» — время слов считается по-новому (5.1): старые дубли с ошибочным временем не берём из кэша
        key = hashlib.md5(f"v2|{vkey}|{json.dumps(parts, ensure_ascii=False)}|{extra}".encode()).hexdigest()[:12]
        hit = (d.get("voice") or {}).get(key)
        if hit and Path(hit["audio"]).exists():
            d["voice_label"] = await tts.label()
            return hit
        try:
            res = await tts.speak_story(parts, folder / "voice" / f"take_{key}", extra)
            if res.get("approx"):
                d["voice_note"] = "распознавание не сошлось с текстом — слова идут по голосу приблизительно"
            d["voice"] = {key: res}
            d["voice_label"] = await tts.label()
            return res
        except Exception as exc:
            log.warning("Рилс: дубль OpenAI не получился, читаю по фразам запасным голосом", exc_info=True)
            d["voice_note"] = f"OpenAI не ответил ({str(exc)[:80]}) — прочитал запасной голос"
            vkey = "kokoro:af_heart"
    cache = d.get("voice") or {}
    texts = [(b["text"], b.get("how") or "") for b in bs]
    out = []
    try:
        for text, how in texts:
            key = hashlib.md5(f"{vkey}|{tts.SPEED}|{text}|{how if vkey.startswith('openai:') else ''}".encode()).hexdigest()[:12]
            hit = cache.get(key)
            if hit and Path(hit["audio"]).exists():
                out.append(hit)
                continue
            res = (await tts.speak(text, folder / "voice" / key, how or None) if not vkey.startswith("kokoro:af_heart")
                   or not d.get("voice_note") else await tts._speak_with(vkey, text, folder / "voice" / key))
            if res and res.get("fallback"):
                d["voice_note"] = res["fallback"]
            if res and not res.get("fallback"):     # запасной голос не запоминаем — в следующий раз попробуем выбранный
                cache[key] = res
            out.append(res)
    except Exception as exc:
        log.warning("Рилс: озвучка не получилась", exc_info=True)
        d["voice_note"] = f"голос не получился ({str(exc)[:80]}) — видео без озвучки"
        return None
    d["voice"] = {k: v for k, v in cache.items() if v in out}
    d["voice_label"] = await tts.label()
    return out


async def generate(bot: Bot, rid: int, background: bool = True) -> None:
    """Собрать (или пересобрать) рилс и прислать карточку. Ошибка — уведомление с кнопкой «ещё раз»."""
    from app import curator
    async with _gen_lock:
        r = await get(rid)
        if not r:
            return
        await _set(rid, status="building", note=None)
        d = _data(r)
        old_video = d.get("video")
        try:
            if not reelrender.ffmpeg_ok():
                raise ReelError("на сервере нет ffmpeg — проверь, что в requirements.txt есть imageio-ffmpeg")
            async with httpx.AsyncClient(headers=UA, follow_redirects=True) as client:
                d = await (_collection if r["kind"] == "collection" else _details)(rid, d, background, client)
        except Exception as exc:
            log.exception("Рилс %s", rid)
            why = str(exc) if isinstance(exc, ReelError) else curator.explain(exc)
            await _set(rid, status="failed", note=why[:300], data=d)
            await screen.notify(bot, f"🎬 Рилс ({KIND_RU[r['kind']]}) не собрался: {why}"[:900],
                                [("🔁 Ещё раз", f"rl:retry:{rid}"), ("🎬 Рилсы", "rl:go")])
            return
        if old_video and old_video != d["video"]:
            Path(old_video).unlink(missing_ok=True)
        d.pop("request_note", None)
        await _set(rid, status="ready", title=d.get("title"), data=d, video=d["video"])
    await send_card(bot, rid)
    screen.refresh_soon(bot)


def start(bot: Bot, rid: int, background: bool = False) -> None:
    asyncio.create_task(generate(bot, rid, background))


async def new(bot: Bot, kind: str, request: str | None = None, day: str | None = None,
              background: bool = False) -> int:
    rid = await _add(kind, day or await free_day(), {"request": request} if request else {})
    start(bot, rid, background)
    return rid


# ======================= карточка =======================

def _music_lines(d: dict) -> list[str]:
    out = []
    for m in d.get("music") or []:
        if m.get("artist") and m.get("track"):
            out.append(f"• {html.escape(m['artist'])} — {html.escape(m['track'])}"
                       + (f" <i>({html.escape(m['mood'])})</i>" if m.get("mood") else ""))
    return out


def _card_text(r, d: dict, note: str | None = None) -> str:
    when = f"{human(r['day'])} в {TIME[0]:02d}:{TIME[1]:02d}"
    st = {"ready": "ждёт решения", "approved": "одобрен", "sent": "ждёт публикации", "posted": "выложен"}.get(
        r["status"], r["status"])
    lines = [f"🎬 <b>Рилс · {KIND_RU[r['kind']]}</b> · {when} · {st}"]
    if note:
        lines.append(f"<b>{html.escape(note)}</b>")
    if r["kind"] == "collection":
        lines.append(f"\n<b>{html.escape(d.get('title') or '')}</b>" + (f" — {html.escape(d['theme_ru'])}"
                                                                        if d.get("theme_ru") else ""))
        for n, it in enumerate(d.get("items") or [], 1):
            lines.append(f"{n}. {html.escape(it['title'])} — {html.escape(it['author'])}"
                         + (f", {html.escape(str(it['year']))}" if it.get("year") else ""))
    else:
        pt = d.get("painting") or {}
        lines.append(f"\n<b>{html.escape(pt.get('title') or '')}</b> — {html.escape(pt.get('author') or '')}"
                     + (f", {html.escape(str(pt['year']))}" if pt.get("year") else ""))
        cut = lambda x: x if len(x) <= 95 else x[:92].rsplit(" ", 1)[0] + "…"
        mark = {"hook": "🪝", "context": "·", "reveal": "·", "climax": "❗️", "final": "↩️"}
        st = d.get("story") or {}
        for b in beats(d):
            if b["kind"] == "hook":
                hooks = st.get("hooks") or []
                h = hooks[st.get("hook_i", 0) % len(hooks)]
                lines.append(f"🪝 <b>{html.escape(b['text'])}</b> <i>({HOOK_TYPES.get(h.get('type'), 'хук')}, "
                             f"{st.get('hook_i', 0) % len(hooks) + 1} из {len(hooks)})</i>")
            else:
                lines.append(f"{mark[b['kind']]} {html.escape(cut(b['text']))}")
    mus = _music_lines(d)
    if mus:
        lines += ["", "🎵 Музыка:"] + mus
    if d.get("render_note"):
        lines.append(f"\n⚠️ {html.escape(d['render_note'])}")
    if r["kind"] == "details":
        lines.append("\n" + (f"⚠️ {html.escape(d['voice_note'])}" if d.get("voice_note") else f"🎙 {html.escape(d.get('voice_label') or '')}"))
    lines.append(f"\n⏱ {d.get('duration', 0):.0f} с · подпись на английском придёт в день выхода")
    text = "\n".join(lines)
    return text if len(text) <= 1024 else text[:1020] + "…"


def _card_kb(r, sub: str | None = None) -> InlineKeyboardMarkup:
    rid, d = r["id"], _data(r)
    if sub == "redo":
        rows = []
        if r["kind"] == "collection":
            rows.append([btn("🔀 Другая тема", f"rl:theme:{rid}"), btn("🔄 Сделать «детали»", f"rl:kind:{rid}")])
            nums = [btn(f"🖼 {n}", f"rl:rep:{rid}:{n}") for n in range(1, len(d.get("items") or []) + 1)]
            if nums:
                rows.append([btn("Заменить работу:", "rl:noop")])
                rows += [nums[i:i + 4] for i in range(0, len(nums), 4)]
        else:
            if len((d.get("story") or {}).get("hooks") or []) > 1:
                rows.append([btn("🪝 Другой хук", f"rl:hook:{rid}")])
            rows.append([btn("🔀 Другая картина", f"rl:theme:{rid}"), btn("🎯 Другой сюжет", f"rl:det:{rid}")])
            rows.append([btn("🔄 Сделать подборку", f"rl:kind:{rid}")])
        rows.append([btn("✏️ Подпись", f"rl:cap:{rid}"), btn("← Назад", f"rl:back:{rid}")])
        return screen._kb(rows)
    if r["status"] == "ready":
        return screen._kb([[btn("✅ Беру", f"rl:ok:{rid}"), btn("🔁 Переделать", f"rl:redo:{rid}"),
                            btn("❌ Не надо", f"rl:no:{rid}")]])
    if r["status"] == "approved":
        return screen._kb([[btn("📤 Выложить сейчас", f"rl:now:{rid}"), btn("↩️ Снять", f"rl:undo:{rid}")],
                           [btn("🔁 Переделать", f"rl:redo:{rid}")]])
    return screen._kb([[btn("🎬 Рилсы", "rl:go")]])


async def send_card(bot: Bot, rid: int, note: str | None = None) -> None:
    r = await get(rid)
    if not r or not r["video"] or not Path(r["video"]).exists():
        return
    d = _data(r)
    await ui.drop(bot, r["card_msg"])
    m = await bot.send_video(config.ADMIN_ID, FSInputFile(r["video"]), caption=_card_text(r, d, note),
                             reply_markup=_card_kb(r), width=reelrender.W, height=reelrender.H,
                             duration=int(d.get("duration") or 0), supports_streaming=True)
    await _set(rid, card_msg=m.message_id)


async def _refresh_card(cb: CallbackQuery, rid: int, note: str | None = None, sub: str | None = None) -> None:
    r = await get(rid)
    if not r:
        return
    try:
        await cb.message.edit_caption(caption=_card_text(r, _data(r), note), reply_markup=_card_kb(r, sub))
    except Exception:
        pass


# ======================= публикация (руками автора) =======================

async def send_package(bot: Bot, rid: int) -> None:
    """«Пора выкладывать»: видео файлом (без сжатия), подпись одним блоком, треки."""
    r = await get(rid)
    if not r or not r["video"] or not Path(r["video"]).exists():
        return
    d = _data(r)
    old = json.loads(r["pkg_msgs"] or "[]")
    await ui.drop(bot, *old)
    doc = await bot.send_document(config.ADMIN_ID, FSInputFile(r["video"], filename=f"ahmag_reel_{rid}.mp4"),
                                  caption=f"🎬 Пора выкладывать: <b>{html.escape(r['title'] or '')}</b>",
                                  disable_content_type_detection=True)
    cap = build_caption(r["kind"], d)
    mus = _music_lines(d)
    text = ("<b>Подпись</b> — нажми на блок, чтобы скопировать:\n"
            f"<pre>{html.escape(cap)}</pre>"
            + ("\n\n🎵 " + "\n".join(mus) if mus else "")
            + ("\n\nВ видео уже есть голос: музыку в Instagram ставь потише, около 20–30%."
               if r["kind"] == "details" and d.get("voice") and not d.get("voice_note") else "")
            + "\n\nInstagram → Reels → это видео → музыка → подпись. Обложку выбери сам.")
    if len(text) > 4000:
        text = text[:3990] + "…</pre>"
    msg = await bot.send_message(config.ADMIN_ID, text, disable_web_page_preview=True,
                                 reply_markup=screen._kb([[btn("✅ Выложил", f"rl:done:{rid}"),
                                                           btn("⏭ Завтра", f"rl:later:{rid}")]]))
    d["sent_at"] = db.now()
    d.pop("reminded", None)
    await _set(rid, status="sent", pkg_msgs=json.dumps([doc.message_id, msg.message_id]), data=d)
    await ui.drop(bot, r["card_msg"])
    screen.refresh_soon(bot)


# ======================= расписание =======================

async def plan_next(bot: Bot) -> None:
    """Накануне дня рилса — собрать его. Если сегодня день рилса, а его нет и время не прошло, — и на сегодня."""
    if not ENABLED:
        return
    taken = await _taken_days()
    today = _now().date()
    targets = [today + timedelta(days=1)]
    if _now() < _post_dt(today.isoformat()) - timedelta(hours=1):
        targets.insert(0, today)
    for d in targets:
        if is_reel_day(d) and d.isoformat() not in taken:
            last = await db.get_setting("reel_last_kind", "details")
            kind = "collection" if last == "details" else "details"
            await db.set_setting("reel_last_kind", kind)
            rid = await _add(kind, d.isoformat(), {})
            await generate(bot, rid, background=True)


async def due(bot: Bot) -> None:
    """Время выкладывать: одобренные на сегодня (и просроченные одобренные) — пакетом. Не решённый — напомнить."""
    today = _now().date().isoformat()
    for r in await items("approved"):
        if r["day"] <= today:
            await send_package(bot, r["id"])
    for r in await items("ready"):
        if r["day"] == today:
            await screen.notify(bot, f"🎬 Рилс на сегодня ещё ждёт решения: {html.escape(r['title'] or '')}",
                                [("👁 Показать", f"rl:card:{r['id']}")])
        elif r["day"] < (_now().date() - timedelta(days=1)).isoformat():
            await _set(r["id"], status="expired")
            await ui.drop(bot, r["card_msg"])
    screen.refresh_soon(bot)


async def remind(bot: Bot) -> None:
    for r in await items("sent"):
        d = _data(r)
        if d.get("reminded") or not d.get("sent_at"):
            continue
        if datetime.fromisoformat(d["sent_at"]) < _now() - timedelta(hours=REMIND_HOURS):
            d["reminded"] = True
            await _set(r["id"], data=d)
            await screen.notify(bot, f"🎬 Рилс «{html.escape(r['title'] or '')}» ещё не отмечен как выложенный. "
                                     "Видео и подпись — выше в чате.", [("🎬 Рилсы", "rl:go")])


async def cleanup() -> None:
    """Файлы рилсов, которые уже выложены или сняты больше недели назад."""
    edge = db.days_ago(7)
    for r in await items("posted", "rejected", "expired", "failed", limit=500):
        if r["updated_at"] < edge:
            shutil.rmtree(DIR / str(r["id"]), ignore_errors=True)


def schedule(sched, bot: Bot, guarded) -> None:
    if not ENABLED:
        return
    sched.add_job(guarded(bot, "рилс: сборка", plan_next, bot), "cron", hour=BUILD_TIME[0], minute=BUILD_TIME[1],
                  id="reels_build", max_instances=1)
    sched.add_job(guarded(bot, "рилс: выкладывать", due, bot), "cron", hour=TIME[0], minute=TIME[1], id="reels_due")
    sched.add_job(guarded(bot, "рилс: напоминание", remind, bot), "interval", minutes=30, id="reels_remind")
    sched.add_job(guarded(bot, "рилс: очистка", cleanup), "cron", hour=4, minute=40, id="reels_cleanup")


def install(bot: Bot) -> None:
    screen.VIEWS["reels"] = _v_reels
    screen.VIEWS["reelvoice"] = _v_voices


async def home_label() -> str:
    n = len(await items("ready"))
    return "🎬 Рилсы" + (f" · {n} ждёт" if n else "")


# ======================= экран =======================

async def _v_reels(arg: dict):
    days = ", ".join(DOW_RU[DOW_NUM[x]] for x in DOW if x in DOW_NUM)
    lines = [f"<b>🎬 Рилсы</b> — только Instagram · {days} в {TIME[0]:02d}:{TIME[1]:02d}",
             f"<i>Накануне в {BUILD_TIME[0]:02d}:{BUILD_TIME[1]:02d} бот собирает рилс и присылает карточку. "
             "Музыку кладёшь ты при публикации.</i>"]
    if not ENABLED:
        lines.append("⚠️ Выключены переменной REELS=0")
    if not reelplan.available():
        lines.append(f"⚠️ Новая вёрстка недоступна ({reelplan.why_not()}) — видео соберётся запасной")
    if not await asyncio.to_thread(reelrender.ffmpeg_ok):
        lines.append("⚠️ Нет ffmpeg — видео не соберётся")
    if arg.get("note"):
        lines.append(f"<b>{html.escape(arg['note'])}</b>")
    rows_r = await items(limit=12)
    lines.append("")
    rows = []
    for r in rows_r[:10]:
        lines.append(f"{ICON.get(r['status'], '•')} {human(r['day'])} · {KIND_RU[r['kind']]} · "
                     f"{html.escape((r['title'] or '…')[:40])}"
                     + (f" — <i>{html.escape((r['note'] or '')[:60])}</i>" if r["status"] == "failed" else ""))
        if r["status"] in ("ready", "approved", "sent") and r["video"]:
            rows.append([btn(f"{ICON[r['status']]} {human(r['day'])} · {(r['title'] or '')[:22]}", f"rl:card:{r['id']}")])
        elif r["status"] == "failed":
            rows.append([btn(f"🔁 Ещё раз · {human(r['day'])}", f"rl:retry:{r['id']}")])
    if not rows_r:
        lines.append("Пока рилсов не было.")
    lines.append("\n<i>📥 ждёт решения · 🟡 одобрен · 📤 ждёт публикации · ✅ выложен · ⏳ собирается</i>")
    rows.append([btn("➕ Подборка", "rl:new:collection"), btn("➕ Детали картины", "rl:new:details")])
    rows.append([btn("✍️ Своя тема или картина", "rl:ask")])
    rows.append([btn(f"🎙 {await tts.label()}", "rl:voices"),
                 btn("🔈 Звуки: вкл" if await sfx_on() else "🔇 Звуки: выкл", "rl:sfx")])
    rows.append([btn("← Пульт", "h:home")])
    return screen.banner(), "\n".join(lines)[:1020], screen._kb(rows), arg


VOICE_KEYS = tts.available()


async def _v_voices(arg: dict):
    """Выбор голоса для «деталей»: нажал — голос выбран, и приходит образец."""
    cur = await tts.voice()
    lines = ["<b>🎙 Голос для «деталей картины»</b>",
             "Нажми на голос — он станет основным, и я пришлю образец послушать."]
    if tts.EL_KEY:
        lines.append("⚠️ Задан ELEVENLABS_API_KEY — пока он есть, звучит ElevenLabs, а не выбор ниже.")
    if not tts.ENABLED:
        lines.append("⚠️ Озвучка выключена переменной REEL_TTS=0.")
    if arg.get("note"):
        lines.append(f"<b>{html.escape(arg['note'])}</b>")
    lines.append("")
    for k in VOICE_KEYS:
        name, desc = tts.VOICES[k]
        lines.append(f"{'●' if k == cur else '○'} <b>{name}</b> — {desc}")
    rows = []
    b = [btn(("● " if k == cur else "") + tts.VOICES[k][0], f"rl:vs:0:{i}") for i, k in enumerate(VOICE_KEYS)]
    rows += [b[i:i + 3] for i in range(0, len(b), 3)]
    rows.append([btn("← Рилсы", "rl:home")])
    return screen.banner(), "\n".join(lines)[:1020], screen._kb(rows), arg


# ======================= кнопки =======================

@router.callback_query(F.data.startswith("rl:"))
async def on_cb(cb: CallbackQuery, bot: Bot, state: FSMContext):
    p = cb.data.split(":")
    a = p[1]
    if a == "noop":
        return await cb.answer()
    if a == "go":
        await cb.answer()
        await ui.drop(bot, cb.message.message_id)
        return await screen.move_down(bot, "reels")
    if a == "home":
        await cb.answer()
        await screen.adopt(cb.message)
        return await screen.show(bot, "reels")
    if a == "new":
        kind = p[2]
        rid = await new(bot, kind)
        r = await get(rid)
        await cb.answer(f"Собираю {KIND_ACC[kind]} на {human(r['day'])} — пара минут")
        await screen.adopt(cb.message)
        return await screen.show(bot, "reels", note=f"⏳ Собираю {KIND_ACC[kind]}, карточка придёт сообщением")
    if a == "sfx":
        on = not await sfx_on()
        await db.set_setting("reel_sfx", on)
        await cb.answer("Звуки в рилсах включены" if on else "Звуки в рилсах выключены")
        await screen.adopt(cb.message)
        return await screen.show(bot, "reels")
    if a == "voices":
        await cb.answer()
        await screen.adopt(cb.message)
        return await screen.show(bot, "reelvoice")
    if a == "vs":
        k = VOICE_KEYS[int(p[3])] if 0 <= int(p[3]) < len(VOICE_KEYS) else None
        if not k:
            return await cb.answer()
        await db.set_setting("reel_voice", k)
        name = tts.VOICES[k][0]
        await cb.answer(f"Голос: {name}. Готовлю образец — до минуты в первый раз")
        await screen.adopt(cb.message)
        await screen.show(bot, "reelvoice", note=f"Выбран {name}")
        try:
            path = await tts.sample(k)
            m = await bot.send_audio(config.ADMIN_ID, FSInputFile(path, filename=f"{name}.mp3"),
                                     title=f"AHMAG · {name}", performer="образец голоса",
                                     caption=f"🎙 {name} — {tts.VOICES[k][1]}")
            await screen.add_temp([m.message_id])
        except Exception as exc:
            log.warning("Образец голоса %s", k, exc_info=True)
            from app import curator
            await screen.notify(bot, f"🎙 Образец {name} не получился: {str(exc)[:200] or curator.explain(exc)}")
        return
    if a == "ask":
        await cb.answer()
        await state.set_state(ReelEdit.theme)
        m = await bot.send_message(config.ADMIN_ID, "Напиши тему подборки («окна ночью», «бетон, похожий на ткань») "
                                                    "или картину («Stańczyk, Матейко»). /cancel — отмена.")
        await state.update_data(prompt=m.message_id)
        return await screen.add_temp([m.message_id])
    if a == "mk":
        data = await state.get_data()
        req = data.get("reel_request")
        await state.clear()
        await ui.drop(bot, cb.message.message_id)
        if not req:
            return await cb.answer("Запрос потерялся — напиши заново", show_alert=True)
        rid = await new(bot, p[2], req)
        r = await get(rid)
        await cb.answer(f"Собираю на {human(r['day'])} — пара минут")
        return await screen.move_down(bot, "reels", note=f"⏳ {KIND_RU[p[2]]}: «{req[:50]}»")

    rid = int(p[2])
    r = await get(rid)
    if not r:
        return await cb.answer("Рилс не найден", show_alert=True)
    if a == "card":
        await cb.answer()
        if cb.message.text:                         # уведомление — оно больше не нужно
            await ui.drop(bot, cb.message.message_id)
        if r["status"] == "sent":
            return await send_package(bot, rid)
        return await send_card(bot, rid)
    if a == "retry":
        await cb.answer("Собираю заново — пара минут")
        if r["status"] != "failed":
            return
        d = _data(r)
        if "хороших картинок" in (r["note"] or "") or "не нашлось" in (r["note"] or ""):
            d = {k: v for k, v in d.items() if k == "request"}     # тема не удалась — берём новую
        await _set(rid, data=d)
        start(bot, rid)
        if cb.message.photo:
            await screen.adopt(cb.message)
            return await screen.show(bot, "reels", note="⏳ Собираю заново")
        return await ui.drop(bot, cb.message.message_id)
    if a == "ok":
        if r["status"] != "ready":
            return await cb.answer("Уже решено")
        now = _now()
        day = r["day"]
        if _post_dt(day) <= now:                    # время уже прошло — выкладывать сейчас
            await _set(rid, status="approved", day=now.date().isoformat())
            await cb.answer("Время уже пришло — присылаю видео и подпись")
            return await send_package(bot, rid)
        await _set(rid, status="approved")
        await cb.answer(f"Беру. Пришлю видео и подпись {human(day)} в {TIME[0]:02d}:{TIME[1]:02d}")
        await _refresh_card(cb, rid)
        return screen.refresh_soon(bot)
    if a == "no":
        await _set(rid, status="rejected")
        await cb.answer("Не надо — убрал")
        await ui.drop(bot, cb.message.message_id)
        return screen.refresh_soon(bot)
    if a == "undo":
        await _set(rid, status="ready")
        await cb.answer("Снял — рилс снова ждёт решения")
        return await _refresh_card(cb, rid)
    if a in ("redo", "back"):
        await cb.answer()
        return await _refresh_card(cb, rid, sub="redo" if a == "redo" else None)
    if a == "now":
        await cb.answer("Присылаю")
        return await send_package(bot, rid)
    if a == "done":
        await _set(rid, status="posted")
        await cb.answer("Отмечено: рилс выложен")
        await ui.drop(bot, *json.loads(r["pkg_msgs"] or "[]"), cb.message.message_id)
        return screen.refresh_soon(bot)
    if a == "later":
        nd = (_now().date() + timedelta(days=1)).isoformat()
        await _set(rid, status="approved", day=nd)
        await cb.answer(f"Пришлю завтра в {TIME[0]:02d}:{TIME[1]:02d}")
        await ui.drop(bot, *json.loads(r["pkg_msgs"] or "[]"), cb.message.message_id)
        return screen.refresh_soon(bot)
    if a == "cap":
        await cb.answer()
        await state.set_state(ReelEdit.caption)
        cap = build_caption(r["kind"], _data(r))
        m = await bot.send_message(config.ADMIN_ID, "Сейчас подпись такая (нажми, чтобы скопировать), пришли новую "
                                                    f"целиком. /cancel — отмена.\n\n<pre>{html.escape(cap)[:3500]}</pre>")
        await state.update_data(prompt=m.message_id, reel=rid)
        return await screen.add_temp([m.message_id])

    # дальше — пересборка
    if r["status"] in ("building",):
        return await cb.answer("Уже собирается")
    d = _data(r)
    if a == "theme":
        d = {}
        note = "Беру другую тему" if r["kind"] == "collection" else "Беру другую картину"
    elif a == "kind":
        kind = "details" if r["kind"] == "collection" else "collection"
        async with db.connect() as c:
            await c.execute("UPDATE reels SET kind=? WHERE id=?", (kind, rid))
            await c.commit()
        d = {}
        note = f"Делаю {KIND_ACC[kind]}"
    elif a == "det":
        d["avoid_frames"] = [f"{b['text']} (box {b['box']})" for b in beats(d)]
        d.pop("frames", None)
        d.pop("story", None)
        note = "Пишу другой сюжет"
    elif a == "hook":
        st = d.get("story") or {}
        if len(st.get("hooks") or []) < 2:
            return await cb.answer("Других хуков нет")
        st["hook_i"] = (st.get("hook_i", 0) + 1) % len(st["hooks"])
        note = f"Беру хук {st['hook_i'] + 1} из {len(st['hooks'])}"
    elif a == "rep":
        n = int(p[3]) - 1
        its = d.get("items") or []
        if not 0 <= n < len(its):
            return await cb.answer("Такой работы нет")
        repl = await _replacement(d, background=False)
        if not repl:
            return await cb.answer("Замену не нашёл — попробуй «Другая тема»", show_alert=True)
        old = its[n]
        if old.get("path"):
            Path(old["path"]).unlink(missing_ok=True)
        its[n] = repl
        d["items"] = its
        note = f"Меняю работу {n + 1}: {repl['title']}"
    else:
        return await cb.answer()
    await _set(rid, data=d)
    await cb.answer(note + " — пара минут")
    await ui.drop(bot, cb.message.message_id)
    start(bot, rid)


async def _replacement(d: dict, background: bool) -> dict | None:
    """Запасная работа из уже проверенных, иначе Claude предложит новые, и одна пройдёт проверку."""
    from app import curator
    spare = d.get("spare") or []
    if spare:
        d["spare"] = spare[1:]
        return spare[0]
    have = "; ".join(f"{w['title']} — {w['author']}" for w in d.get("items") or [])
    more = await _ask(f"Theme: {d.get('title')}\nAlready in the reel: {have}\nGive 4 more works.",
                      system=MORE_SYSTEM, max_tokens=2500, background=background)
    tall = None
    paths = [Path(w["path"]) for w in d.get("items") or [] if w.get("path") and Path(w["path"]).exists()]
    if paths and reelplan.collection_mode([reelplan._size(x) for x in paths]) == "bleed":
        tall = reelplan.BLEED + 0.03              # подборка на весь кадр — замена тоже вертикальная
    async with httpx.AsyncClient(headers=UA, follow_redirects=True) as client:
        good = await verify(client, (more.get("works") or [])[:4], background, max_aspect=tall)
    authors = {w["author"] for w in d.get("items") or []}
    good = [g for g in good if g.get("author") not in authors]
    if not good:
        return None
    d["spare"] = good[1:]
    return good[0]


@router.message(ReelEdit.theme, F.text, ~F.text.startswith("/"))
async def on_theme(msg: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await ui.drop(bot, data.get("prompt"), msg.message_id)
    req = msg.text.strip()[:300]
    await state.set_state(None)                     # данные остаются до выбора вида
    await state.update_data(reel_request=req)
    m = await bot.send_message(config.ADMIN_ID, f"«{html.escape(req)}» — что собрать?",
                               reply_markup=screen._kb([[btn("🖼 Подборку", "rl:mk:collection"),
                                                         btn("🔍 Детали картины", "rl:mk:details")]]))
    await screen.add_temp([m.message_id])


@router.message(ReelEdit.caption, F.text, ~F.text.startswith("/"))
async def on_caption(msg: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    await ui.drop(bot, data.get("prompt"), msg.message_id)
    rid = data.get("reel")
    r = await get(rid) if rid else None
    if not r:
        return
    d = _data(r)
    d["caption_override"] = msg.text.strip()[:2200]
    await _set(rid, data=d)
    await screen.notify(bot, "✏️ Подпись рилса сохранена.", [("👁 Рилс", f"rl:card:{rid}")])
