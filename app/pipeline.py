"""Путь материала: источник → отсев по заголовку и дублям → первичный фильтр (Haiku) →
фото и текст → оценка (Sonnet, пакетом) → пост в запасе. Текст большого поста пишется
только когда пост выбран (Opus). Плюс: пост по ссылке, заметка, выбор следующего поста."""
import json
import logging
import math
import re
import shutil
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from app import commons, config, curator, db, formatter, media, niche, repeats, sources, voice

log = logging.getLogger(__name__)

MUSEUM_NAMES = {"met": "The Metropolitan Museum of Art", "cma": "Cleveland Museum of Art"}
STOPWORDS = set("""the and with for from into its his her their this that new by in of on at a an to as is are be
architects architecture architect studio design designs designed completes creates unveils project projects
photography photographer photographs photos artist artists series how why what who""".split())


def norm_cat(cat: str | None) -> str:
    cat = (cat or "").strip().lower()
    cat = config.CATEGORY_ALIASES.get(cat, cat)
    return cat if cat in config.CATEGORIES else "architecture"


def target_stock() -> int:
    """Сколько готовых постов держать в запасе: слоты на STOCK_DAYS дней плюс немного на замены."""
    return math.ceil(len(config.SLOTS) * config.STOCK_DAYS) + 3


# ---------- дубли по заголовку ----------

def _tokens(title: str) -> set[str]:
    words = re.findall(r"[a-zа-яё0-9]+", (title or "").lower())
    return {w for w in words if len(w) >= 3 and w not in STOPWORDS}


def _is_dup(toks: set[str], seen: list[set[str]]) -> bool:
    if len(toks) < 3:
        return False
    for other in seen:
        shared = len(toks & other)
        if shared >= 3 and shared / min(len(toks), len(other)) >= 0.6:
            return True
    return False


# ---------- первичный фильтр ----------

def _excerpt(cand) -> str:
    payload = json.loads(cand["payload"] or "{}")
    if payload.get("meta"):
        return "; ".join(f"{k}: {v}" for k, v in payload["meta"].items() if v)[:400]
    html_text = payload.get("content_html") or ""
    text = BeautifulSoup(html_text, "lxml").get_text(" ", strip=True) if html_text else ""
    return re.sub(r"\s+", " ", text)[:400]


async def triage_new() -> dict:
    """Новые кандидаты: дубли — сразу мимо, остальное — через Haiku пачками."""
    new = await db.candidates("new", limit=config.TRIAGE_MAX)
    seen = [_tokens(t) for t in await db.recent_titles(config.REPEAT_DAYS)]
    todo, out = [], {"yes": 0, "no": 0, "dups": 0}
    for c in new:
        toks = _tokens(c["title"])
        if _is_dup(toks, seen):
            await db.mark_candidate(c["id"], "skipped", "дубль уже найденного материала")
            out["dups"] += 1
            continue
        seen.append(toks)
        todo.append(c)
    for start in range(0, len(todo), 25):
        chunk = todo[start:start + 25]
        items = [{"i": i, "source": c["source"], "hint": sources.HINTS.get(c["source"], "разное"),
                  "title": (c["title"] or "")[:200], "excerpt": _excerpt(c)} for i, c in enumerate(chunk)]
        try:
            verdicts = await curator.triage(items)
        except (curator.BudgetExceeded, curator.NoCredits, curator.ApiDown):
            raise
        except Exception:
            log.exception("Фильтр не ответил — эти материалы останутся на следующий сбор")
            continue
        for i, c in enumerate(chunk):
            v = verdicts.get(i) or {"v": "maybe", "cat": "", "why": "фильтр промолчал"}
            why = str(v.get("why") or "")[:120]
            if str(v.get("v")).lower() == "no":
                await db.mark_candidate(c["id"], "skipped", f"фильтр: {why}")
                out["no"] += 1
            else:
                await db.update_candidate(c["id"], status="triaged", tcat=norm_cat(v.get("cat")),
                                          tprio=2 if str(v.get("v")).lower() == "yes" else 1, note=why)
                out["yes"] += 1
    return out


