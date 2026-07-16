"""PNG-карточка статистики: бары по неделям и тепловые карты.

Не зависит от stats_report: текстовый отчёт и картинка считают свои агрегаты
независимо от одних и тех же событий.
"""
import io
from datetime import datetime, timedelta, timezone

from PIL import Image, ImageDraw

from omanko.config import CHANNELS, MSK
from omanko.imaging import load_black, load_semibold


# ============ Визуальная карточка статистики ============
# Одна вертикальная PNG: бары по неделям + две тепловые карты (каналы × час
# дня, каналы × день недели). Источник — те же события stats.json, что и у
# текстового отчёта. Рисуем Pillow'ом (он и так в стеке), шрифты — Nunito из
# репо через load_black/load_semibold. Если событий нет — возвращаем None,
# и тогда карточку просто не отправляем.

_STAT_CH_COLORS = {
    "base":   (236, 238, 242),
    "news":   (90, 156, 255),
    "beauty": (255, 110, 196),
    "music":  (255, 176, 60),
    "agency": (60, 214, 180),
    "gastro": (255, 96, 96),
}
_STAT_BG = (18, 20, 25)
_STAT_CARD = (24, 27, 33)
_STAT_GRID = (44, 48, 56)
_STAT_INK = (235, 237, 240)
_STAT_MUTED = (140, 146, 156)
_STAT_ACCENT = (255, 176, 60)
_STAT_WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _stat_events_msk(events):
    """События с валидным ts -> список (ts_msk, channel, mode, photos)."""
    out = []
    for e in events:
        try:
            ts = datetime.fromisoformat(e["ts"])
        except Exception:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts = ts.astimezone(MSK)
        ch = e.get("channel", "base")
        if ch not in CHANNELS:
            ch = "base"
        out.append((ts, ch, e.get("mode", "type1"), int(e.get("photos", 0))))
    return out


