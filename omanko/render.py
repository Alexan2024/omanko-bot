"""Рендер: чистые функции из картинки в картинку.

Каждый режим — чистая функция: на входе PIL.Image и параметры, на выходе
новая PIL.Image. Ни Telegram, ни состояния диалога, ни переменных окружения.
Именно поэтому эти функции можно накрыть эталонными тестами.
"""
import io

import numpy as np
from PIL import Image, ImageDraw

from omanko.config import (
    ALPHA, BRIGHTNESS_OFFSET, BUBBLE_TEXT_SIZE, CHANNELS, COLLAB_ADAPTIVE,
    COLLAB_BOTTOM, COLLAB_GAP, COLLAB_PARTNER_H,
    COLLAB_WORDMARK_CENTER_DROP, COLLAB_WORDMARK_H, COLLAB_WORDMARK_W,
    COLLAB_X_GLYPH, COLLAB_X_SIZE, COVER_DEFAULT, COVER_FORMATS,
    COVER_LINE_SPACING, COVER_TITLE_BOTTOM_IG, COVER_TITLE_BOTTOM_TG,
    COVER_TITLE_LS_FEED, COVER_TITLE_LS_STORY, COVER_TITLE_SIZE_FEED,
    COVER_TITLE_SIZE_STORY, FEED_BUBBLE_ALPHA, FEED_BUBBLE_FILL,
    FEED_BUBBLE_PAD_X, FORMATS, GRAD_ALPHA_CEIL, GRAD_ALPHA_DARK,
    GRAD_ALPHA_LIGHT, GRAD_RISE_STORY, HASHTAG_BOTTOM, HASHTAG_RIGHT,
    HASHTAG_SIZE, IG_BUBBLE_BOTTOM, IG_BUBBLE_H, IG_BUBBLE_RADIUS,
    IG_BUBBLE_W, LOGO_BOTTOM, LOGO_H, LOGO_LEFT, LOGO_W, STORE_LINE_HEIGHT,
    STORE_LOGO_BOTTOM, STORE_LOGO_H, STORE_LOGO_LEFT, STORE_LOGO_W,
    STORE_SIZE, STORE_TEXT_BOTTOM, STORE_TEXT_GAP, STORE_TEXT_SIZE,
    STORY_BUBBLE_ALPHA, STORY_SIZE, TG_BUBBLE_BOTTOM, TG_BUBBLE_H,
    TG_BUBBLE_RADIUS, TG_BUBBLE_W, WORDMARK_BOTTOM_FEED, WORDMARK_TOP_STORY,
    WORDMARK_W_FEED, WORDMARK_W_STORY,
)
from omanko.imaging import (
    _tint_white_logo, adjust_brightness, brightness_of, draw_logo,
    fit_image_to_canvas, get_average_color, get_wordmark, load_black,
    load_bold, load_semibold, paste_story_channel_logo,
    paste_type1_channel_logo,
)