async def _select_pool(n: int, target: int) -> list:
    """Из отобранного фильтром — то, чего в запасе не хватает по рубрикам (50% архитектура, 50% остальное)."""
    pool = await db.candidates("triaged", limit=400)
    have: dict[str, int] = {c: 0 for c in config.CATEGORIES}
    for cat, fm in (await db.stock_counts()).items():
        have[norm_cat(cat)] = have.get(norm_cat(cat), 0) + sum(fm.values())
    for cat, k in (await db.batched_categories()).items():
        have[norm_cat(cat)] = have.get(norm_cat(cat), 0) + k
    buckets: dict[str, list] = {}
    for c in sorted(pool, key=lambda c: (-(c["tprio"] or 0), -c["id"])):
        buckets.setdefault(norm_cat(c["tcat"]), []).append(c)
    chosen, per_source = [], {}
    while len(chosen) < n and any(buckets.values()):
        cats = [c for c, b in buckets.items() if b]
        cat = max(cats, key=lambda c: config.TARGET_MIX.get(c, 0.05) * target - have.get(c, 0))
        cand = buckets[cat].pop(0)
        if per_source.get(cand["source"], 0) >= 4:
            continue
        per_source[cand["source"]] = per_source.get(cand["source"], 0) + 1
        have[cat] = have.get(cat, 0) + 1
        chosen.append(cand)
    return await _top_up_finds(chosen, n)


async def ready_finds() -> list:
    """Находки в запасе — посты из нишевых источников."""
    return [p for p in await db.ready_posts() if p["source"] in niche.FINDS and p["format"] != "notes"]


async def _top_up_finds(chosen: list, n: int) -> list:
    """Находок в запасе мало — добираем в оценку пару материалов из нишевых источников."""
    have = len(await ready_finds()) + sum(1 for c in chosen if c["source"] in niche.FINDS)
    need = min(2, niche.FIND_STOCK - have)
    if need <= 0 or n <= 0:
        return chosen
    ids = {c["id"] for c in chosen}
    pool = [c for c in await db.candidates("triaged", limit=400) if c["source"] in niche.FINDS and c["id"] not in ids]
    pool.sort(key=lambda c: (-(c["tprio"] or 0), -c["id"]))
    extra = pool[:need]
    return chosen[:max(0, n - len(extra))] + extra if extra else chosen


# ---------- подготовка: текст и фото ----------

def _museum_text(source: str, meta: dict) -> str:
    intro = niche.META_INTRO.get(source) or f"Объект из открытой коллекции {MUSEUM_NAMES.get(source, source)}."
    return intro + "\n" + "\n".join(f"{k}: {v}" for k, v in meta.items() if v)


async def _prepare(client: httpx.AsyncClient, cand, force: bool = False) -> dict | str:
    """→ подготовленный материал или причина отказа."""
    cid, source = cand["id"], cand["source"]
    payload = json.loads(cand["payload"] or "{}")
    folder = config.IMG_DIR / f"c{cid}"
    museum = bool(payload.get("meta")) and bool(payload.get("images"))
    try:
        if museum:
            meta = payload["meta"]
            title, text, urls, min_photos = meta.get("title") or cand["title"], _museum_text(source, meta), payload["images"], 1
        else:
            art = await media.extract_article(client, cand["url"], payload.get("content_html", ""))
            title, text, urls = art["title"] or cand["title"], art["text"], art["image_urls"]
            min_photos = 1 if force else config.MIN_PHOTOS_MINI
            if len(text) < (80 if force else 300):
                return "мало текста"
        if source in niche.CINEMA_SOURCES:     # кадры из фильмов: широкие и невысокие — это нормально
            images = await media.download_images(client, urls, folder, max_ratio=niche.CINEMA_MAX_RATIO,
                                                 min_short=niche.CINEMA_MIN_SHORT)
        else:
            images = await media.download_images(client, urls, folder)
    except Exception as exc:
        shutil.rmtree(folder, ignore_errors=True)
        return f"не открылся: {exc!r}"[:200]
    for extra in images[config.EVAL_PHOTOS:]:
        Path(extra).unlink(missing_ok=True)
    images = images[:config.EVAL_PHOTOS]
    if len(images) < min_photos:
        shutil.rmtree(folder, ignore_errors=True)
        return f"мало качественных фото: {len(images)}"
    allow_std = museum or force or len(images) >= config.MIN_PHOTOS_ARTICLE
    if not force and (source in niche.MINI_ONLY or (source == "arena" and "meta" in payload)):
        allow_std = False       # кадры из фильма или блок Are.na без источника: фактов мало — только мини
    return {"title": title or "", "text": (text or "")[:6000], "images": [str(p) for p in images],
            "allow_std": allow_std}


