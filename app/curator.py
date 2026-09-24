"""Все обращения к Claude: первичный фильтр (Haiku), оценка (Sonnet, пакетами и с кэшем),
тексты больших постов и заметок (Opus), учёт расходов в долларах."""
import json
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import anthropic
from anthropic import AsyncAnthropic

from app import config, db, media, voice

log = logging.getLogger(__name__)
client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)


# ---------- профиль вкуса ----------

def _taste_profile(raw: str) -> str:
    """Из профиля убран раздел «5. Голос» с примерами: его приёмы («не X, а Y», афоризм в конце,
    «выверенный», «сдержанный») звучали как нейросеть. Голос теперь задаёт voice.RULES."""
    out = re.sub(r"\n## 5\..*?(?=\n## 6\.)", "\n", raw, flags=re.S)
    out = re.sub(r"\*\*Целевой микс[^\n]*\n", "", out)
    return out


PROFILE = _taste_profile(config.PROFILE_PATH.read_text(encoding="utf-8"))
ARCHIVE = json.loads(config.ARCHIVE_PATH.read_text(encoding="utf-8"))
def _within_repeat_window(p: dict) -> bool:
    """Пост из архива канала вышел не раньше REPEAT_DAYS назад? Старше — объект можно показать снова."""
    try:
        day = datetime.strptime(p.get("date", ""), "%d %B %Y").date()
    except ValueError:
        return True
    return day >= date.today() - timedelta(days=config.REPEAT_DAYS)


ARCHIVE_HEADLINES = [p["headline"] for p in ARCHIVE if p.get("headline") and _within_repeat_window(p)]
NOTES_HEADLINES = [p["headline"] for p in ARCHIVE if "ahmagnotes" in (p.get("tags") or []) and p.get("headline")]
# Примеры заголовков и тегов из архива — только формат, без текстов
HEADLINE_EXAMPLES = "\n".join(
    f"{p['headline']}  →  " + " ".join("#" + t for t in (p.get("tags") or []))
    for p in [p for p in ARCHIVE if " // " in (p.get("headline") or "") and "ahmagnotes" not in (p.get("tags") or [])][:8])


# ---------- ошибки ----------

class BudgetExceeded(RuntimeError):
    """Достигнут дневной потолок фоновых трат или вызовов."""


class NoCredits(RuntimeError):
    """На счёте Anthropic закончились средства."""


class ApiDown(RuntimeError):
    """API Anthropic временно недоступен."""


def explain(exc: Exception) -> str:
    """Человеческое объяснение ошибки для сообщения в чат."""
    if isinstance(exc, NoCredits):
        return ("На счёте Anthropic закончились средства. Пополните баланс: "
                "console.anthropic.com → Plans & Billing → Add credits. После этого всё заработает само.")
    if isinstance(exc, BudgetExceeded):
        return (f"Дневной потолок фоновых трат исчерпан (${config.DAILY_BUDGET_USD:.2f} или "
                f"{config.DAILY_API_CALLS_MAX} вызовов). Сбор продолжится завтра; поднять потолок — "
                "переменная DAILY_BUDGET_USD. Кнопки работают как обычно.")
    if isinstance(exc, ApiDown):
        return "API Anthropic сейчас недоступен. Обычно это ненадолго — попробуйте через несколько минут."
    return f"Что-то пошло не так: {exc!r}"


def _map_error(exc: Exception) -> Exception:
    if isinstance(exc, anthropic.APIStatusError):
        detail = str(getattr(exc, "message", "") or exc).lower()
        if "credit balance" in detail or "billing" in detail:
            return NoCredits()
        if exc.status_code in (429, 500, 502, 503, 529):
            return ApiDown()
    if isinstance(exc, anthropic.APIConnectionError):
        return ApiDown()
    return exc


# ---------- расход ----------

# $ за миллион токенов: вход, выход, запись в кэш (5 мин), чтение из кэша
PRICES = {
    "opus": (5.0, 25.0, 6.25, 0.5),
    "sonnet": (2.0, 10.0, 2.5, 0.2),
    "haiku": (1.0, 5.0, 1.25, 0.1),
}


