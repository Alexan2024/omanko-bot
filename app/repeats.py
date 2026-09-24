"""Защита от повторов: один и тот же объект снова — не раньше чем через REPEAT_DAYS (по умолчанию год).

Работает без Claude, бесплатно. У каждого поста хранится «отпечаток»:
  • слова заголовка — название, автор, место (кириллица переводится в латиницу, окончания срезаются,
    так что «Капелла Брата Клауса // Петер Цумтор» и «Bruder Klaus // Peter Zumthor» сходятся по автору);
  • отпечатки фото (dHash 64 бита) — одно и то же здание, снятое тем же фотографом, узнаётся,
    даже если пришло из другого издания и под другим заголовком.

Новый материал после оценки сравнивается со всем, что вышло за последний год, с тем, что лежит в запасе,
во входящих и в слотах, и с архивом канала 2025 года (по датам). Совпало — в запас не идёт.
Пост по ссылке и по запросу не отсекается: автор просил сам — на карточке появляется замечание.

Ещё раньше, до оценки, одинаковые заголовки источников отсекаются в pipeline.triage_new — тоже за год."""
import asyncio
import json
import logging
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps, ImageStat

from app import config, db

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS post_prints (
    post_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,       -- json: слова названия
    who TEXT NOT NULL,        -- json: слова автора
    place TEXT NOT NULL DEFAULT '[]',   -- json: слова места
    hashes TEXT NOT NULL,     -- json: отпечатки фото
    updated_at TEXT NOT NULL
);
"""

HASH_DIST = 6          # отпечатки фото ближе этого — один кадр (из 64 бит)
FLAT_STD = 12          # почти однотонные кадры (пустой лист, небо) не сравниваем — слишком похожи все

_TR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
               ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s",
                "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))
STOP = set("""the and with for from into its his her their this that new by in of on at a an to as is are be
house home building project studio design architects architect office works work series photo photos
dom doma zdanie proekt byuro seriya rabota rabot foto dlya pod pri nad ili kak eto
les del des der die das und von den the""".split())


async def init() -> None:
    async with db.connect() as c:
        await c.executescript(SCHEMA)
        await c.commit()


# ======================= слова =======================

def _latin(text: str) -> str:
    s = "".join(_TR.get(ch, ch) for ch in (text or "").lower())
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s if not unicodedata.combining(ch))


SOUND = [("sch", "sh"), ("zh", "j"), ("ph", "f"), ("th", "t"), ("ck", "k"), ("ts", "s"), ("c", "k"), ("q", "k"),
         ("w", "v"), ("x", "ks"), ("z", "s"), ("j", "i")]


def _skeleton(w: str) -> str:
    """Согласный «скелет» слова: Corbusier и Корбюзье, Zumthor и Цумтор, Kyoto и Киото, chapel и капелла дают одно и то же."""
    for a, b in SOUND:
        w = w.replace(a, b)
    w = w[0] + w[1:].replace("h", "") if len(w) > 1 else w    # h почти всегда немая или часть сочетания
    out = w[0]
    for ch in w[1:]:
        if ch not in "aeiouy" and ch != out[-1]:
            out += ch
    return out[:4]


def words(text: str) -> set[str]:
    """Значимые слова: латиницей, без чисел и служебных слов, сведённые к согласному скелету,
    так что разное написание и окончания не мешают."""
    out = set()
    for w in re.findall(r"[a-z]+", _latin(text)):
        if len(w) < 3 or w in STOP:
            continue
        k = _skeleton(w)
        if len(k) >= 2:
            out.add(k)
    return out


def _parts(data: dict) -> tuple[set[str], set[str], set[str]]:
    """→ (слова названия, слова автора, слова места)."""
    parts = [str(p) for p in (data.get("headline_parts") or []) if p and str(p).strip().lower() != "null"]
    if not parts and data.get("headline"):
        parts = [p.strip() for p in str(data["headline"]).split("//")]
    name = words(parts[0]) if parts else set()
    who = words(parts[1]) if len(parts) > 1 else set()
    place = set().union(*(words(p) for p in parts[2:])) if len(parts) > 2 else set()
    return name, who, place


def same_object(a: tuple, b: tuple) -> bool:
    """Два заголовка об одном и том же? Название должно совпасть, а автор — подтвердить или хотя бы не спорить.
    Место подтверждает, только когда автора нет ни у одного (музейные вещи, архивные снимки)."""
    (na, wa, pa), (nb, wb, pb) = a, b
    if not na or not nb:
        return False
    shared = len(na & nb)
    small = min(len(na), len(nb))
    if wa and wb:
        confirm = agree = bool(wa & wb)
    else:
        agree = True
        confirm = not wa and not wb and bool(pa & pb)
    if shared == small and shared >= 2 and agree:
        return True       # название то же (или вложено целиком), автор не спорит
    if na == nb and confirm:
        return True       # короткое название — только вместе с автором (или местом, если авторов нет)
    return shared >= 2 and shared / small >= 0.6 and confirm


# ======================= фото =======================

def _dhash(path: str | Path) -> int | None:
    try:
        with Image.open(path) as im:
            g = ImageOps.exif_transpose(im).convert("L")
            if ImageStat.Stat(g.resize((32, 32))).stddev[0] < FLAT_STD:
                return None
            px = list(g.resize((9, 8), Image.LANCZOS).getdata())
    except Exception:
        return None
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return bits


def hashes(paths: list[str]) -> list[int]:
    return [h for h in (_dhash(p) for p in paths[:10] if Path(p).exists()) if h is not None]


def photos_match(a: list[int], b: list[int]) -> bool:
    """Один и тот же набор снимков: два совпавших кадра, а если у кого-то снимок один — одного хватит."""
    if not a or not b:
        return False
    hits = sum(1 for x in a if any(bin(x ^ y).count("1") <= HASH_DIST for y in b))
    return hits >= min(2, len(a), len(b))


# ======================= отпечатки постов =======================

async def remember(post) -> None:
    """Отпечаток поста — в базу. Фото считаются, пока файлы на диске."""
    data = json.loads(post["data"])
    name, who, place = _parts(data)
    paths = json.loads(post["images"] or "[]")
    hs = await asyncio.to_thread(hashes, paths)
    async with db.connect() as c:
        cur = await c.execute("SELECT hashes FROM post_prints WHERE post_id=?", (post["id"],))
        old = await cur.fetchone()
        if old and not hs:             # файлы уже удалены — старые отпечатки фото не теряем
            hs = json.loads(old["hashes"] or "[]")
        await c.execute(
            "INSERT INTO post_prints(post_id, name, who, place, hashes, updated_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(post_id) DO UPDATE SET name=excluded.name, who=excluded.who, place=excluded.place, "
            "hashes=excluded.hashes, updated_at=excluded.updated_at",
            (post["id"], json.dumps(sorted(name)), json.dumps(sorted(who)), json.dumps(sorted(place)),
             json.dumps(hs), db.now()))
        await c.commit()


async def backfill() -> int:
    """После обновления: отпечатки для постов, у которых их ещё нет (вышедшие за год и все живые)."""
    async with db.connect() as c:
        cur = await c.execute(
            "SELECT p.* FROM posts p LEFT JOIN post_prints f ON f.post_id=p.id WHERE f.post_id IS NULL AND "
            "p.format!='notes' AND (p.status IN ('ready','sent','approved','announced') OR "
            "(p.status='published' AND p.decided_at>=?))", (db.days_ago(config.REPEAT_DAYS),))
        rows = await cur.fetchall()
    for p in rows:
        try:
            await remember(p)
        except Exception:
            log.warning("Отпечаток поста %s не посчитался", p["id"], exc_info=True)
    if rows:
        log.info("Защита от повторов: отпечатки для %d постов", len(rows))
    return len(rows)


def _archive() -> list[dict]:
    """Посты из архива канала (ahmag_posts.json) за последний год: только слова заголовка."""
    try:
        posts = json.loads(config.ARCHIVE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    out, limit = [], datetime.fromisoformat(db.days_ago(config.REPEAT_DAYS)).date()
    for p in posts:
        try:
            day = datetime.strptime(p.get("date", ""), "%d %B %Y").date()
        except ValueError:
            continue
        if day >= limit and p.get("headline"):
            out.append({"headline": p["headline"], "day": day, "parts": _parts({"headline": p["headline"]})})
    return out


async def find(data: dict, images: list[str], exclude_ids: set[int] | None = None) -> str | None:
    """Был ли уже такой объект? → человеческое пояснение («вышел 12.03: …») или None."""
    new = _parts(data)
    hs = await asyncio.to_thread(hashes, images)
    exclude_ids = exclude_ids or set()
    async with db.connect() as c:
        cur = await c.execute(
            "SELECT f.*, p.status, p.decided_at, p.data FROM post_prints f JOIN posts p ON p.id=f.post_id "
            "WHERE p.status IN ('ready','sent','approved','announced') OR "
            "(p.status='published' AND p.decided_at>=?)", (db.days_ago(config.REPEAT_DAYS),))
        rows = await cur.fetchall()
    for r in rows:
        if r["post_id"] in exclude_ids:
            continue
        old = (set(json.loads(r["name"])), set(json.loads(r["who"])), set(json.loads(r["place"] or "[]")))
        by_text = same_object(new, old)
        by_photo = photos_match(hs, json.loads(r["hashes"] or "[]"))
        if by_text or by_photo:
            head = json.loads(r["data"]).get("headline") or "без заголовка"
            when = (f"вышел {r['decided_at'][8:10]}.{r['decided_at'][5:7]}.{r['decided_at'][2:4]}"
                    if r["status"] == "published" else "уже в запасе или очереди")
            how = "по фото" if by_photo and not by_text else "по заголовку" if not by_photo else "по фото и заголовку"
            return f"{when}: {head[:80]} ({how})"
    for a in _archive():
        if same_object(new, a["parts"]):
            return f"был в канале {a['day']:%d.%m.%y}: {a['headline'][:80]} (по заголовку)"
    return None