def _params(context: str, cand, prep: dict, forced: bool = False) -> dict:
    return curator.eval_params(context, cand["source"], cand["url"], prep["title"], prep["text"],
                               prep["images"], allow_std=prep["allow_std"], forced=forced)


# ---------- результат оценки → пост в запасе ----------

def _order(data: dict, n: int) -> list[int]:
    order = [i for i in (data.get("photo_order") or []) if isinstance(i, int) and 0 <= i < n]
    return list(dict.fromkeys(order)) or list(range(n))


async def finish(cid: int, data: dict, forced: bool = False) -> int | None:
    """Ответ оценки → пост в запасе (status ready). None — не прошёл."""
    cand = await db.get_candidate(cid)
    prep = json.loads(cand["prep"] or "{}")
    images = [p for p in prep.get("images", []) if Path(p).exists()]
    folder = config.IMG_DIR / f"c{cid}"
    data["flags"] = [str(f) for f in (data.get("flags") or [])]
    score = int(data.get("score") or 0)
    passed = score >= config.SCORE_THRESHOLD and not data.get("stoplist") and not data.get("already_posted")
    if not passed and not forced:
        await db.mark_candidate(cid, "processed", f"авто-отказ {score}: {data.get('score_reason', '')}")
        shutil.rmtree(folder, ignore_errors=True)
        return None
    if not images:
        await db.mark_candidate(cid, "error", "фото пропали с диска до оценки")
        return None
    if forced:
        if data.get("stoplist"):
            data["flags"].append("совпадает со стоп-листом")
        if data.get("already_posted"):
            data["flags"].append("похоже, уже было в канале")

    dup = await repeats.find(data, images)
    if dup and not forced:
        await db.mark_candidate(cid, "processed", f"повтор — {dup}"[:300])
        shutil.rmtree(folder, ignore_errors=True)
        return None
    if dup:
        data["flags"].append(f"похоже, уже было — {dup}")

    fmt = "mini" if (data.get("format") == "mini" or not prep.get("allow_std")) else "std"
    data.pop("body", None)  # основной текст пишется позже, когда пост выберут
    line = str(data.get("mini_line") or "").strip()
    if line and line.lower() != "null":
        hits = voice.check(line, await voice.banned())
        if hits:
            try:
                fixed = await curator.fix_line(line, hits, " // ".join(formatter.headline_parts(data)))
                if fixed and not voice.check(fixed, await voice.banned()):
                    data["mini_line"] = fixed
                else:
                    data["flags"].append("штамп во фразе: " + ", ".join(hits))
            except Exception:
                data["flags"].append("штамп во фразе: " + ", ".join(hits))

    order = _order(data, len(images))
    rest = [i for i in range(len(images)) if i not in order]
    ordered = [images[i] for i in order + rest]
    data["_excluded"] = list(range(len(order), len(ordered)))  # не выбранные Claude — выключены, но их можно вернуть
    data["_source_text"] = prep.get("text", "")[:6000]
    data["_title"] = prep.get("title") or cand["title"] or ""
    category = norm_cat(data.get("category"))
    data["category"] = category
    caption = formatter.build_caption(data, fmt)

    pid = await db.add_post(
        candidate_id=cid, source=cand["source"], url=cand["url"], category=category,
        data=data, caption=caption, score=score, format=fmt,
        reason=data.get("score_reason", ""), images=ordered, status="ready",
    )
    await db.update_candidate(cid, status="processed", note=f"в запасе {score} ({fmt})", prep=None)
    try:
        await repeats.remember(await db.get_post(pid))
    except Exception:
        log.warning("Отпечаток поста %s не записался", pid, exc_info=True)
    return pid