def cost_of(model: str, usage, batch: bool = False) -> float:
    p = next((v for k, v in PRICES.items() if k in (model or "")), PRICES["sonnet"])
    cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
    c = ((usage.input_tokens or 0) * p[0] + (usage.output_tokens or 0) * p[1] + cw * p[2] + cr * p[3]) / 1e6
    if batch:
        c *= 0.5
    stu = getattr(usage, "server_tool_use", None)
    searches = getattr(stu, "web_search_requests", 0) or 0 if stu else 0
    return c + searches * 0.01


async def _record(model: str, usage, background: bool, batch: bool = False) -> None:
    try:
        cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cr = getattr(usage, "cache_read_input_tokens", 0) or 0
        await db.add_usage((usage.input_tokens or 0) + cw + cr, usage.output_tokens or 0,
                           cost_of(model, usage, batch), background)
    except Exception:
        log.warning("расход не записан", exc_info=True)


async def budget_ok() -> bool:
    return (await db.calls_today() < config.DAILY_API_CALLS_MAX
            and await db.cost_today(background=True) < config.DAILY_BUDGET_USD)


# ---------- общий вызов ----------

def _system(text: str) -> list[dict]:
    """Системный промпт неизменный — кэшируем: повторное чтение стоит десятую часть цены."""
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def _img(path, size: int | None = None) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": media.thumb_b64(Path(path), size or config.THUMB_SIZE)}}


def _parse_json(text: str) -> dict:
    text = re.sub(r"```(?:json)?", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"Claude вернул не JSON: {text[:200]!r}")
    return json.loads(text[start:end + 1])


def _public(data: dict) -> dict:
    """Без служебных полей (_source_text и т.п.) — чтобы не гонять их в промпт."""
    return {k: v for k, v in data.items() if not k.startswith("_")}


async def _create(params: dict, background: bool) -> str:
    if background and not await budget_ok():
        raise BudgetExceeded()
    messages = list(params["messages"])
    text = ""
    for _ in range(4):  # веб-поиск может вернуть pause_turn — тогда продолжаем тот же ход
        try:
            resp = await client.messages.create(**{**params, "messages": messages})
        except Exception as exc:
            raise _map_error(exc) from exc
        await _record(params["model"], resp.usage, background)
        text += "".join(b.text for b in resp.content if b.type == "text")
        if resp.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": resp.content})
    return text


async def _call(content: list | str, *, system: str, model: str, max_tokens: int = 2000,
                tools: list | None = None, background: bool = False) -> dict:
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    params = {"model": model, "max_tokens": max_tokens, "system": _system(system),
              "messages": [{"role": "user", "content": content}]}
    if tools:
        params["tools"] = tools
    return _parse_json(await _create(params, background))


async def ping() -> None:
    """Дешёвая проверка доступа к API — для /diag."""
    try:
        await client.messages.create(model=config.TRIAGE_MODEL, max_tokens=4,
                                     messages=[{"role": "user", "content": "ping"}])
    except Exception as exc:
        raise _map_error(exc) from exc


# ---------- 1. первичный фильтр (Haiku, текст без фото) ----------

TRIAGE_SYSTEM = """Ты — первый фильтр для Telegram-канала AHMAG об архитектуре, искусстве, фотографии, архивах и кино. По заголовку и началу текста реши, стоит ли показывать материал редактору. Подробно его посмотрят потом; сейчас важно отсеять явно чужое.

yes — похоже на вкус канала:
• построенная архитектура и интерьеры: частные дома, небольшие объекты малоизвестных бюро, модернистская и послевоенная классика, сакральное, руины, мемориалы, переделка старых зданий; естественные материалы, свет, связь с ландшафтом;
• искусство без кича: сюрреализм и тихая метафизика, лэнд-арт, объекты в среде, мастер за работой, визуальная культура (гравюры, манускрипты, вывески, мультипликация), тёплый юмор;
• документальная, уличная, архивная фотография, этнография;
• исторические серии и находки из прошлого;
• авторское кино с сильной визуальной стороной, закулисье съёмок.

no — не наше:
• рендеры, конкурсы, концепции и неосуществлённые проекты; небоскрёбы, девелоперские комплексы, офисы, торговые центры, сетевые отели;
• продуктовый и промышленный дизайн, мебель, гаджеты, мода, автомобили, еда;
• новости индустрии, премии, вакансии, мероприятия, подборки и рейтинги, реклама, интервью без конкретной работы, политика и скандалы;
• обзоры мейнстримного кино и сериалов.

maybe — если не ясно.

Архитектуры в потоке много, к ней будь строже. К искусству, фотографии, архиву и кино — мягче.
Рубрика cat: architecture (включая интерьеры), art, photography, archive, cinema.

Верни ТОЛЬКО JSON: {"r": [{"i": 0, "v": "yes|maybe|no", "cat": "architecture", "why": "3–6 слов"}]}"""