def process_image(img: Image.Image, format_key: str, hashtag: str, channel: str = "base") -> Image.Image:
    """ТИП 1 — брендинг. Логотип: у базового/agency — векторный Ö (как раньше),
    у остальных каналов — свой PNG-логотип своего размера, адаптивно перекрашенный."""
    fmt = FORMATS[format_key]
    if fmt is None:
        if img.width < 1920:
            scale = 1920 / img.width
            canvas_w = 1920
            canvas_h = int(img.height * scale)
        else:
            canvas_w, canvas_h = img.size
    else:
        canvas_w, canvas_h = fmt

    scale = canvas_w / 1920

    # Геометрия логотипа: своя у канала (type1_box), иначе дефолтные LOGO_*
    ch = CHANNELS.get(channel, CHANNELS["base"])
    if ch["type1_box"]:
        bw, bh, bleft, bbottom = ch["type1_box"]
        logo_w = int(bw * scale)
        logo_h = int(bh * scale)
        logo_x = int(bleft * scale)
        logo_y = canvas_h - int(bbottom * scale) - logo_h
    else:
        logo_w = int(LOGO_W * scale)
        logo_h = int(LOGO_H * scale)
        logo_x = int(LOGO_LEFT * scale)
        logo_y = canvas_h - int(LOGO_BOTTOM * scale) - logo_h

    hashtag_right = int(HASHTAG_RIGHT * scale)
    hashtag_bottom = int(HASHTAG_BOTTOM * scale)
    hashtag_size = int(HASHTAG_SIZE * scale)

    canvas = fit_image_to_canvas(img, canvas_w, canvas_h)

    # ЛОГОТИП — адаптивный цвет по фону под ним
    r, g, b = get_average_color(canvas, logo_x, logo_y, logo_w, logo_h)
    percent = BRIGHTNESS_OFFSET if brightness_of(r, g, b) < 128 else -BRIGHTNESS_OFFSET
    logo_color = adjust_brightness(r, g, b, percent)
    canvas_rgba = canvas.convert("RGBA")
    placed = False
    if ch["type1_logo"]:
        placed = paste_type1_channel_logo(canvas_rgba, ch["type1_logo"],
                                          logo_x, logo_y, logo_w, logo_h, logo_color)
    if not placed:
        draw_logo(canvas_rgba, logo_x, logo_y, logo_w, logo_h, logo_color)
    canvas = canvas_rgba.convert("RGB")

    # ХЕШТЕГ
    if hashtag and hashtag != "— Без хештега —":
        sample_x = max(0, canvas_w - hashtag_right - int(200 * scale))
        sample_y = max(0, canvas_h - hashtag_bottom - hashtag_size)
        hr, hg, hb = get_average_color(canvas, sample_x, sample_y, int(200 * scale), hashtag_size + 20)
        h_percent = BRIGHTNESS_OFFSET if brightness_of(hr, hg, hb) < 128 else -BRIGHTNESS_OFFSET
        hcr, hcg, hcb = adjust_brightness(hr, hg, hb, h_percent)

        overlay = canvas.convert("RGBA")
        draw = ImageDraw.Draw(overlay)
        font = load_semibold(hashtag_size)
        fill = (hcr, hcg, hcb, int(255 * ALPHA))
        spacing = int(hashtag_size * (-0.007))
        total_w = 0
        char_widths = []
        for ch in hashtag:
            bbox = draw.textbbox((0, 0), ch, font=font)
            cw = bbox[2] - bbox[0]
            char_widths.append(cw)
            total_w += cw + spacing
        total_w -= spacing
        tx = canvas_w - hashtag_right - total_w
        ty = canvas_h - hashtag_bottom - hashtag_size
        cx = tx
        for ch, cw in zip(hashtag, char_widths):
            draw.text((cx, ty), ch, font=font, fill=fill)
            cx += cw + spacing
        canvas = overlay.convert("RGB")

    return canvas