# ---------- сбор целиком ----------

async def _set_error(exc: Exception | None) -> None:
    await db.set_setting("api_error", {"at": db.now(), "text": curator.explain(exc)} if exc else None)


async def run_collection(manual: bool = False) -> dict:
    """Сбор → фильтр → оценка того, чего не хватает в запасе. Возвращает сводку для чата."""
    s = {"added": 0, "dropped": 0, "yes": 0, "no": 0, "dups": 0, "queued": 0, "made": 0, "note": ""}
    s["added"], s["dropped"] = await sources.collect_all()
    await db.expire_candidates(config.CANDIDATE_TTL_DAYS)
    await db.set_setting("last_collect", db.now())
    try:
        s.update(await triage_new())
    except (curator.BudgetExceeded, curator.NoCredits, curator.ApiDown) as exc:
        await _set_error(exc)
        s["note"] = curator.explain(exc)
        return s

    ready, inflight, target = await db.count_ready(), await db.batched_count(), target_stock()
    need = target - ready - inflight
    if need <= 0:
        s["note"] = f"запас полный ({ready} готово, {inflight} на оценке) — оценку пропустил"
        await _set_error(None)
        return s
    chosen = await _select_pool(min(config.MAX_PER_RUN, math.ceil(need * 1.5)), target)
    if not chosen:
        s["note"] = "подходящих новых материалов нет"
        return s

    prepared = []
    async with httpx.AsyncClient(headers={"User-Agent": config.USER_AGENT}) as client:
        for c in chosen:
            res = await _prepare(client, c)
            if isinstance(res, str):
                await db.mark_candidate(c["id"], "skipped", res)
                continue
            await db.update_candidate(c["id"], prep=res)
            prepared.append(await db.get_candidate(c["id"]))
    if not prepared:
        s["note"] = "у отобранных материалов не нашлось хороших фото"
        return s

    context = await curator.eval_context()
    quick = not config.USE_BATCH or (manual and ready < 4)
    try:
        if quick:  # запас пуст и автор ждёт — оцениваем сразу, по полной цене
            for c in prepared:
                try:
                    data = await curator.evaluate(_params(context, c, json.loads(c["prep"])))
                except (curator.BudgetExceeded, curator.NoCredits, curator.ApiDown):
                    raise
                except Exception as exc:
                    log.exception("Оценка %s", c["id"])
                    await db.mark_candidate(c["id"], "error", repr(exc))
                    continue
                if await finish(c["id"], data):
                    s["made"] += 1
        else:
            requests = [{"custom_id": f"c{c['id']}", "params": _params(context, c, json.loads(c["prep"]))}
                        for c in prepared]
            bid = await curator.batch_create(requests)
            await db.add_batch(bid, len(requests), manual)
            for c in prepared:
                await db.update_candidate(c["id"], status="batched", batch_id=bid)
            s["queued"] = len(requests)
    except (curator.BudgetExceeded, curator.NoCredits, curator.ApiDown) as exc:
        await _set_error(exc)
        s["note"] = curator.explain(exc)
        return s
    await _set_error(None)
    return s