async def triage(items: list[dict]) -> dict[int, dict]:
    """items: [{i, source, hint, title, excerpt}] → {i: {v, cat, why}}"""
    lines = [f"[{it['i']}] {it['source']} ({it['hint']}) | {it['title']}\n{it['excerpt']}" for it in items]
    data = await _call("\n\n".join(lines), system=TRIAGE_SYSTEM, model=config.TRIAGE_MODEL,
                       max_tokens=60 * len(items) + 200, background=True)
    out = {}
    for r in data.get("r") or []:
        try:
            out[int(r["i"])] = r
        except (KeyError, TypeError, ValueError):
            continue
    return out


# ---------- 2. оценка (Sonnet, с фото) ----------

EVAL_SYSTEM = f"""Ты — редактор-куратор Telegram-канала AHMAG. Ниже профиль канала: вкус, темы и формат.

{PROFILE}

{voice.RULES}

# Твоя задача
Тебе дают материал-кандидат: текст источника и пронумерованные превью фото.
1. Проверь стоп-лист и повтор: сравни со списком «Уже опубликовано» ниже и с недавними заголовками из сообщения.
2. Оцени соответствие вкусу канала по шкале 0–10 (раздел 6 профиля). Будь строгим: 7 и выше — только то, что автор канала опубликовал бы сам.
3. Если оценка не ниже {config.SCORE_THRESHOLD}: определи рубрику и формат, составь заголовок, кредиты, теги, одну фразу для мини-поста и порядок фото. Основной текст большого поста сейчас НЕ пиши: его напишут отдельно, если пост выберут.

# Рубрика (category)
architecture — архитектура и интерьеры; art — искусство, скульптура, инсталляции, выставки, музейные предметы; photography — фотография; archive — исторические серии, старые снимки и документы визуальной культуры; cinema — кино.

# Формат (format)
Канал выходит в пропорции примерно 70% мини-постов и 30% больших.
- "std" — большой пост: заголовок, 1–2 абзаца, кредиты, теги. Только если есть что рассказать: история, приём, контекст, судьба вещи. И нужно не меньше {config.MIN_PHOTOS_ARTICLE} хороших фото.
- "mini" — всё остальное: заголовок, одна простая фраза, кредиты и теги, от 1 до {config.MINI_MAX_PHOTOS} фото. Если сомневаешься — mini.

# Фраза мини-поста (mini_line)
Одна простая фраза до 140 знаков: что это за вещь и что видно на фото, как сказал бы человек в переписке. Пиши её почти всегда, и для std тоже (пост могут сжать до мини). null — только если заголовок уже сказал всё.
Хорошо: «Бетонная часовня посреди поля, внутри обугленные стены и дыра в потолке.» · «Большая волна в Канагаве Хокусая, та самая, с маленькой Фудзи на заднем плане.» · «Ночной Париж Брассаи: туман, фонари и мокрая брусчатка.»
Плохо: «Архитектура, которая растворяется в тишине.» · «Не дом, а манифест.» · «Гармония света и материала.»

# Заголовок, кредиты, теги
- Заголовок и кредиты — по правилам раздела 4 профиля. Только факты из материала: неизвестные год, город, фотограф — null. Не выдумывай.
- Теги: 2–3, строчными, с префиксом ahmag: сначала рубрика (architecture, interiors, art, sculpture, photography, archive, cinema), затем страна по-английски одним словом. Для исторического материала добавь ahmagarchive.
- Так выглядят заголовки канала:
{HEADLINE_EXAMPLES}

# Фото
photo_order — индексы превью в порядке публикации: для std от {config.MIN_PHOTOS_ARTICLE} до {config.EVAL_PHOTOS}, для mini от 1 до {config.MINI_MAX_PHOTOS}. Последовательность: общий план → детали и материал → интерьер и свет. Исключай слабые, повторяющиеся, с текстом поверх, чертежи без необходимости.

# Уже опубликовано в канале (не повторять)
{chr(10).join(ARCHIVE_HEADLINES)}

# Формат ответа
Верни ТОЛЬКО JSON без пояснений и без markdown:
{{
  "stoplist": false,
  "already_posted": false,
  "score": 0,
  "score_reason": "одна фраза по-русски, почему такая оценка",
  "category": "architecture|art|photography|archive|cinema",
  "format": "std|mini",
  "headline_parts": ["Название", "Автор/бюро или null", "Город, Страна, Год или null"],
  "mini_line": "одна простая фраза или null",
  "credits": {{"pr": null, "pr_url": null, "ph": null, "ph_url": null, "via": null}},
  "tags": ["ahmagarchitecture", "ahmagjapan"],
  "photo_order": [0, 1, 2],
  "flags": ["нет ph"]
}}
Если оценка ниже {config.SCORE_THRESHOLD}, stoplist или already_posted — достаточно полей stoplist, already_posted, score, score_reason и category."""