def _stat_rrect(d, box, r, fill):
    """rounded_rectangle с защитой от слишком большого радиуса (Pillow ругается,
    если радиус больше половины меньшей стороны)."""
    x0, y0, x1, y1 = box
    if x1 <= x0 or y1 <= y0:
        return
    r = max(0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    d.rounded_rectangle(box, radius=r, fill=fill)


def _stat_draw_bars_legend(d, x0, y, w):
    """Экспликация для бар-чарта: цветной свотч + название канала.
    Одна горизонтальная строка по центру; если не влезает по ширине —
    автоматически переносится на 2 ряда (по 3 элемента)."""
    font = load_semibold(17)
    swatch = 14
    text_gap = 8   # между свотчем и текстом
    item_gap = 22  # между элементами
    items = []
    for key in CHANNELS:
        title = CHANNELS[key]["title"]
        try:
            tw = int(d.textlength(title, font=font))
        except Exception:
            tw = len(title) * 10
        items.append((key, title, tw))

    def _row_width(row):
        if not row:
            return 0
        return (sum(swatch + text_gap + tw for _, _, tw in row)
                + item_gap * (len(row) - 1))

    if _row_width(items) <= w:
        rows = [items]
    else:
        half = (len(items) + 1) // 2
        rows = [items[:half], items[half:]]

    row_h = 22
    for ri, row in enumerate(rows):
        rw = _row_width(row)
        cx = x0 + max(0, (w - rw) // 2)
        ry = y + ri * row_h
        for key, title, tw in row:
            color = _STAT_CH_COLORS.get(key, _STAT_INK)
            _stat_rrect(d, (cx, ry + 2, cx + swatch, ry + swatch + 2), 3, color)
            d.text((cx + swatch + text_gap, ry - 2), title,
                   font=font, fill=_STAT_INK)
            cx += swatch + text_gap + tw + item_gap


def _stat_draw_bars(d, x0, y0, w, h, msk_events, until):
    """Stacked-бары: посты по неделям (8 недель) с разбивкой по каналам."""
    d.text((x0, y0), "Посты по неделям", font=load_black(34), fill=_STAT_INK)
    d.text((x0, y0 + 40), "последние 8 недель · по каналам",
           font=load_semibold(19), fill=_STAT_MUTED)
    # Экспликация: цвет → канал. Без неё юзеру приходилось гадать,
    # какой бар чему соответствует.
    _stat_draw_bars_legend(d, x0, y0 + 74, w)
    weeks = 8
    lbl = load_semibold(17)
    buckets = [{k: 0 for k in CHANNELS} for _ in range(weeks)]
    start = until - timedelta(days=7 * weeks)
    for ts, ch, _mode, _ph in msk_events:
        if ts <= start or ts > until:
            continue
        idx = int((ts - start).days // 7)
        idx = max(0, min(idx, weeks - 1))
        buckets[idx][ch] += 1
    totals = [sum(b.values()) for b in buckets]
    maxtot = max(totals) if any(totals) else 1
    plot_top = y0 + 114  # сдвинуто вниз, чтобы освободить место под экспликацию
    plot_bot = y0 + h - 40
    plot_h = plot_bot - plot_top
    for g in range(5):
        gy = plot_bot - plot_h * g / 4
        d.line((x0, gy, x0 + w, gy), fill=_STAT_GRID, width=1)
        d.text((x0 - 8, gy - 9), str(round(maxtot * g / 4)),
               font=lbl, fill=_STAT_MUTED, anchor="ra")
    bw = w / weeks
    barw = bw * 0.56
    for wi in range(weeks):
        cx = x0 + bw * (wi + 0.5)
        yb = plot_bot
        for key in CHANNELS:
            v = buckets[wi][key]
            if v <= 0:
                continue
            bh = plot_h * v / maxtot
            _stat_rrect(d, (cx - barw / 2, yb - bh, cx + barw / 2, yb), 4,
                        _STAT_CH_COLORS.get(key, _STAT_INK))
            yb -= bh + 2
        wd = start + timedelta(days=7 * (wi + 1) - 1)
        d.text((cx, plot_bot + 10), wd.strftime("%d.%m"),
               font=lbl, fill=_STAT_MUTED, anchor="ma")


def _stat_draw_heat(d, x0, y0, w, h, msk_events, mode, title_txt, sub_txt):
    """Тепловая карта каналы × время. mode='hour' (24 колонки) | 'weekday' (7)."""
    d.text((x0, y0), title_txt, font=load_black(34), fill=_STAT_INK)
    d.text((x0, y0 + 40), sub_txt, font=load_semibold(19), fill=_STAT_MUTED)
    ncols = 24 if mode == "hour" else 7
    rows = list(CHANNELS.keys())
    grid = [[0] * ncols for _ in rows]
    for ts, ch, _mode, _ph in msk_events:
        ci = rows.index(ch) if ch in rows else 0
        col = ts.hour if mode == "hour" else ts.weekday()
        grid[ci][col] += 1
    maxv = max((max(r) for r in grid), default=0) or 1
    lbl = load_semibold(17)
    sm = load_semibold(14)
    label_w = 150
    gx0 = x0 + label_w
    gy0 = y0 + 86
    cell = (w - label_w) / ncols
    ch_h = 40
    for ci, key in enumerate(rows):
        ry = gy0 + ci * ch_h
        d.text((gx0 - 14, ry + ch_h / 2 - 11), CHANNELS[key]["title"],
               font=lbl, fill=_STAT_INK, anchor="ra")
        for c in range(ncols):
            t = (grid[ci][c] / maxv) ** 0.7 if maxv else 0
            cc = tuple(int(_STAT_CARD[i] + (_STAT_ACCENT[i] - _STAT_CARD[i]) * t)
                       for i in range(3))
            cx = gx0 + c * cell
            _stat_rrect(d, (cx + 1, ry + 1, cx + cell - 1, ry + ch_h - 3), 3, cc)
    axis_y = gy0 + len(rows) * ch_h + 6
    if mode == "hour":
        for c in range(ncols):
            if c % 3 == 0:
                d.text((gx0 + c * cell + cell / 2, axis_y), f"{c:02d}",
                       font=sm, fill=_STAT_MUTED, anchor="ma")
    else:
        for c in range(ncols):
            d.text((gx0 + c * cell + cell / 2, axis_y), _STAT_WEEKDAYS[c],
                   font=sm, fill=_STAT_MUTED, anchor="ma")


def render_stats_card(events, until=None):
    """Единая PNG-карточка (бары + 2 тепловые карты). Возвращает PNG-байты
    или None, если валидных событий нет."""
    msk_events = _stat_events_msk(events)
    if not msk_events:
        return None
    until = until or datetime.now(MSK)
    W = 1000
    pad = 56
    inner = W - pad * 2
    n_ch = len(CHANNELS)
    h_bars = 400  # 360 → 400: +40px на строку легенды над графиком
    h_heat = n_ch * 40 + 130
    gap = 36
    H = pad + h_bars + gap + h_heat + gap + h_heat + pad
    img = Image.new("RGB", (W, H), _STAT_BG)
    d = ImageDraw.Draw(img)
    _stat_rrect(d, (pad - 24, pad - 24, W - (pad - 24), H - (pad - 24)), 28, _STAT_CARD)
    y = pad
    _stat_draw_bars(d, pad, y, inner, h_bars, msk_events, until)
    y += h_bars + gap
    _stat_draw_heat(d, pad, y, inner, h_heat, msk_events, "hour",
                    "Когда постим", "каналы × час дня (МСК) · по всей истории")
    y += h_heat + gap
    _stat_draw_heat(d, pad, y, inner, h_heat, msk_events, "weekday",
                    "Дни недели", "каналы × день недели (МСК) · по всей истории")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue()
