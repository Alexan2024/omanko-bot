"""Текстовый еженедельный отчёт по производству постов."""
from datetime import datetime, timedelta, timezone

from omanko.config import CHANNELS, MSK, _RU_MONTHS


def _plural_post(n: int) -> str:
    n100, n10 = abs(n) % 100, abs(n) % 10
    if 11 <= n100 <= 14:
        return "постов"
    if n10 == 1:
        return "пост"
    if 2 <= n10 <= 4:
        return "поста"
    return "постов"


def _ru_date(d: datetime) -> str:
    return f"{d.day} {_RU_MONTHS[d.month]}"


def build_weekly_report(events, until=None) -> str:
    """Сводка за 7 дней до момента until (по МСК): всего и по каналам +
    разбивка фото по типам."""
    until = until or datetime.now(MSK)
    since = until - timedelta(days=7)

    chans = {k: {"posts": 0, "photos": 0} for k in CHANNELS}
    total_posts = total_photos = type1_photos = cover_photos = 0
    store_photos = 0

    for e in events:
        try:
            ts = datetime.fromisoformat(e["ts"])
        except Exception:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts = ts.astimezone(MSK)
        if not (since < ts <= until):
            continue
        ch = e.get("channel", "base")
        if ch not in chans:
            ch = "base"
        ph = int(e.get("photos", 0))
        chans[ch]["posts"] += 1
        chans[ch]["photos"] += ph
        total_posts += 1
        total_photos += ph
        if e.get("mode") == "cover":
            cover_photos += ph
        elif e.get("mode") == "store":
            store_photos += ph
        else:
            type1_photos += ph

    period = f"{_ru_date(since)} — {_ru_date(until)}"
    if total_posts == 0:
        return (f"📊 *Итоги недели* ({period})\n\n"
                "Тишина в эфире — за неделю ни одного поста. "
                "Контент сам себя не сделает 😉")

    lines = [
        f"📊 *Итоги недели* ({period})",
        "",
        f"🔥 Всего: *{total_posts}* {_plural_post(total_posts)} · "
        f"*{total_photos}* фото",
        "",
        "*По каналам:*",
    ]
    for k in CHANNELS:
        c = chans[k]
        if c["posts"] == 0:
            continue
        lines.append(f"• {CHANNELS[k]['title']}: "
                     f"{c['posts']} {_plural_post(c['posts'])}, {c['photos']} фото")
    lines += [
        "",
        "*По типам (фото):*",
        f"🏷 Тип 1 — {type1_photos}",
        f"🖼 Обложка — {cover_photos}",
        f"🛍 STORE — {store_photos}",
    ]
    return "\n".join(lines)