# ============ Коллаборация — рендер ============
def process_collab(img: Image.Image, format_key: str, partner_bytes: bytes) -> Image.Image:
    """КОЛЛАБОРАЦИЯ — нижняя строка «ÖMANKÖ × лого партнёра».

    Слева вордмарк ÖMANKÖ (327×71), пропуск 43px, «×» (шрифт/размер как у
    хештегов), пропуск 43px, лого партнёра (высота 58px). Группа центрируется
    по горизонтали; элементы выровнены по общей горизонтальной оси. Низ строки —
    на том же отступе от края, что и логотип в брендинге (COLLAB_BOTTOM).
    Все размеры заданы в опорных единицах 1920px и масштабируются под канвас.
    """
    fmt = FORMATS[format_key]
    if fmt is None:
        if img.width < 1920:
            up = 1920 / img.width
            canvas_w = 1920
            canvas_h = int(img.height * up)
        else:
            canvas_w, canvas_h = img.size
    else:
        canvas_w, canvas_h = fmt

    scale = canvas_w / 1920
    canvas = fit_image_to_canvas(img, canvas_w, canvas_h)

    # --- Размеры элементов в пикселях канваса ---
    wm_w = max(1, int(COLLAB_WORDMARK_W * scale))
    wm_h = max(1, int(COLLAB_WORDMARK_H * scale))
    gap = int(COLLAB_GAP * scale)
    x_size = max(1, int(COLLAB_X_SIZE * scale))
    partner_h = max(1, int(COLLAB_PARTNER_H * scale))
    bottom = int(COLLAB_BOTTOM * scale)
    center_drop = COLLAB_WORDMARK_CENTER_DROP * scale

    # --- Лого партнёра: масштаб по высоте 58px (ширина пропорционально) ---
    partner = Image.open(io.BytesIO(partner_bytes)).convert("RGBA")
    partner_w = max(1, round(partner.width * (partner_h / partner.height)))
    partner_rs = partner.resize((partner_w, partner_h), Image.LANCZOS)

    # --- Вордмарк ÖMANKÖ ---
    wm_rs = get_wordmark().resize((wm_w, wm_h), Image.LANCZOS)

    # --- Габариты глифа «×» ---
    font = load_semibold(x_size)
    _probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    xbbox = _probe.textbbox((0, 0), COLLAB_X_GLYPH, font=font)
    x_w = xbbox[2] - xbbox[0]
    x_h = xbbox[3] - xbbox[1]

    # --- Горизонталь: вся группа по центру канваса ---
    group_w = wm_w + gap + x_w + gap + partner_w
    group_left = (canvas_w - group_w) // 2

    # --- Вертикаль: низ вордмарка на `bottom` от низа канваса ---
    wm_top = canvas_h - bottom - wm_h
    # Ось выравнивания = геометрический центр вордмарка + сдвиг вниз на 9px
    center_y = wm_top + wm_h / 2 + center_drop

    # --- Адаптивный цвет по фону под всей строкой ---
    if COLLAB_ADAPTIVE:
        strip_top = int(min(wm_top, center_y - x_h / 2, center_y - partner_h / 2))
        strip_bottom = int(max(wm_top + wm_h, center_y + x_h / 2, center_y + partner_h / 2))
        sr, sg, sb = get_average_color(canvas, group_left, strip_top,
                                       group_w, max(1, strip_bottom - strip_top))
        percent = BRIGHTNESS_OFFSET if brightness_of(sr, sg, sb) < 128 else -BRIGHTNESS_OFFSET
        color = adjust_brightness(sr, sg, sb, percent)
    else:
        color = (255, 255, 255)

    overlay = canvas.convert("RGBA")

    # 1) Вордмарк ÖMANKÖ
    overlay.alpha_composite(_tint_white_logo(wm_rs, color, ALPHA), (group_left, wm_top))

    # 2) «×» — центр глифа на оси center_y
    x_left = group_left + wm_w + gap
    draw = ImageDraw.Draw(overlay)
    fill = (color[0], color[1], color[2], int(255 * ALPHA))
    x_tx = int(x_left - xbbox[0])
    x_ty = int(center_y - x_h / 2 - xbbox[1])
    draw.text((x_tx, x_ty), COLLAB_X_GLYPH, font=font, fill=fill)

    # 3) Лого партнёра — высота 58px, центр по center_y
    partner_left = x_left + x_w + gap
    partner_top = int(center_y - partner_h / 2)
    overlay.alpha_composite(_tint_white_logo(partner_rs, color, ALPHA),
                            (partner_left, partner_top))

    return overlay.convert("RGB")


# ============ Обложка: примитивы ============
def paste_wordmark(canvas_rgba: Image.Image, target_w: int, cx: int, y_top: int):
    wm = get_wordmark()
    ratio = wm.height / wm.width
    target_h = max(1, round(target_w * ratio))
    resized = wm.resize((target_w, target_h), Image.LANCZOS)
    x = int(cx - target_w / 2)
    canvas_rgba.alpha_composite(resized, (x, y_top))
    return target_h