async def eval_context() -> str:
    """Общая для всех кандидатов прохода часть: недавние заголовки и отказы автора."""
    recent = await db.recent_headlines()
    parts = ["# Недавно в канале и в очереди (не повторять)\n" + ("\n".join(recent) or "—")]
    rej = await db.recent_rejections()
    if rej:
        parts.append("# Недавно отклонено автором — учитывай при оценке\n" + "\n".join(
            f"- {json.loads(r['data']).get('headline', '?')} — {r['reject_reason']}" for r in rej))
    from app import taste
    rules = await taste.active_text("select")
    if rules:
        parts.append(rules)
    return "\n\n".join(parts)


def eval_params(context: str, source: str, url: str, title: str, text: str, images: list,
                allow_std: bool = True, forced: bool = False) -> dict:
    extra = []
    if not allow_std:
        extra.append(f"Качественных фото меньше {config.MIN_PHOTOS_ARTICLE}: возможен только формат mini.")
    if forced:
        extra.append("Автор канала сам прислал эту ссылку. Оценку поставь честно, но заполни все поля, "
                     "даже при низкой оценке или совпадении со стоп-листом (отметь это во flags).")
    content: list = [
        {"type": "text", "text": context, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": (
            f"# Кандидат\nИсточник: {source}\nURL: {url}\nЗаголовок: {title}\n\n"
            f"Текст:\n{text[:config.EVAL_TEXT_CHARS]}\n\n"
            + ("# Важно\n" + "\n".join(extra) + "\n\n" if extra else "")
            + f"# Превью фото ({len(images)} шт., индексы по порядку)")},
    ]
    for i, p in enumerate(images):
        content.append({"type": "text", "text": f"Фото {i}:"})
        content.append(_img(p))
    return {"model": config.CLAUDE_MODEL, "max_tokens": 1500, "system": _system(EVAL_SYSTEM),
            "messages": [{"role": "user", "content": content}]}


async def evaluate(params: dict, background: bool = True) -> dict:
    """Оценка сразу, без пакета: пост по ссылке и срочный сбор при пустом запасе."""
    return _parse_json(await _create(params, background))


# ---------- пакеты (Message Batches): вдвое дешевле, ответ — в пределах суток ----------

async def batch_create(requests: list[dict]) -> str:
    if not await budget_ok():
        raise BudgetExceeded()
    try:
        batch = await client.messages.batches.create(requests=requests)
    except Exception as exc:
        raise _map_error(exc) from exc
    return batch.id


async def batch_status(bid: str) -> tuple[bool, dict]:
    """→ (закончен ли, счётчики)"""
    try:
        b = await client.messages.batches.retrieve(bid)
    except Exception as exc:
        raise _map_error(exc) from exc
    counts = b.request_counts.model_dump() if b.request_counts else {}
    return b.processing_status == "ended", counts


async def batch_results(bid: str):
    """Асинхронно отдаёт (custom_id, data | None, ошибка | None) и записывает расход по тарифу пакетов."""
    try:
        results = await client.messages.batches.results(bid)
    except Exception as exc:
        raise _map_error(exc) from exc
    async for entry in results:
        res = entry.result
        if res.type != "succeeded":
            detail = ""
            if res.type == "errored":
                detail = str(getattr(getattr(res, "error", None), "error", "") or getattr(res, "error", ""))[:200]
            yield entry.custom_id, None, f"{res.type} {detail}".strip()
            continue
        msg = res.message
        await _record(msg.model or config.CLAUDE_MODEL, msg.usage, background=True, batch=True)
        text = "".join(b.text for b in msg.content if b.type == "text")
        try:
            yield entry.custom_id, _parse_json(text), None
        except Exception as exc:
            yield entry.custom_id, None, f"не JSON: {exc}"[:200]


