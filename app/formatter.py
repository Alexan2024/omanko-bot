"""Сборка подписи в формате AHMAG (Telegram HTML)."""
import html
import re

TAG_FIX = {
    "ahmagreece": "ahmaggreece",
    "ahmagchine": "ahmagchina",
    "ahmagkorea": "ahmagsouthkorea",
    "ahmagengland": "ahmaguk",
    "ahmagunitedkingdom": "ahmaguk",
    "ahmagunitedstates": "ahmagusa",
}


def _safe_body(text: str) -> str:
    """Экранирует всё, кроме <b>/<i>; чинит тире."""
    s = html.escape(text or "", quote=False)
    s = re.sub(r"&lt;(/?)(b|i)&gt;", r"<\1\2>", s)
    s = re.sub(r"(?<=\s)-(?=\s)", "—", s)  # дефис между пробелами → тире
    s = re.sub(r"\n{3,}", "\n\n", s.strip())
    return s


def _credit(label: str, name: str | None, url: str | None) -> str | None:
    if not name or str(name).strip().lower() == "null":
        return None
    name_e = html.escape(str(name), quote=False)
    if url and str(url).startswith("http"):
        return f'<i>{label}: </i><a href="{html.escape(url)}"><i>{name_e}</i></a>'
    return f"<i>{label}: {name_e}</i>"


def normalize_tags(tags: list[str], fmt: str = "std") -> list[str]:
    out = []
    for t in tags or []:
        t = re.sub(r"[^a-z]", "", str(t).lower().lstrip("#"))
        if not t:
            continue
        if not t.startswith("ahmag"):
            t = "ahmag" + t
        t = TAG_FIX.get(t, t)
        if t not in out:
            out.append(t)
    if fmt == "notes":
        out = ["ahmagnotes"] + [t for t in out if t != "ahmagnotes"]
        return out[:4]
    return out[:3]


def headline_parts(data: dict) -> list[str]:
    return [str(p).strip() for p in (data.get("headline_parts") or []) if p and str(p).strip().lower() != "null"]


def has_body(data: dict) -> bool:
    return bool(str(data.get("body") or "").strip())


def build_caption(data: dict, fmt: str = "std") -> str:
    """Собирает подпись и заодно проставляет data['headline'] для истории.
    std / notes — заголовок, текст, кредиты, теги; mini — заголовок, одна фраза (если есть), кредиты, теги.
    У большого поста, чей текст ещё не написан, подпись временно без основного текста."""
    parts = headline_parts(data)
    data["headline"] = " // ".join(parts)
    headline = " // ".join(html.escape(p, quote=False) for p in parts)
    blocks = [f"<b>{headline}</b>"] if headline else []

    if fmt == "mini":
        line = str(data.get("mini_line") or "").strip()
        if line and line.lower() != "null":
            blocks.append(_safe_body(line))
    else:
        blocks.append(_safe_body(data.get("body", "")))

    c = data.get("credits") or {}
    credits = [x for x in (
        _credit("pr", c.get("pr"), c.get("pr_url")),
        _credit("ph", c.get("ph"), c.get("ph_url")),
        _credit("via", c.get("via"), None),
    ) if x]
    if credits:
        blocks.append("\n".join(credits))

    tags = normalize_tags(data.get("tags", []), fmt)
    if tags:
        blocks.append(" ".join("#" + t for t in tags))
    return "\n\n".join(b for b in blocks if b)


def visible_len(caption: str) -> int:
    """Длина подписи без HTML-разметки — так её считает Telegram."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", caption or "")))


def plain_text(caption: str) -> str:
    """Подпись без разметки — для Instagram и для сравнения версий."""
    return html.unescape(re.sub(r"<[^>]+>", "", caption or "")).strip()


def split_blocks(text: str, limit: int) -> list[str]:
    """Режет длинный HTML-текст по абзацам, чтобы каждый кусок влезал в сообщение."""
    chunks, cur = [], ""
    for block in text.split("\n\n"):
        candidate = f"{cur}\n\n{block}" if cur else block
        if visible_len(candidate) <= limit:
            cur = candidate
        else:
            if cur:
                chunks.append(cur)
            cur = block
    if cur:
        chunks.append(cur)
    return chunks


def clip_blocks(text: str, budget: int) -> tuple[str, bool]:
    """Первые абзацы, которые влезают в budget видимых знаков. → (текст, обрезано ли)"""
    if visible_len(text) <= budget:
        return text, False
    chunks = split_blocks(text, max(budget, 120))
    first = chunks[0] if chunks else ""
    if visible_len(first) > budget:  # один огромный абзац — режем по словам, без разметки
        plain = plain_text(first)[: max(budget - 1, 60)].rsplit(" ", 1)[0]
        return html.escape(plain, quote=False) + "…", True
    return first, True
