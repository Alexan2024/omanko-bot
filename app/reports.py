"""Тексты статистики, недельного дайджеста и источников."""
from app import cards, config, db, sources


def _fmt(d: dict) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(d.items(), key=lambda x: -x[1])) or "—"


def _rate(d: dict) -> str:
    total = d["published"] + d["rejected"]
    return f"{round(100 * d['published'] / total)}% из {total}" if total else "нет решений"


def money(x: float) -> str:
    return f"${x:.2f}"


async def stats_text() -> str:
    s = await db.stats()
    p, c = s["posts"], s["candidates"]
    return (
        "<b>📊 Где сейчас посты</b>\n"
        f"🔎 найдено, ждёт фильтра: {c.get('new', 0)}\n"
        f"🔎 прошло фильтр, ждёт оценки: {c.get('triaged', 0)} · на оценке: {c.get('batched', 0)}\n"
        f"📦 в запасе: {p.get('ready', 0)} ({_fmt({cards.FORMAT_LABEL.get(k, k): v for k, v in s['ready_by_format'].items()})})\n"
        f"📥 ждут решения: {p.get('sent', 0) + p.get('announced', 0)}\n"
        f"🗓 стоят в слотах: {p.get('approved', 0)}\n"
        f"✅ опубликовано всего: {p.get('published', 0)} · ❌ отклонено: {p.get('rejected', 0)}\n\n"
        f"<b>Отсеяно ботом:</b> по заголовку, фильтром и оценкой — {c.get('skipped', 0) + c.get('processed', 0)}"
        f" · ошибок: {c.get('error', 0)}\n\n"
        f"<b>Запас по источникам:</b> {_fmt(s['ready_by_source'])}\n\n"
        f"💵 Claude сегодня: {money(await db.cost_today())} (фоном {money(await db.cost_today(True))} "
        f"из {money(config.DAILY_BUDGET_USD)}) · за 7 дней: {money(await db.cost_days(7))}"
    )


async def digest_text(days: int = 7) -> str:
    d = await db.digest(days)
    total = sum(d["by_format"].values())
    src = "\n".join(f"• {k}: {_rate(v)}" for k, v in sorted(
        d["sources"].items(), key=lambda x: -(x[1]["published"] + x[1]["rejected"]))[:12]) or "—"
    u = d["usage"]
    cats = {cards.cat_label(k): v for k, v in d["by_category"].items()}
    return (
        f"<b>📈 AHMAG · итоги за {days} дн.</b>\n\n"
        f"Опубликовано: <b>{total}</b> ({_fmt({cards.FORMAT_LABEL.get(k, k): v for k, v in d['by_format'].items()})})\n"
        f"Рубрики: {_fmt(cats)}\n\n"
        f"<b>Одобрено по источникам</b>\n{src}\n\n"
        f"<b>Причины отказов:</b> {_fmt(d['reasons'])}\n\n"
        f"💵 Claude: <b>{money(u['cost'])}</b> · {u['calls']} вызовов · "
        f"{u['in_tok'] // 1000}k вход / {u['out_tok'] // 1000}k выход"
    )


async def sources_rows() -> list[tuple[str, bool, str]]:
    """[(имя, включён ли, рейтинг)]"""
    stats, off = await db.source_stats(), await sources.disabled()
    return [(name, name not in off, _rate(stats.get(name, {"published": 0, "rejected": 0})))
            for name in await sources.source_names()]