# ---------- 3. тексты (Opus) ----------

WRITER_SYSTEM = f"""Ты пишешь тексты для Telegram-канала AHMAG об архитектуре, искусстве, фотографии и кино. Автор канала — архитектор по образованию. Пишет для людей со вкусом, без снобизма и без восторгов.

{voice.RULES}

# Большой пост
- Основной текст (body): 1–2 абзаца, всего 250–600 знаков. Абзацы разделяй пустой строкой.
- Выбери одну-две вещи, которые действительно стоит рассказать: историю, приём, материал, место, судьбу здания, деталь на фото. Не пытайся пересказать всё.
- Только факты из материала. Не выдумывай ни дат, ни цифр, ни имён.
- Разметка: можно одно выделение <b> или <i>, лучше без них.
- Без заголовка, кредитов и хэштегов — их добавят отдельно.

# Фраза мини-поста
Одна простая фраза до 140 знаков: что это и что видно на фото, как сказал бы человек в переписке.

{voice.EXAMPLES}"""


async def _voice_context() -> str:
    parts = []
    edits = await db.recent_edits(5)
    if edits:
        parts.append("# Так автор правит тексты бота — пиши сразу как во второй версии\n" + "\n\n".join(
            f"Было: {e['before'][:700]}\nСтало: {e['after'][:700]}" for e in edits))
    banned = await voice.banned()
    if banned:
        parts.append("# Автор запретил эти слова и обороты\n" + "; ".join(banned))
    from app import taste
    rules = await taste.active_text("write")
    if rules:
        parts.append(rules)
    return "\n\n".join(parts)


def _post_brief(data: dict) -> str:
    return (f"Заголовок: {' // '.join(p for p in (data.get('headline_parts') or []) if p) or data.get('headline', '')}\n"
            f"Фраза мини-поста: {data.get('mini_line') or '—'}\n"
            f"Рубрика: {data.get('category') or '—'}")


async def write_body(data: dict, source_text: str, images: list, comment: str = "") -> tuple[str, list[str]]:
    """Основной текст большого поста. → (текст, штампы, которые не ушли после одной правки)"""
    ctx = await _voice_context()
    head = (f"# Пост\n{_post_brief(data)}\n\n# Материал источника\n{(source_text or '')[:5000]}\n\n"
            + (ctx + "\n\n" if ctx else ""))
    if comment or data.get("body"):
        head += f"# Текущая версия текста\n{data.get('body') or '—'}\n\n"
    if comment:
        head += f"# Комментарий автора\n{comment}\n\n"
    head += "Напиши основной текст большого поста. Верни ТОЛЬКО JSON: {\"body\": \"...\"}"
    content: list = [{"type": "text", "text": head}]
    for p in images[:3]:
        content.append(_img(p))
    out = await _call(content, system=WRITER_SYSTEM, model=config.WRITER_MODEL, max_tokens=1500)
    body = str(out.get("body") or "").strip()
    hits = voice.check(body, await voice.banned())
    if hits and body:  # одна попытка убрать штампы
        fix = await _call(
            f"# Текст\n{body}\n\nВ тексте есть то, чего в канале быть не должно: {', '.join(hits)}. "
            "Перепиши без этого, сохрани факты и длину. Верни ТОЛЬКО JSON: {\"body\": \"...\"}",
            system=WRITER_SYSTEM, model=config.WRITER_MODEL, max_tokens=1500)
        body = str(fix.get("body") or body).strip()
        hits = voice.check(body, await voice.banned())
    return body, hits


async def rewrite_mini(data: dict, source_text: str, comment: str, images: list) -> dict:
    ctx = await _voice_context()
    content: list = [{"type": "text", "text": (
        f"# Пост\n{_post_brief(data)}\n\n# Материал источника\n{(source_text or '')[:3000]}\n\n"
        + (ctx + "\n\n" if ctx else "")
        + f"# Комментарий автора\n{comment or 'Перепиши фразу проще и конкретнее.'}\n\n"
        "Перепиши фразу мини-поста; заголовок меняй, только если об этом просит комментарий. "
        "Верни ТОЛЬКО JSON: {\"mini_line\": \"...\", \"headline_parts\": [\"...\"]}")}]
    for p in images[:2]:
        content.append(_img(p))
    return await _call(content, system=WRITER_SYSTEM, model=config.WRITER_MODEL, max_tokens=600)


