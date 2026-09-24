"""Instagram: каждый пост, вышедший в канале, уходит в Instagram на английском.
Путь: публикация в Telegram → английская подпись (Claude Sonnet, один вызов) → фото приводятся к одной
пропорции (Instagram обрезает карусель по первому фото) → бот сам раздаёт их по публичному адресу Railway →
контейнеры Instagram → публикация. Подборки со ссылками на Telegram не уходят.

Переменные Railway: IG_ACCESS_TOKEN, IG_USER_ID и публичный домен (Settings → Networking → Generate Domain).
Токен бот продлевает сам раз в неделю — токен живёт 60 дней, продление сдвигает срок.
На пульте кнопка «📸 Instagram» показывает, работает ли связка; внутри — проверка, история и повтор."""
import asyncio
import hashlib
import html
import json
import logging
import os
import re
import secrets
import shutil
import time
from datetime import datetime
from pathlib import Path

import httpx
from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery
from aiohttp import web
from PIL import Image, ImageOps

from app import brand, cards, config, curator, db, formatter, igfit, igtags, screen, stories

log = logging.getLogger(__name__)
router = Router(name="instagram")
router.callback_query.filter(F.from_user.id == config.ADMIN_ID)
btn = screen.btn

API = "https://graph.instagram.com/v25.0"
ENV_TOKEN = os.getenv("IG_ACCESS_TOKEN", "").strip()
ENV_USER = os.getenv("IG_USER_ID", "").strip()
PORT = int(os.getenv("PORT", "8080"))
PAD = os.getenv("IG_PAD_COLOR", "#FFFFFF")
FOOTER = os.getenv("IG_FOOTER", "").strip()     # строка в конце подписи, например «More on Telegram — link in bio»
PUBLIC_DIR = config.DATA_DIR / "ig_public"
WIDTH = 1080
MAX_ATTEMPTS = 3
CAPTION_MAX = 2200
DAILY_LIMIT = 50