def apply_bottom_gradient(canvas: Image.Image, brightness: float, rise: int,
                          dark_level: float = 1.0) -> Image.Image:
    """Чёрный градиент снизу вверх.

    Плотность складывается из двух частей:
      • АДАПТИВ — базовая alpha по яркости фона в зоне заголовка (тёмный фон →
        слабый градиент, светлый → плотный). Это поведение при dark_level=1.0.
      • РУЧНОЙ сдвиг dark_level — прибавляется к базовой alpha (Светлее/Темнее).
        1.0 = как было; <1 светлее; >1 темнее. Сдвиг действует одинаково сильно
        при любой яркости фона. Итог клампим [0 … GRAD_ALPHA_CEIL].
    """
    cw, ch = canvas.size
    t = max(0.0, min(1.0, brightness / 255.0))
    base_alpha = GRAD_ALPHA_DARK + (GRAD_ALPHA_LIGHT - GRAD_ALPHA_DARK) * t
    # dark_level — это сдвиг относительно базы: 1.0 = без сдвига, <1 светлее, >1 темнее.
    alpha = max(0.0, min(GRAD_ALPHA_CEIL, base_alpha + (dark_level - 1.0)))
    max_alpha = int(255 * alpha)
    rise = min(rise, ch)
    # вертикальный градиент: 0 сверху rise-зоны -> max_alpha у низа
    ramp = np.linspace(0, max_alpha, rise).astype(np.uint8).reshape(-1, 1)
    ramp = np.repeat(ramp, cw, axis=1)
    mask_full = np.zeros((ch, cw), dtype=np.uint8)
    mask_full[ch - rise:ch, :] = ramp
    mask = Image.fromarray(mask_full, mode="L")
    black = Image.new("RGBA", (cw, ch), (0, 0, 0, 255))
    base = canvas.convert("RGBA")
    base = Image.composite(black, base, mask)
    return base


def draw_centered_title(canvas_rgba: Image.Image, text: str, size: int,
                        ls_ratio: float, bottom_offset: int):
    cw, ch = canvas_rgba.size
    font = load_black(size)
    draw = ImageDraw.Draw(canvas_rgba)
    ls_px = round(size * ls_ratio)
    lines = [ln for ln in text.split("\n")]
    if not lines:
        return
    ascent, descent = font.getmetrics()
    line_adv = int(size * COVER_LINE_SPACING)
    line_visual = ascent + descent
    n = len(lines)
    last_top = (ch - bottom_offset) - line_visual
    first_top = last_top - (n - 1) * line_adv
    cx = cw / 2
    fill = (255, 255, 255, 255)
    for i, line in enumerate(lines):
        # ширина строки с трекингом
        widths = [draw.textlength(c, font=font) for c in line]
        total = sum(widths) + ls_px * (len(line) - 1 if len(line) > 1 else 0)
        x = cx - total / 2
        y = first_top + i * line_adv
        for c, w in zip(line, widths):
            draw.text((x, y), c, font=font, fill=fill)
            x += w + ls_px