FIX_LINE_SYSTEM = """Ты правишь одну короткую фразу для Telegram-канала об архитектуре и искусстве. Фраза должна быть простой и человечной: что это за вещь и что видно на фото. Без противопоставлений «не X, а Y», без афоризмов, без слов «гармония», «баланс», «диалог», «выверенный», «сдержанный», «подчёркивает», «уникальный». Только факты из исходной фразы."""


async def fix_line(line: str, hits: list[str], headline: str) -> str:
    out = await _call(
        f"Заголовок поста: {headline}\nФраза: {line}\nУбери: {', '.join(hits)}.\n"
        "Верни ТОЛЬКО JSON: {\"line\": \"...\"}",
        system=FIX_LINE_SYSTEM, model=config.TRIAGE_MODEL, max_tokens=300, background=True)
    return str(out.get("line") or "").strip()


# ---------- #ahmagnotes ----------

NOTES_SYSTEM = f"""Ты — редактор Telegram-канала AHMAG и ведёшь рубрику #ahmagnotes: длинные авторские заметки об архитектуре, искусстве, фотографии и кино. Ниже профиль канала: вкус и темы.

{PROFILE}

{voice.RULES}

# Правила заметок
- Заметка — не энциклопедическая справка, а одна мысль, развёрнутая на материале: приём, линия влияния, судьба здания, взгляд автора.
- Только проверяемые факты. Даты, имена, цифры — лишь те, что подтверждены источниками. Сомневаешься — не пиши.
- Русский язык. Без подзаголовков и списков.
- Стоп-лист канала действует и здесь."""

NOTE_FORMAT = """{
  "headline_parts": ["Заголовок заметки", "подзаголовок или null"],
  "body": "текст заметки",
  "credits": {"pr": null, "pr_url": null, "ph": null, "ph_url": null, "via": null},
  "tags": ["ahmagnotes", "ahmagarchitecture", "ahmagjapan"],
  "photo_order": [0, 1, 2],
  "flags": ["что автору стоит перепроверить"]
}"""

BRIEF_FORMAT = """{
  "title": "рабочий заголовок",
  "thesis": "главная мысль заметки одной-двумя фразами",
  "plan": ["о чём первый абзац", "о чём второй", "..."],
  "facts": [{"text": "проверенный факт", "url": "откуда"}],
  "sources": [{"title": "название страницы", "url": "https://..."}],
  "page_urls": ["страницы с хорошими фотографиями по теме (до 3)"],
  "image_queries": ["запросы на английском для поиска фото в Wikimedia Commons (3–5)"]
}"""


async def notes_topics(avoid: list[str]) -> list[dict]:
    recent = [r["data"] for r in await db.published_posts(25)]
    heads = [json.loads(d).get("headline", "") for d in recent]
    content = (
        "Предложи 5 тем для заметки #ahmagnotes.\n\n"
        "# О чём канал писал в последнее время (темы могут расти отсюда, но не повторять посты)\n"
        + "\n".join(h for h in heads if h) + "\n\n"
        "# Заметки, которые уже выходили или предлагались (не повторять)\n"
        + "\n".join(NOTES_HEADLINES + avoid) + "\n\n"
        "Темы должны быть конкретными (не «японская архитектура», а один приём, один автор, одна линия, одно здание "
        "и его судьба), разными по рубрикам и такими, по которым есть достоверные открытые источники и хорошие фото.\n\n"
        'Верни ТОЛЬКО JSON: {"topics": [{"title": "короткое название темы", "angle": "одна фраза: в чём мысль заметки"}]}'
    )
    data = await _call(content, system=NOTES_SYSTEM, model=config.CLAUDE_MODEL, max_tokens=1200)
    return [t for t in data.get("topics", []) if t.get("title")][:6]