async def poll_batches() -> list[dict]:
    """Забирает готовые пакеты. → [{manual, made, failed}] по каждому закрытому пакету."""
    closed = []
    for b in await db.open_batches():
        try:
            ended, _ = await curator.batch_status(b["id"])
        except (curator.NoCredits, curator.ApiDown) as exc:
            await _set_error(exc)
            break
        except Exception:
            log.exception("Пакет %s", b["id"])
            continue
        if not ended:
            continue
        made = failed = 0
        seen = set()
        async for custom_id, data, err in curator.batch_results(b["id"]):
            try:
                cid = int(str(custom_id).lstrip("c"))
            except ValueError:
                continue
            seen.add(cid)
            if data is None:
                failed += 1
                retry = err.startswith(("expired", "canceled"))
                await db.update_candidate(cid, status="triaged" if retry else "error", note=err, batch_id=None)
                continue
            try:
                if await finish(cid, data):
                    made += 1
            except Exception as exc:
                log.exception("Итог оценки %s", cid)
                await db.mark_candidate(cid, "error", repr(exc))
        for c in await db.candidates("batched"):
            if c["batch_id"] == b["id"] and c["id"] not in seen:
                await db.update_candidate(c["id"], status="triaged", batch_id=None)
        await db.close_batch(b["id"])
        closed.append({"manual": bool(b["manual"]), "made": made, "failed": failed})
    return closed


# ---------- текст большого поста: пишется, когда пост выбран ----------

def post_images(post) -> list[str]:
    return [p for p in json.loads(post["images"] or "[]") if Path(p).exists()]


async def ensure_text(pid: int, comment: str = "") -> object:
    """Большой пост без текста → пишет текст (Opus). С comment — переписывает. → свежая строка поста."""
    post = await db.get_post(pid)
    data = json.loads(post["data"])
    if post["format"] != "std" or (formatter.has_body(data) and not comment):
        return post
    body, hits = await curator.write_body(data, data.get("_source_text", ""), post_images(post), comment)
    if not body:
        raise RuntimeError("Claude вернул пустой текст")
    data["body"] = body
    data.pop("_manual", None)
    data["flags"] = [f for f in (data.get("flags") or []) if not str(f).startswith(("штамп", "длинная подпись"))]
    if hits:
        data["flags"].append("штамп в тексте: " + ", ".join(hits))
    caption = formatter.build_caption(data, "std")
    if formatter.visible_len(caption) > config.CAPTION_LIMIT:
        data["flags"].append("длинная подпись — уйдёт отдельным сообщением")
    await db.update_post(pid, data=data, caption=caption)
    return await db.get_post(pid)


async def rewrite(pid: int, comment: str) -> object:
    post = await db.get_post(pid)
    data = json.loads(post["data"])
    fmt = post["format"]
    if fmt == "std":
        return await ensure_text(pid, comment or "Перепиши живее и конкретнее.")
    if fmt == "notes":
        new = await curator.rewrite_notes(data, data.get("_source_text", ""), comment)
        for k, v in data.items():
            if k.startswith("_"):
                new.setdefault(k, v)
        new["flags"] = new.get("flags") or []
        await db.update_post(pid, data=new, caption=formatter.build_caption(new, "notes"))
        return await db.get_post(pid)
    out = await curator.rewrite_mini(data, data.get("_source_text", ""), comment, post_images(post))
    line = str(out.get("mini_line") or "").strip()
    if line:
        data["mini_line"] = line
    if [p for p in (out.get("headline_parts") or []) if p]:
        data["headline_parts"] = out["headline_parts"]
    data.pop("_manual", None)
    data["flags"] = [f for f in (data.get("flags") or []) if not str(f).startswith("штамп")]
    hits = voice.check(line, await voice.banned())
    if hits:
        data["flags"].append("штамп во фразе: " + ", ".join(hits))
    await db.update_post(pid, data=data, caption=formatter.build_caption(data, "mini"))
    return await db.get_post(pid)


async def set_format(pid: int, fmt: str, write: bool = True) -> object:
    """Мини ⇄ большой. Большому без текста текст пишется сразу (write=True) или при выборе."""
    post = await db.get_post(pid)
    data = json.loads(post["data"])
    data.pop("_manual", None)
    n = len(json.loads(post["images"]))
    if fmt == "std" and n - len(data.get("_excluded") or []) < config.MIN_PHOTOS_ARTICLE:
        data["_excluded"] = []   # мини жил на 1–4 фото, большому нужны все
    await db.update_post(pid, format=fmt, data=data, caption=formatter.build_caption(data, fmt))
    if fmt == "std" and write and not formatter.has_body(data):
        return await ensure_text(pid)
    return await db.get_post(pid)