SCHEMA = """
CREATE TABLE IF NOT EXISTS ig_posts (
    post_id INTEGER PRIMARY KEY,
    status TEXT NOT NULL,       -- queued | publishing | done | failed | skipped
    attempts INTEGER NOT NULL DEFAULT 0,
    caption TEXT,               -- английская подпись: переводится один раз, повтор её не переписывает
    media_id TEXT,
    permalink TEXT,
    error TEXT,
    tags TEXT,                  -- json: {"author", "photographer", "via"} — ищутся один раз
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_lock = asyncio.Lock()
_bot: Bot | None = None
_runner: web.AppRunner | None = None


def public_url() -> str | None:
    url = os.getenv("PUBLIC_URL", "").strip() or os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if not url:
        return None
    return (url if url.startswith("http") else f"https://{url}").rstrip("/")


def configured() -> bool:
    return bool(ENV_TOKEN and ENV_USER)


async def enabled() -> bool:
    return bool(await db.get_setting("ig_enabled", True))


async def init() -> None:
    async with db.connect() as c:
        await c.executescript(SCHEMA)
        cur = await c.execute("PRAGMA table_info(ig_posts)")
        if "tags" not in {r["name"] for r in await cur.fetchall()}:
            await c.execute("ALTER TABLE ig_posts ADD COLUMN tags TEXT")
        await c.commit()


async def tags_enabled() -> bool:
    return bool(await db.get_setting("ig_tags", True))


# ======================= раздача фото =======================

async def _serve(request: web.Request) -> web.StreamResponse:
    token, name = request.match_info["token"], request.match_info["name"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", token) or not re.fullmatch(r"\d{2}\.jpg", name):
        raise web.HTTPNotFound()
    path = PUBLIC_DIR / token / name
    if not path.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Content-Type": "image/jpeg"})


async def _ping(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def start_server() -> None:
    """Маленький веб-сервер: Instagram забирает фото по ссылке, загрузить файл напрямую API не даёт."""
    global _runner
    app = web.Application()
    app.router.add_get("/ig/{token}/{name}", _serve)
    app.router.add_get("/ig/ping", _ping)
    app.router.add_get("/", _ping)
    _runner = web.AppRunner(app, access_log=None)
    await _runner.setup()
    await web.TCPSite(_runner, "0.0.0.0", PORT).start()
    log.info("Раздача фото для Instagram: порт %s, адрес %s", PORT, public_url())


def _fit(im: Image.Image, w: int, h: int) -> Image.Image:
    """Фото в кадр w×h и логотип в левый нижний угол снимка. Обычно снимок уже обрезан под пропорцию
    карусели (см. _photos), и остаётся только масштаб. Если пропорция всё же другая — снимок целиком
    с полями, знак тогда встаёт в угол самого снимка, а не полей."""
    im = ImageOps.exif_transpose(im).convert("RGB")
    r, R = im.width / im.height, w / h
    if abs(r - R) / R < 0.03:
        out, box = ImageOps.fit(im, (w, h), Image.LANCZOS), None
    else:
        scale = min(w / im.width, h / im.height)
        small = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS)
        out = Image.new("RGB", (w, h), PAD)
        box = ((w - small.width) // 2, (h - small.height) // 2, small.width, small.height)
        out.paste(small, box[:2])
    if not brand.enabled():
        return out
    try:
        return brand.stamp(out, box)
    except Exception:
        log.warning("Логотип: фото для Instagram ушло без знака", exc_info=True)
        return out


async def _photos(bot: Bot, post) -> tuple[str, list[str]]:
    """Фото поста → JPEG одной пропорции в публичной папке, без полей. → (токен папки, ссылки)
    Пропорция карусели и какие кадры в неё влезают без полей — app/igfit.py."""
    images = json.loads(post["images"] or "[]")
    fids = cards._fids(post)
    token = secrets.token_urlsafe(18)
    folder = PUBLIC_DIR / token
    folder.mkdir(parents=True, exist_ok=True)

    raw: list[Path] = []
    for n, i in enumerate(cards.photo_plan(post)[:10]):
        src = Path(images[i]) if i < len(images) else None
        tmp = folder / f"src{n:02d}"
        if src and src.exists():
            shutil.copyfile(src, tmp)
        elif fids.get(str(i)):
            await bot.download(fids[str(i)], destination=tmp)
        else:
            continue
        raw.append(tmp)

    files, ratios = [], []
    for p in raw:
        try:
            ratios.append(igfit.read_ratio(p))
            files.append(p)
        except Exception:
            log.warning("Instagram: файл %s не открылся, кадр пропущен", p.name, exc_info=True)
            p.unlink(missing_ok=True)
    if not files:
        shutil.rmtree(folder, ignore_errors=True)
        raise RuntimeError("у поста не нашлось фото — ни на диске, ни в Telegram")

    target, keep = igfit.plan(ratios)
    w = WIDTH
    h = max(1, round(w / target))
    exact = w / h                                        # пропорция готового кадра, с учётом округления
    if len(keep) < len(files):
        log.info("Instagram: пропорция %.3f, в карусель идут %d из %d кадров (остальные не влезают без полей)",
                 exact, len(keep), len(files))

    urls, base, out = [], public_url(), 0
    for n, p in enumerate(files):
        if n in keep:
            with Image.open(p) as im:
                # обрезка до точной пропорции кадра: дальше _fit только масштабирует и ставит логотип
                _fit(igfit.crop(im, exact), w, h).save(folder / f"{out:02d}.jpg", "JPEG", quality=92)
            urls.append(f"{base}/ig/{token}/{out:02d}.jpg")
            out += 1
        p.unlink(missing_ok=True)
    return token, urls


# ======================= токен и API =======================

def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


async def token() -> str:
    """Продлённый токен из базы, пока в Railway тот же исходный; новый токен в Railway сбрасывает продлённый."""
    st = await db.get_setting("ig_token") or {}
    if st.get("src") == _h(ENV_TOKEN) and st.get("token"):
        return st["token"]
    return ENV_TOKEN


class IGError(RuntimeError):
    pass


def _err(r: httpx.Response) -> IGError:
    try:
        e = r.json().get("error") or {}
        msg = e.get("error_user_msg") or e.get("message") or r.text
        code = e.get("code")
    except Exception:
        msg, code = r.text, None
    if code == 190:
        msg = "токен недействителен или истёк — сгенерируй новый в Meta и замени IG_ACCESS_TOKEN в Railway"
    elif code == 10 or code == 200:
        msg = f"нет права на публикацию (instagram_business_content_publish): {msg}"
    return IGError(str(msg)[:300])


async def _get(client: httpx.AsyncClient, path: str, **params) -> dict:
    r = await client.get(f"{API}/{path}", params={**params, "access_token": await token()}, timeout=30)
    if r.status_code != 200:
        raise _err(r)
    return r.json()


async def _post(client: httpx.AsyncClient, path: str, **data) -> dict:
    r = await client.post(f"{API}/{path}", data={**data, "access_token": await token()}, timeout=60)
    if r.status_code != 200:
        raise _err(r)
    return r.json()


async def _wait(client: httpx.AsyncClient, cid: str, timeout: int = 180) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = (await _get(client, cid, fields="status_code,status")).get("status_code")
        if st in ("FINISHED", "PUBLISHED"):
            return
        if st in ("ERROR", "EXPIRED"):
            raise IGError(f"Instagram не принял фото ({st}). Чаще всего он не смог скачать его по ссылке — "
                          "проверь публичный домен Railway")
        await asyncio.sleep(3)
    raise IGError("Instagram слишком долго обрабатывает фото — попробую ещё раз позже")


async def refresh_token() -> None:
    """Раз в неделю: продление токена на 60 дней (Instagram разрешает с суток после выдачи)."""
    if not configured():
        return
    st = await db.get_setting("ig_token") or {}
    if st.get("src") == _h(ENV_TOKEN) and time.time() - st.get("at", 0) < 7 * 86400:
        return
    async with httpx.AsyncClient() as client:
        r = await client.get("https://graph.instagram.com/refresh_access_token",
                             params={"grant_type": "ig_refresh_token", "access_token": await token()}, timeout=30)
    if r.status_code != 200:
        log.warning("Токен Instagram не продлился: %s", r.text[:300])
        return
    d = r.json()
    await db.set_setting("ig_token", {"token": d["access_token"], "at": time.time(), "src": _h(ENV_TOKEN),
                                      "expires": time.time() + int(d.get("expires_in") or 0)})
    log.info("Токен Instagram продлён")


# ======================= подпись по-английски =======================

EN_SYSTEM = """You adapt posts from AHMAG, a Russian-language Telegram channel about architecture, art, photography, archives and film, for its Instagram account in English. The author is an architect by training. He writes for people with taste: plainly, specifically, without hype.