def draw_bubble(canvas_rgba: Image.Image, center_x: int, center_y: int,
                bubble_w, bubble_h: int, radius: int, bg_img: Image.Image,
                label=None, color_mode="dark", alpha=0.85):
    """Бабл. label=None -> пустой бабл (сторис).
    color_mode: 'dark' (фикс. тёмный, лента) | 'adaptive_invert' (инверт к фону, сторис).
    bubble_w=None -> авто-ширина под текст (лента)."""
    font = load_semibold(BUBBLE_TEXT_SIZE) if label else None
    d = ImageDraw.Draw(canvas_rgba)
    tw = d.textlength(label, font=font) if label else 0
    if bubble_w is None:
        bubble_w = int(tw + 2 * FEED_BUBBLE_PAD_X)
    left = int(center_x - bubble_w / 2)
    top = int(center_y - bubble_h / 2)
    right = left + bubble_w
    bottom = top + bubble_h

    # цвет фона под баблом
    r, g, b = get_average_color(bg_img, left, top, bubble_w, bubble_h)
    dark_bg = brightness_of(r, g, b) < 128
    if color_mode == "adaptive_invert":
        fill_rgb = (255, 255, 255) if dark_bg else (0, 0, 0)
    else:
        fill_rgb = FEED_BUBBLE_FILL
    fill = (fill_rgb[0], fill_rgb[1], fill_rgb[2], int(255 * alpha))

    layer = Image.new("RGBA", canvas_rgba.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle([left, top, right, bottom], radius=radius, fill=fill)
    canvas_rgba.alpha_composite(layer)

    if label:
        d = ImageDraw.Draw(canvas_rgba)
        bbox = d.textbbox((0, 0), label, font=font)
        txt_h = bbox[3] - bbox[1]
        tx = center_x - tw / 2
        ty = center_y - txt_h / 2 - bbox[1]
        d.text((tx, ty), label, font=font, fill=(255, 255, 255, 255))


# ============ Обложка: рендер вариантов ============
def render_cover_feed(img: Image.Image, format_key: str, title: str, hashtag: str,
                      dark_level: float = 1.0) -> Image.Image:
    spec = COVER_FORMATS.get(format_key)
    if spec is None:
        # Адаптивный: канвас по картинке (мин. ширина 1920), параметры — дефолтные
        if img.width < 1920:
            sc = 1920 / img.width
            canvas_w, canvas_h = 1920, int(img.height * sc)
        else:
            canvas_w, canvas_h = img.size
        bubble_h = COVER_DEFAULT["bubble_h"]
        bubble_top = COVER_DEFAULT["bubble_top"]
        title_bottom = COVER_DEFAULT["title_bottom"]
    else:
        canvas_w, canvas_h = spec["size"]
        bubble_h = spec["bubble_h"]
        bubble_top = spec["bubble_top"]
        title_bottom = spec["title_bottom"]

    # Размеры элементов абсолютные (не масштабируются от ширины)
    title_size = COVER_TITLE_SIZE_FEED
    wm_w = WORDMARK_W_FEED
    wm_bottom = WORDMARK_BOTTOM_FEED
    radius = bubble_h // 2  # полная «таблетка»

    base = fit_image_to_canvas(img, canvas_w, canvas_h)

    # градиент — по яркости в зоне заголовка
    region_y = max(0, canvas_h - title_bottom - title_size * 2)
    br_r, br_g, br_b = get_average_color(base, 0, region_y, canvas_w, title_size * 2)
    grad_rise = min(canvas_h, title_bottom + title_size * 4)
    canvas = apply_bottom_gradient(base, brightness_of(br_r, br_g, br_b), grad_rise,
                                   dark_level=dark_level)

    # заголовок
    draw_centered_title(canvas, title, title_size, COVER_TITLE_LS_FEED, title_bottom)

    # бабл сверху по центру — тёмный плотный, с хештегом
    if hashtag and hashtag != "— Без хештега —":
        cy = bubble_top + bubble_h // 2
        bg_for_bubble = canvas.convert("RGB")
        label = "# " + hashtag.lstrip("#")
        draw_bubble(canvas, canvas_w // 2, cy, None, bubble_h, radius, bg_for_bubble,
                    label=label, color_mode="dark", alpha=FEED_BUBBLE_ALPHA)

    # вордмарк снизу по центру — в ленте ВСЕГДА базовый ÖMANKÖ (по каналу не меняется)
    ratio = get_wordmark().height / get_wordmark().width
    wm_h = round(wm_w * ratio)
    wm_y = canvas_h - wm_bottom - wm_h
    paste_wordmark(canvas, wm_w, canvas_w // 2, wm_y)

    return canvas.convert("RGB")


def render_cover_story(img: Image.Image, variant: str, title: str, hashtag: str,
                       channel: str = "base", dark_level: float = 1.0) -> Image.Image:
    cw, ch = STORY_SIZE
    if variant == "ig":
        title_bottom = COVER_TITLE_BOTTOM_IG
        b_w, b_h, b_r, b_bottom = IG_BUBBLE_W, IG_BUBBLE_H, IG_BUBBLE_RADIUS, IG_BUBBLE_BOTTOM
    else:  # tg
        title_bottom = COVER_TITLE_BOTTOM_TG
        b_w, b_h, b_r, b_bottom = TG_BUBBLE_W, TG_BUBBLE_H, TG_BUBBLE_RADIUS, TG_BUBBLE_BOTTOM

    base = fit_image_to_canvas(img, cw, ch)

    # градиент по яркости в зоне заголовка
    region_y = ch - title_bottom - COVER_TITLE_SIZE_STORY * 2
    br_r, br_g, br_b = get_average_color(base, 0, max(0, region_y), cw, COVER_TITLE_SIZE_STORY * 2)
    canvas = apply_bottom_gradient(base, brightness_of(br_r, br_g, br_b), GRAD_RISE_STORY,
                                   dark_level=dark_level)

    # лого сверху по центру:
    #  - канал со своим story-лого → фиксированный размер (story_box), белый как есть
    #  - база/agency → широкий вордмарк ÖMANKÖ (как раньше)
    chan = CHANNELS.get(channel, CHANNELS["base"])
    placed = False
    if chan["story_logo"] and chan["story_box"]:
        lw, lh = chan["story_box"]
        placed = paste_story_channel_logo(canvas, chan["story_logo"], cw // 2, WORDMARK_TOP_STORY, lw, lh)
    if not placed:
        paste_wordmark(canvas, WORDMARK_W_STORY, cw // 2, WORDMARK_TOP_STORY)

    # заголовок
    draw_centered_title(canvas, title, COVER_TITLE_SIZE_STORY, COVER_TITLE_LS_STORY, title_bottom)

    # бабл под заголовком — ПУСТОЙ (без хештега), цвет инвертный к фону
    cy = ch - b_bottom - b_h // 2
    bg_for_bubble = canvas.convert("RGB")
    draw_bubble(canvas, cw // 2, cy, b_w, b_h, b_r, bg_for_bubble,
                label=None, color_mode="adaptive_invert", alpha=STORY_BUBBLE_ALPHA)

    return canvas.convert("RGB")


# ============ ÖMANKÖ STORE — рендер ============
def process_store(img: Image.Image, text: str, color=None) -> Image.Image:
    """ÖMANKÖ STORE — витрина магазина.
    Холст всегда 2000×2500, фон-фото (cover-fit). Векторный Ö внизу слева;
    справа — подпись в 2 строки тем же цветом, шрифт Nunito Bold, межстрочный 90%.
    color=None → адаптивный цвет под фоном (как в брендинге);
    color=(r,g,b) → фиксированный цвет графики (Ö + текст)."""
    cw, ch = STORE_SIZE
    canvas = fit_image_to_canvas(img, cw, ch)

    logo_x = STORE_LOGO_LEFT
    logo_y = ch - STORE_LOGO_BOTTOM - STORE_LOGO_H
    logo_w, logo_h = STORE_LOGO_W, STORE_LOGO_H

    if color is None:
        # Адаптивный цвет под лого (как в брендинге) — общий для Ö и текста
        r, g, b = get_average_color(canvas, logo_x, logo_y, logo_w, logo_h)
        percent = BRIGHTNESS_OFFSET if brightness_of(r, g, b) < 128 else -BRIGHTNESS_OFFSET
        color = adjust_brightness(r, g, b, percent)
    color = (int(color[0]), int(color[1]), int(color[2]))

    canvas_rgba = canvas.convert("RGBA")
    draw_logo(canvas_rgba, logo_x, logo_y, logo_w, logo_h, color)

    # Подпись: до 2 строк справа от лого, тем же цветом
    lines = text.split("\n")[:2]
    font = load_bold(STORE_TEXT_SIZE)
    draw = ImageDraw.Draw(canvas_rgba)
    text_x = logo_x + logo_w + STORE_TEXT_GAP
    line_h = STORE_TEXT_SIZE * STORE_LINE_HEIGHT
    fill = (color[0], color[1], color[2], int(255 * ALPHA))
    _, descent = font.getmetrics()
    base_last = ch - STORE_TEXT_BOTTOM - descent  # базовая линия нижней строки
    n = len(lines)
    for i, line in enumerate(lines):
        baseline = base_last - (n - 1 - i) * line_h
        draw.text((text_x, baseline), line, font=font, fill=fill, anchor="ls")

    return canvas_rgba.convert("RGB")