async def notes_research(topic: str, angle: str = "") -> dict:
    """Сбор материала с веб-поиском. Если поиск недоступен — без него, с пометкой."""
    prompt = (
        f"Тема заметки #ahmagnotes: {topic}\n" + (f"Угол: {angle}\n" if angle else "") + "\n"
        "Собери материал: найди достоверные источники (музеи, архивы, профильные издания, монографии, "
        "интервью), выпиши факты с адресами страниц, сформулируй главную мысль и план на 4–7 абзацев. "
        "Факты — только те, что нашёл в источниках; к каждому — url.\n\n"
        f"Верни ТОЛЬКО JSON:\n{BRIEF_FORMAT}"
    )
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": config.NOTES_WEB_SEARCHES}]
    try:
        brief = await _call(prompt, system=NOTES_SYSTEM, model=config.CLAUDE_MODEL, max_tokens=4000, tools=tools)
    except (NoCredits, ApiDown):
        raise
    except Exception as exc:
        log.warning("Веб-поиск не сработал (%r), собираю без него", exc)
        brief = await _call(prompt + "\n\nВеб-поиск недоступен: опирайся только на то, в чём уверен, url оставляй пустыми.",
                            system=NOTES_SYSTEM, model=config.CLAUDE_MODEL, max_tokens=3000)
        brief["_no_search"] = True
    brief["topic"] = topic
    return brief


async def notes_replan(brief: dict, comment: str) -> dict:
    content = (
        f"# Собранный материал (JSON)\n{json.dumps(_public(brief), ensure_ascii=False)}\n\n"
        f"# Комментарий автора к плану\n{comment}\n\n"
        "Поправь thesis и plan по комментарию. Факты и источники не выдумывай: оставь те, что есть, "
        "лишние можно убрать. Верни полный JSON в том же формате."
    )
    new = await _call(content, system=NOTES_SYSTEM, model=config.CLAUDE_MODEL, max_tokens=3000)
    for k in ("topic", "_no_search"):
        if k in brief:
            new.setdefault(k, brief[k])
    for k in ("facts", "sources", "page_urls", "image_queries"):
        if not new.get(k):
            new[k] = brief.get(k, [])
    return new


def brief_text(brief: dict) -> str:
    facts = "\n".join(f"- {f.get('text', '')} ({f.get('url', '')})" for f in brief.get("facts", []))
    return (f"Тема: {brief.get('topic', '')}\nМысль: {brief.get('thesis', '')}\n"
            f"План:\n" + "\n".join(f"{i + 1}. {p}" for i, p in enumerate(brief.get("plan", []))) +
            f"\n\nФакты:\n{facts}")


async def notes_write(brief: dict, images: list[Path]) -> dict:
    ctx = await _voice_context()
    content: list = [{"type": "text", "text": (
        f"# Материал\n{brief_text(brief)}\n\n" + (ctx + "\n\n" if ctx else "")
        + "Напиши заметку по плану. Только факты из материала. Объём body — 1500–2800 знаков, 4–7 абзацев, "
        "абзацы разделяй пустой строкой; допустимы <b> и <i> (до 2 выделений). Первый тег — ahmagnotes, "
        "затем рубрика и страна.\n"
        + (f"photo_order — индексы превью в порядке публикации (до 10), слабые и не по теме исключай.\n\n"
           f"# Превью фото ({len(images)} шт.)" if images else "Фото нет: photo_order — пустой список.")
        + f"\n\nВерни ТОЛЬКО JSON:\n{NOTE_FORMAT}")}]
    for i, p in enumerate(images):
        content.append({"type": "text", "text": f"Фото {i}:"})
        content.append(_img(p))
    return await _call(content, system=NOTES_SYSTEM, model=config.WRITER_MODEL, max_tokens=4000)


async def rewrite_notes(data: dict, source_text: str, comment: str) -> dict:
    ctx = await _voice_context()
    new = await _call(
        f"# Текущая версия заметки (JSON)\n{json.dumps(_public(data), ensure_ascii=False)}\n\n"
        f"# Материал\n{source_text[:8000]}\n\n" + (ctx + "\n\n" if ctx else "")
        + f"# Комментарий автора\n{comment or 'Перепиши живее и конкретнее.'}\n\n"
        "Перепиши headline_parts, body и tags по комментарию. Только факты из материала, объём 1500–2800 знаков. "
        f"photo_order оставь как есть. Верни полный JSON:\n{NOTE_FORMAT}",
        system=NOTES_SYSTEM, model=config.WRITER_MODEL, max_tokens=4000)
    new["photo_order"] = data.get("photo_order")
    return new