Translate the meaning, not word for word. It should read as if the same person wrote it in English. Keep every fact, name, date and number exactly; add nothing. Keep the paragraph breaks.

Style: plain and concrete, conversational, uneven sentence rhythm is fine. Never use: stunning, breathtaking, masterpiece, testament to, nestled, boasts, seamlessly, harmonious, "a dialogue between", "not X but Y" constructions, aphoristic closing lines, exclamation marks, emoji.

Headline parts: names of buildings, works and people keep their original Latin spelling if they already have one; Russian names are transliterated the standard way; descriptive words and places are translated (Токио, Япония → Tokyo, Japan).

Return ONLY JSON: {"headline_parts": ["...", "..."], "text": "..."}"""


async def english_caption(post, tags: dict | None = None) -> str:
    data = json.loads(post["data"])
    fmt = post["format"]
    text_ru = str(data.get("mini_line") or "") if fmt == "mini" else str(data.get("body") or "")
    text_ru = re.sub(r"</?[bi]>", "", text_ru).strip()
    parts = formatter.headline_parts(data)
    en = {"headline_parts": parts, "text": ""}
    if parts or text_ru:
        en = await curator._call(
            f"# Headline parts\n{json.dumps(parts, ensure_ascii=False)}\n\n# Text\n{text_ru or '(none)'}\n\n"
            + ("This is a short post: the text is one simple line describing what is in the photos."
               if fmt == "mini" else "This is a full post: one or more paragraphs."),
            system=EN_SYSTEM, model=config.CLAUDE_MODEL, max_tokens=2500)
    head = " // ".join(str(p).strip() for p in (en.get("headline_parts") or parts) if str(p).strip())
    body = str(en.get("text") or "").strip()
    c = {k: v for k, v in (data.get("credits") or {}).items() if v and str(v).strip().lower() != "null"}
    tags = tags or {}
    handle = {"pr": tags.get("author"), "ph": tags.get("photographer"), "via": tags.get("via")}
    credits = []
    for k in ("pr", "ph", "via"):
        if handle[k]:
            credits.append(f"{k}: @{handle[k]}")      # имя заменяется аккаунтом — так принято в Instagram
        elif c.get(k):
            credits.append(f"{k}: {c[k]}")
    tags = " ".join("#" + t for t in formatter.normalize_tags(data.get("tags", []), fmt))
    tail = [x for x in ("\n".join(credits), FOOTER, tags) if x]
    blocks = [b for b in (head, body) if b]
    caption = "\n\n".join(blocks + tail)
    while len(caption) > CAPTION_MAX and "\n\n" in body:   # длинная заметка — по абзацам с конца
        body = body.rsplit("\n\n", 1)[0]
        caption = "\n\n".join([b for b in (head, body) if b] + tail)
    return caption[:CAPTION_MAX]


# ======================= очередь и публикация =======================

async def _row(pid: int):
    async with db.connect() as c:
        cur = await c.execute("SELECT * FROM ig_posts WHERE post_id=?", (pid,))
        return await cur.fetchone()


async def _set(pid: int, **f) -> None:
    f["updated_at"] = db.now()
    async with db.connect() as c:
        await c.execute(f"UPDATE ig_posts SET {','.join(k + '=?' for k in f)} WHERE post_id=?", (*f.values(), pid))
        await c.commit()


async def enqueue(pid: int) -> None:
    async with db.connect() as c:
        await c.execute("INSERT OR IGNORE INTO ig_posts(post_id, status, created_at, updated_at) VALUES (?,?,?,?)",
                        (pid, "queued", db.now(), db.now()))
        await c.execute("UPDATE ig_posts SET status='queued', attempts=0, error=NULL, updated_at=? "
                        "WHERE post_id=? AND status IN ('failed','skipped')", (db.now(), pid))
        await c.commit()


async def publish(bot: Bot, pid: int) -> None:
    """Один пост в Instagram. Ошибка записывается и приходит уведомлением; повтор — сам или кнопкой."""
    async with _lock:
        row = await _row(pid)
        post = await db.get_post(pid)
        if not row or row["status"] != "queued" or not post:
            return   # уже вышел, уже упал и ждёт повтора, или его взял параллельный вызов
        if (post["source"] or "") == "digest":
            return await _set(pid, status="skipped", error="подборки со ссылками на Telegram в Instagram не идут")
        await _set(pid, status="publishing", attempts=row["attempts"] + 1)
        token_dir = None
        try:
            if not configured():
                raise IGError("не заданы IG_ACCESS_TOKEN и IG_USER_ID в Railway")
            if not public_url():
                raise IGError("у бота нет публичного адреса: Railway → Settings → Networking → Generate Domain")
            if row["tags"] is not None:
                tags = json.loads(row["tags"] or "{}")
            else:
                tags = {}
                if await tags_enabled():
                    try:
                        tags = await igtags.find(post)
                    except Exception:
                        log.warning("Instagram: отметки для %s не нашлись", pid, exc_info=True)
                await _set(pid, tags=json.dumps(tags, ensure_ascii=False))
            caption = row["caption"] or await english_caption(post, tags)
            await _set(pid, caption=caption)
            token_dir, urls = await _photos(bot, post)
            marks = igtags.photo_tags(tags)
            async with httpx.AsyncClient() as client:
                if len(urls) == 1:
                    cid = await _container(client, pid, marks, image_url=urls[0], caption=caption)
                else:
                    kids = []
                    for n, u in enumerate(urls):
                        kids.append(await _container(client, pid, marks if n == 0 else [], image_url=u,
                                                     is_carousel_item="true"))
                    for k in kids:
                        await _wait(client, k)
                    cid = (await _post(client, f"{ENV_USER}/media", media_type="CAROUSEL",
                                       children=",".join(kids), caption=caption))["id"]
                await _wait(client, cid)
                media_id = (await _post(client, f"{ENV_USER}/media_publish", creation_id=cid))["id"]
                try:
                    link = (await _get(client, media_id, fields="permalink")).get("permalink")
                except Exception:
                    link = None
            await _set(pid, status="done", media_id=media_id, permalink=link, error=None)
            await db.set_setting("ig_last_ok", db.now())
            log.info("Instagram: пост %s опубликован %s", pid, link)
            from app import stories
            asyncio.create_task(stories.after_post(bot, pid))    # фон для сторис или сама сторис
        except Exception as exc:
            msg = str(exc) if isinstance(exc, IGError) else curator.explain(exc)
            log.warning("Instagram: пост %s не ушёл: %s", pid, msg)
            await _set(pid, status="failed", error=msg[:300])
            attempts = row["attempts"] + 1
            again = attempts < MAX_ATTEMPTS
            await screen.notify(
                bot, f"⚠️ Instagram: пост не ушёл — {html.escape(msg[:300])}"
                     + ("\nПопробую ещё раз через 20 минут." if again else "\nБольше сам пробовать не буду."),
                [("🔁 Повторить", f"ig:retry:{pid}"), ("📸 Instagram", "ig:go")])
        finally:
            if token_dir:   # Instagram скачивает фото при создании контейнера — после публикации они не нужны
                shutil.rmtree(PUBLIC_DIR / token_dir, ignore_errors=True)


async def _container(client: httpx.AsyncClient, pid: int, marks: list, **data) -> str:
    """Контейнер фото с метками. Не принял метки (аккаунт закрыт, переименован, не найден) — без них."""
    if not marks:
        return (await _post(client, f"{ENV_USER}/media", **data))["id"]
    try:
        return (await _post(client, f"{ENV_USER}/media", user_tags=json.dumps(marks), **data))["id"]
    except IGError as exc:
        first = exc
    cid = (await _post(client, f"{ENV_USER}/media", **data))["id"]   # без меток прошло — значит, дело было в них
    log.warning("Instagram: метки %s не приняты (%s), фото без них", marks, first)
    row = await _row(pid)
    tags = json.loads(row["tags"] or "{}")
    tags["_photo_tags_rejected"] = str(first)[:120]
    await _set(pid, tags=json.dumps(tags, ensure_ascii=False))
    return cid


async def process_pending(bot: Bot) -> None:
    """Каждые 10 минут: очередь и автоповтор неудачных (до трёх попыток, не чаще раза в 20 минут)."""
    if not configured() or not await enabled():
        return
    async with db.connect() as c:
        cur = await c.execute(
            "SELECT post_id FROM ig_posts WHERE status='queued' OR (status='publishing' AND updated_at<?) "
            "OR (status='failed' AND attempts<? AND updated_at<?) ORDER BY post_id",
            (db.hours_ago(0.5), MAX_ATTEMPTS, db.hours_ago(1 / 3)))
        ids = [r["post_id"] for r in await cur.fetchall()]
        for pid in ids:
            await c.execute("UPDATE ig_posts SET status='queued' WHERE post_id=? AND status!='done'", (pid,))
        await c.commit()
    for pid in ids:
        await publish(bot, pid)


def install(bot: Bot) -> None:
    """Вид «📸 Instagram» — в экран. Кнопка на пульте — в screen._home, зеркало публикации — в cards.publish_post."""
    global _bot
    _bot = bot
    screen.VIEWS["ig"] = _v_ig


async def mirror(bot: Bot, pid: int) -> None:
    """После публикации в канале — пост в очередь Instagram (подборки со ссылками на Telegram туда не идут)."""
    if not configured() or not await enabled():
        return
    post = await db.get_post(pid)
    if post and (post["source"] or "") != "digest":
        await enqueue(pid)
        asyncio.create_task(_safe_publish(bot, pid))


async def _safe_publish(bot: Bot, pid: int) -> None:
    try:
        await publish(bot, pid)
    except Exception:
        log.exception("Instagram: публикация %s", pid)


# ======================= проверка =======================

async def check(bot: Bot | None = None) -> dict:
    """Токен, аккаунт, право на публикацию (по лимиту), публичный адрес. Результат — в базу для кнопки на пульте."""
    h = {"at": db.now(), "ok": False, "problems": []}
    if not configured():
        h["problems"].append("не заданы IG_ACCESS_TOKEN и IG_USER_ID в Railway → Variables")
        await db.set_setting("ig_health", h)
        return h
    async with httpx.AsyncClient() as client:
        try:
            me = await _get(client, "me", fields="user_id,username")
            h["username"] = me.get("username")
        except Exception as exc:
            h["problems"].append(f"аккаунт не отвечает: {exc}")
        try:
            if h["problems"]:
                raise IGError("")   # токен уже не прошёл — вторую строку с той же ошибкой не пишем
            lim = await _get(client, f"{ENV_USER}/content_publishing_limit", fields="quota_usage,config")
            d = (lim.get("data") or [{}])[0]
            h["quota"] = d.get("quota_usage")
            h["quota_total"] = (d.get("config") or {}).get("quota_total", DAILY_LIMIT)
        except Exception as exc:
            if str(exc):
                h["problems"].append(f"нет доступа к публикации: {exc}")
        base = public_url()
        if not base:
            h["problems"].append("нет публичного адреса: Railway → Settings → Networking → Generate Domain")
        else:
            try:
                r = await client.get(f"{base}/ig/ping", timeout=15)
                if r.status_code != 200 or r.text.strip() != "ok":
                    raise RuntimeError(f"ответ {r.status_code}")
                h["domain"] = base.split("//", 1)[-1]
            except Exception as exc:
                h["problems"].append(f"адрес {base.split('//', 1)[-1]} не открывается снаружи ({exc}). "
                                     f"В Railway у домена должен быть порт {PORT}")
    h["ok"] = not h["problems"]
    await db.set_setting("ig_health", h)
    return h


async def status_icon() -> str:
    if not configured():
        return "⚙️"
    if not await enabled():
        return "⏸"
    h = await db.get_setting("ig_health") or {}
    if h and not h.get("ok"):
        return "⚠️"
    async with db.connect() as c:
        cur = await c.execute("SELECT status FROM ig_posts ORDER BY updated_at DESC LIMIT 1")
        last = await cur.fetchone()
    if last and last["status"] == "failed":
        return "⚠️"
    return "✅" if h else "…"


async def recent(limit: int = 6) -> list:
    async with db.connect() as c:
        cur = await c.execute("SELECT i.*, p.data FROM ig_posts i LEFT JOIN posts p ON p.id=i.post_id "
                              "ORDER BY i.updated_at DESC LIMIT ?", (limit,))
        return await cur.fetchall()


async def failed_ids() -> list[int]:
    async with db.connect() as c:
        cur = await c.execute("SELECT post_id FROM ig_posts WHERE status='failed' ORDER BY post_id")
        return [r["post_id"] for r in await cur.fetchall()]


async def last_unsent():
    """Последний вышедший в канале пост, которого нет в Instagram."""
    async with db.connect() as c:
        cur = await c.execute(
            "SELECT * FROM posts WHERE status='published' AND COALESCE(source,'')!='digest' "
            "AND id NOT IN (SELECT post_id FROM ig_posts WHERE status IN ('done','publishing','queued')) "
            "ORDER BY decided_at DESC LIMIT 1")
        return await cur.fetchone()


# ======================= экран =======================

STATE = {"done": "✅", "failed": "⚠️", "queued": "⏳", "publishing": "⏳", "skipped": "—"}


async def _v_ig(arg: dict):
    h = await db.get_setting("ig_health") or {}
    on = await enabled()
    icon = await status_icon()
    word = {"✅": "работает", "⚠️": "есть проблема", "⏸": "автопостинг выключен",
            "⚙️": "не настроен", "…": "ещё не проверялся"}.get(icon, "")
    lines = ["<b>📸 Instagram</b>" + (f" · @{html.escape(h['username'])}" if h.get("username") else ""),
             f"Статус: {icon} {word}"]
    if on and configured():
        lines.append("Каждый пост из канала уходит сюда на английском.")
    for p in h.get("problems") or []:
        lines.append(f"• {html.escape(p)}")
    if h.get("quota") is not None:
        lines.append(f"Публикаций за сутки: {h['quota']} из {h.get('quota_total', DAILY_LIMIT)}")
    if h.get("domain"):
        lines.append(f"Фото отдаю через {html.escape(h['domain'])}")
    tk = await db.get_setting("ig_token") or {}
    if tk.get("expires") and tk.get("src") == _h(ENV_TOKEN):
        lines.append(f"Токен действует до {datetime.fromtimestamp(tk['expires']):%d.%m}, продлеваю сам")
    if h.get("at"):
        lines.append(f"<i>Проверено {h['at'][5:16].replace('T', ' ')}</i>")
    if arg.get("note"):
        lines += ["", f"<b>{html.escape(arg['note'])}</b>"]
    sl = await stories.last_line()
    if sl:
        lines.append(html.escape(sl))
    rows_ig = await recent()
    if rows_ig:
        lines += ["", "<b>Последние</b>"]
        for r in rows_ig:
            d = json.loads(r["data"] or "{}")
            head = d.get("headline") or " // ".join(formatter.headline_parts(d)) or f"пост {r['post_id']}"
            head = head if len(head) <= 40 else head[:39] + "…"
            item = f"{STATE.get(r['status'], '')} {html.escape(head)}"
            if r["status"] == "done" and r["permalink"]:
                item = f'{STATE["done"]} <a href="{html.escape(r["permalink"])}">{html.escape(head)}</a>'
            if r["status"] == "failed" and r["error"]:
                item += f" — {html.escape(r['error'][:90])}"
            t = json.loads(r["tags"] or "{}") if r["tags"] else {}
            marks = [f"@{t[k]}" for k in ("author", "photographer", "via") if t.get(k)]
            if marks:
                item += " · " + html.escape(" ".join(marks))
            lines.append(item)
    failed = await failed_ids()
    tags_on = await tags_enabled()
    lines.insert(3 if on and configured() else 2,
                 "Отмечаю бюро, автора, фотографа и издание, если нахожу их аккаунты." if tags_on
                 else "Отметки аккаунтов выключены.")
    rows = [[btn("⏸ Выключить автопостинг" if on else "▶️ Включить автопостинг", "ig:toggle"),
             btn("🔄 Проверить", "ig:check")],
            [btn("🏷 Отметки: вкл" if tags_on else "🏷 Отметки: выкл", "ig:tags"),
             btn(f"📖 Сторис: {stories.MODES[await stories.mode()]} ▸", "ig:stories")]]
    if failed:
        rows.append([btn(f"🔁 Повторить неудачные · {len(failed)}", "ig:retryall")])
    rows.append([btn("📤 Отправить последний пост из канала", "ig:last")])
    rows.append([btn("← Пульт", "h:home")])
    return screen.banner(), "\n".join(lines)[:1020], screen._kb(rows), arg


@router.callback_query(F.data.startswith("ig:"))
async def on_ig(cb: CallbackQuery, bot: Bot):
    await screen.adopt(cb.message)
    p = cb.data.split(":")
    a = p[1]
    if a == "go":
        await cb.answer()
        try:
            await bot.delete_message(config.ADMIN_ID, cb.message.message_id)
        except Exception:
            pass
        return await screen.move_down(bot, "ig")
    if a == "home":
        await cb.answer()
        return await screen.show(bot, "ig")
    if a == "toggle":
        now_on = not await enabled()
        await db.set_setting("ig_enabled", now_on)
        await cb.answer("Автопостинг в Instagram включён" if now_on else "Автопостинг выключен: в Instagram ничего не уходит",
                        show_alert=True)
        return await screen.show(bot, "ig")
    if a == "stories":
        new = stories.NEXT[await stories.mode()]
        await db.set_setting("ig_story_mode", new)
        await cb.answer({"bg": f"Фон для сторис — тебе, к постам с оценкой от {stories.MIN_SCORE}",
                         "auto": "Бот сам публикует сторис к большим постам",
                         "off": "Сторис выключены"}[new], show_alert=True)
        return await screen.show(bot, "ig")
    if a == "tags":
        now_on = not await tags_enabled()
        await db.set_setting("ig_tags", now_on)
        await cb.answer("Отмечаю аккаунты в новых постах" if now_on else "Отметки выключены")
        return await screen.show(bot, "ig")
    if a == "check":
        await cb.answer("Проверяю…")
        await check(bot)
        return await screen.show(bot, "ig")
    if a in ("retry", "retryall"):
        ids = [int(p[2])] if a == "retry" else await failed_ids()
        for pid in ids:
            await enqueue(pid)
        await cb.answer("Отправляю заново")
        if a == "retry" and not getattr(cb.message, "photo", None):
            try:
                await cb.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
        for pid in ids:
            await publish(bot, pid)
        return await screen.show(bot, "ig", note="Готово — смотри статус ниже")
    if a == "last":
        post = await last_unsent()
        if not post:
            return await cb.answer("Все вышедшие посты уже в Instagram", show_alert=True)
        await cb.answer("Перевожу и отправляю — до минуты…")
        await enqueue(post["id"])
        await publish(bot, post["id"])
        return await screen.show(bot, "ig")
    await cb.answer()


def schedule(sched, bot: Bot, guarded) -> None:
    sched.add_job(guarded(bot, "Instagram: очередь", process_pending, bot), "interval", minutes=10,
                  id="ig_queue", max_instances=1)
    sched.add_job(guarded(bot, "Instagram: проверка", check, bot), "interval", hours=1, id="ig_check")
    sched.add_job(guarded(bot, "Instagram: токен", refresh_token), "cron", hour=5, minute=10, id="ig_token")
    sched.add_job(guarded(bot, "Instagram: чистка", cleanup), "cron", hour=4, minute=40, id="ig_cleanup")


async def cleanup() -> None:
    """Папки с фото, которые остались после сбоев, — через сутки."""
    if not PUBLIC_DIR.exists():
        return
    for d in PUBLIC_DIR.iterdir():
        if d.is_dir() and time.time() - d.stat().st_mtime > 86400:
            shutil.rmtree(d, ignore_errors=True)