# ---------- пост по ссылке ----------

async def process_link(url: str) -> tuple[int | None, str]:
    """Пост из ссылки, которую прислал автор: оценка и текст сразу, без пакета."""
    await db.add_candidate(url, "link", "", {})
    cand = await db.get_candidate_by_url(url)
    old = await db.post_by_candidate(cand["id"])
    if old and old["status"] == "ready":
        return old["id"], "уже был в запасе"
    if old and old["status"] in ("sent", "announced"):
        return old["id"], "уже во входящих"
    if old and old["status"] == "approved":
        return None, "этот материал уже стоит в слоте"
    if old and old["status"] == "published":
        return None, "этот материал уже опубликован"
    async with httpx.AsyncClient(headers={"User-Agent": config.USER_AGENT}) as client:
        prep = await _prepare(client, cand, force=True)
    if isinstance(prep, str):
        await db.mark_candidate(cand["id"], "skipped", prep)
        return None, prep
    await db.update_candidate(cand["id"], prep=prep)
    cand = await db.get_candidate(cand["id"])
    data = await curator.evaluate(_params(await curator.eval_context(), cand, prep, forced=True), background=False)
    pid = await finish(cand["id"], data, forced=True)
    if not pid:
        return None, "не получилось собрать пост"
    post = await db.get_post(pid)
    if post["format"] == "std":
        await ensure_text(pid)
    return pid, "ok"


# ---------- #ahmagnotes ----------

async def build_notes_post(nid: int) -> int:
    """Заметка по утверждённому плану: фото со страниц-источников и из Wikimedia Commons, текст, пост."""
    note = await db.get_note(nid)
    brief = json.loads(note["brief"])
    folder = config.IMG_DIR / f"n{nid}"
    urls: list[str] = []
    async with httpx.AsyncClient(headers={"User-Agent": config.USER_AGENT}) as client:
        for page in (brief.get("page_urls") or [])[:3]:
            try:
                art = await media.extract_article(client, page)
                urls += art["image_urls"][:12]
            except Exception as exc:
                log.info("notes: страница без фото (%s): %s", exc, page)
        for q in (brief.get("image_queries") or [])[:5]:
            urls += await commons.search(client, q)
        images = await media.download_images(client, list(dict.fromkeys(urls))[:40], folder) if urls else []

    data = await curator.notes_write(brief, images)
    data["_source_text"] = curator.brief_text(brief)
    data["_sources"] = (brief.get("sources") or [])[:8]
    data["flags"] = data.get("flags") or []
    if brief.get("_no_search"):
        data["flags"].append("собрано без веб-поиска — проверьте факты")
    if not images:
        data["flags"].append("фото не нашлись — заметка выйдет текстом")
    hits = voice.check(data.get("body", ""), await voice.banned())
    if hits:
        data["flags"].append("штамп в тексте: " + ", ".join(hits))
    order = _order(data, len(images))[: config.MAX_PHOTOS] if images else []
    chosen = [str(images[i]) for i in order]
    caption = formatter.build_caption(data, "notes")
    src = data["_sources"][0].get("url", "") if data["_sources"] else ""
    pid = await db.add_post(
        candidate_id=None, source="notes", url=src, category="notes", format="notes",
        data=data, caption=caption, score=0, reason=brief.get("thesis", ""),
        images=chosen, status="ready",
    )
    await db.update_note(nid, status="written", post_id=pid)
    return pid


# ---------- выбор поста ----------

async def files_ok(post) -> bool:
    """Файлы фото на месте? Если volume отвалился, пост снимаем, а не падаем при отправке."""
    images = json.loads(post["images"] or "[]")
    if not images or all(Path(p).exists() for p in images):
        return True
    await db.update_post(post["id"], status="auto_rejected", reject_reason="файлы фото пропали с диска")
    log.warning("Пост %s снят: файлов нет на диске", post["id"])
    return False


async def pick_next(fmt: str | None = None, category: str | None = None, exclude: set[int] | None = None,
                    min_score: int = 0, clean_only: bool = False, skip_sources: set[str] | None = None,
                    planned: list | None = None, find_slot: bool = False):
    """Лучший пост из запаса. find_slot — это слот находки: сначала пробуем находку.
    В остальных слотах последние niche.FIND_RESERVE находок не трогаем — они ждут своего слота."""
    skip = set(skip_sources or ())
    kw = dict(category=category, exclude=exclude, min_score=min_score, clean_only=clean_only, planned=planned)
    if find_slot:
        others = {p["source"] for p in await db.ready_posts()} - niche.FINDS
        post = await _pick(fmt, skip_sources=skip | others, **kw)
        if post:
            return post
    else:
        finds = await ready_finds()
        if finds and len(finds) <= niche.FIND_RESERVE:
            post = await _pick(fmt, skip_sources=skip | niche.FINDS, **kw)
            if post:
                return post
    return await _pick(fmt, skip_sources=skip, **kw)


async def _pick(fmt: str | None = None, category: str | None = None, exclude: set[int] | None = None,
                min_score: int = 0, clean_only: bool = False, skip_sources: set[str] | None = None,
                planned: list | None = None):
    """Лучший пост из запаса с поправкой на баланс рубрик (50% архитектура, 50% остальное).
    Если нужного формата нет, берёт другой: большой сжимается до мини, мини с 3+ фото становится большим."""
    exclude = exclude or set()
    planned = planned or []
    ready = [p for p in await db.ready_posts(None, category)
             if p["format"] != "notes" and p["id"] not in exclude and (p["score"] or 0) >= min_score
             and p["source"] not in (skip_sources or set())]
    if clean_only:
        ready = [p for p in ready if not (json.loads(p["data"]).get("flags") or [])]
    museums_today = sum(1 for r in await db.sent_today() if r["source"] in db.MUSEUMS) \
        + sum(1 for p in planned if p["source"] in db.MUSEUMS)
    if museums_today >= config.MUSEUM_DAILY_MAX:
        ready = [p for p in ready if p["source"] not in db.MUSEUMS]
    if fmt:
        exact = [p for p in ready if p["format"] == fmt]
        if exact:
            ready = exact
        elif fmt == "mini":
            ready = [p for p in ready if p["format"] == "std"]
        else:
            ready = [p for p in ready if len(json.loads(p["images"])) >= config.MIN_PHOTOS_ARTICLE]
    ready = [p for p in ready if await files_ok(p)]
    if not ready:
        return None
    mix = await db.recent_mix(7) + [norm_cat(p["category"]) for p in planned]
    total = len(mix) + 1

    def weight(p):
        cat = norm_cat(p["category"])
        return (p["score"] or 0) + 4 * (config.TARGET_MIX.get(cat, 0.05) - mix.count(cat) / total)

    best = max(ready, key=weight)
    if fmt and best["format"] != fmt:
        return await set_format(best["id"], fmt, write=False)
    return best


async def untrusted_sources() -> set[str]:
    """Источники, которые автор чаще отклоняет, чем берёт (от 5 решений)."""
    out = set()
    for src, s in (await db.source_stats()).items():
        decided = s["published"] + s["rejected"]
        if decided >= 5 and s["published"] / decided < 0.5:
            out.add(src)
    return out


def auto_ok(post, untrusted: set[str] | None = None) -> bool:
    """Пост годится выйти без автора: оценка от AUTO_MIN_SCORE, без замечаний, источник не под подозрением."""
    if not post or post["format"] == "notes" or (post["score"] or 0) < config.AUTO_MIN_SCORE:
        return False
    if json.loads(post["data"]).get("flags"):
        return False
    return post["source"] not in (untrusted or set())


async def pick_auto(fmt: str, find_slot: bool = False):
    """Для автомата и страховки: только высокая оценка, без флагов, из источников, которым автор доверяет."""
    return await pick_next(fmt, min_score=config.AUTO_MIN_SCORE, clean_only=True,
                           skip_sources=await untrusted_sources(), find_slot=find_slot)
