import os
import io
import logging
from PIL import Image, ImageDraw, ImageFont
import cairosvg
import numpy as np
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, ConversationHandler
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("BOT_TOKEN")

# Состояния диалога
WAITING_PHOTOS = 1
CHOOSING_FORMAT = 2
CHOOSING_HASHTAG = 3

FORMATS = {
    "4:5":        (1920, 2400),
    "2:3":        (1920, 2880),
    "1:1":        (1920, 1920),
    "3:2":        (1920, 1280),
    "Адаптивный": None,
}

HASHTAGS = [
    "— Без хештега —",
    "#architecture", "#cars", "#cinema", "#archives",
    "#art", "#community", "#item", "#music",
    "#paper", "#space", "#style",
]

# Размеры графики (пиксели, для ширины 1920)
LOGO_W = 56
LOGO_H = 71
LOGO_LEFT = 92
LOGO_BOTTOM = 70
HASHTAG_RIGHT = 80
HASHTAG_BOTTOM = 79
HASHTAG_SIZE = 51
BRIGHTNESS_OFFSET = 45
ALPHA = 0.55


def get_svg_logo(color_hex: str) -> bytes:
    """Возвращает SVG логотип с нужным цветом как PNG bytes."""
    svg = f'''<svg width="365" height="459" viewBox="0 0 365 459" fill="none" xmlns="http://www.w3.org/2000/svg">
<path d="M182.521 94.4141C81.7214 94.4141 0 176.043 0 276.686C0 377.329 81.7214 459 182.521 459C283.32 459 365 377.371 365 276.686C365 176.001 283.32 94.4141 182.521 94.4141ZM182.521 374.355C128.515 374.355 84.7404 330.63 84.7404 276.728C84.7404 222.825 128.515 179.058 182.521 179.058C236.527 179.058 280.26 222.783 280.26 276.728C280.26 330.672 236.527 374.355 182.521 374.355Z" fill="{color_hex}"/>
<path d="M243.399 84.6861C266.811 84.6861 285.79 65.7284 285.79 42.343C285.79 18.9576 266.811 0 243.399 0C219.987 0 201.008 18.9576 201.008 42.343C201.008 65.7284 219.987 84.6861 243.399 84.6861Z" fill="{color_hex}"/>
<path d="M121.727 84.6861C145.139 84.6861 164.118 65.7284 164.118 42.343C164.118 18.9576 145.139 0 121.727 0C98.3151 0 79.3359 18.9576 79.3359 42.343C79.3359 65.7284 98.3151 84.6861 121.727 84.6861Z" fill="{color_hex}"/>
</svg>'''
    return cairosvg.svg2png(bytestring=svg.encode(), output_width=LOGO_W, output_height=LOGO_H)


def get_average_color(img: Image.Image, x: int, y: int, w: int, h: int):
    """Средний цвет области изображения."""
    region = img.crop((x, y, x + w, y + h)).convert("RGB")
    arr = np.array(region)
    return arr[:, :, 0].mean(), arr[:, :, 1].mean(), arr[:, :, 2].mean()


def adjust_brightness(r, g, b, percent):
    """Сдвиг яркости на percent% (+ светлее, - темнее)."""
    if percent > 0:
        r = min(255, r + (255 - r) * percent / 100)
        g = min(255, g + (255 - g) * percent / 100)
        b = min(255, b + (255 - b) * percent / 100)
    else:
        p = abs(percent)
        r = max(0, r - r * p / 100)
        g = max(0, g - g * p / 100)
        b = max(0, b - b * p / 100)
    return int(r), int(g), int(b)


def hex_color(r, g, b):
    return f"#{r:02x}{g:02x}{b:02x}"


def process_image(img: Image.Image, format_key: str, hashtag: str) -> Image.Image:
    """Накладывает логотип и хештег на изображение."""
    fmt = FORMATS[format_key]

    # Размер канваса
    if fmt is None:
        canvas_w, canvas_h = img.size
    else:
        canvas_w, canvas_h = fmt

    # Центрируем изображение
    canvas = Image.new("RGB", (canvas_w, canvas_h), (0, 0, 0))
    img_ratio = img.width / img.height
    canvas_ratio = canvas_w / canvas_h

    if img_ratio > canvas_ratio:
        draw_h = canvas_h
        draw_w = int(draw_h * img_ratio)
        offset_x = (canvas_w - draw_w) // 2
        offset_y = 0
    else:
        draw_w = canvas_w
        draw_h = int(draw_w / img_ratio)
        offset_x = 0
        offset_y = (canvas_h - draw_h) // 2

    resized = img.resize((draw_w, draw_h), Image.LANCZOS)
    canvas.paste(resized, (offset_x, offset_y))

    # --- ЛОГОТИП ---
    logo_x = LOGO_LEFT
    logo_y = canvas_h - LOGO_BOTTOM - LOGO_H

    r, g, b = get_average_color(canvas, logo_x, logo_y, LOGO_W, LOGO_H)
    brightness = (r * 299 + g * 587 + b * 114) / 1000
    is_dark = brightness < 128
    percent = BRIGHTNESS_OFFSET if is_dark else -BRIGHTNESS_OFFSET
    lr, lg, lb = adjust_brightness(r, g, b, percent)
    logo_color = hex_color(lr, lg, lb)

    logo_png = get_svg_logo(logo_color)
    logo_img = Image.open(io.BytesIO(logo_png)).convert("RGBA")

    # Применяем прозрачность
    logo_with_alpha = logo_img.copy()
    logo_with_alpha.putalpha(
        Image.fromarray((np.array(logo_img)[:, :, 3] * ALPHA).astype(np.uint8))
    )
    canvas.paste(logo_with_alpha, (logo_x, logo_y), logo_with_alpha)

    # --- ХЕШТЕГ ---
    if hashtag and hashtag != "— Без хештега —":
        hx = canvas_w - HASHTAG_RIGHT
        hy = canvas_h - HASHTAG_BOTTOM

        # Цвет под хештегом
        sample_x = max(0, hx - 200)
        sample_y = max(0, hy - HASHTAG_SIZE)
        hr, hg, hb = get_average_color(canvas, sample_x, sample_y, 200, HASHTAG_SIZE + 20)
        h_brightness = (hr * 299 + hg * 587 + hb * 114) / 1000
        h_is_dark = h_brightness < 128
        h_percent = BRIGHTNESS_OFFSET if h_is_dark else -BRIGHTNESS_OFFSET
        hcr, hcg, hcb = adjust_brightness(hr, hg, hb, h_percent)

        # Рисуем текст
        draw = ImageDraw.Draw(canvas, "RGBA")
        try:
            font_path = os.path.join(os.path.dirname(__file__), "NunitoVariable.ttf")
            font = ImageFont.truetype(font_path, HASHTAG_SIZE)
        except Exception:
            try:
                font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", HASHTAG_SIZE)
            except Exception:
                font = ImageFont.load_default()

        alpha_val = int(255 * ALPHA)
        color = (hcr, hcg, hcb, alpha_val)

        bbox = draw.textbbox((0, 0), hashtag, font=font)
        text_w = bbox[2] - bbox[0]
        draw.text((hx - text_w, hy - HASHTAG_SIZE), hashtag, font=font, fill=color)

    return canvas


def format_keyboard():
    keys = list(FORMATS.keys())
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(k, callback_data=f"fmt:{k}") for k in keys[:4]],
        [InlineKeyboardButton(keys[4], callback_data=f"fmt:{keys[4]}")],
    ])


def hashtag_keyboard():
    rows = []
    row = []
    for i, tag in enumerate(HASHTAGS):
        row.append(InlineKeyboardButton(tag, callback_data=f"tag:{tag}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "👋 Привет! Я Post Creator для ÖMANKÖ.\n\n"
        "Отправь мне фото (можно сразу несколько), и я добавлю логотип и хештег.\n\n"
        "📸 Отправляй фото:"
    )
    return WAITING_PHOTOS


async def receive_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photos = context.user_data.setdefault("photos", [])

    if update.message.photo:
        file = await update.message.photo[-1].get_file()
        data = await file.download_as_bytearray()
        photos.append(bytes(data))
    elif update.message.document and update.message.document.mime_type.startswith("image/"):
        file = await update.message.document.get_file()
        data = await file.download_as_bytearray()
        photos.append(bytes(data))

    count = len(photos)
    await update.message.reply_text(
        f"✅ Фото получено ({count} шт). Отправь ещё или нажми /done когда закончишь."
    )
    return WAITING_PHOTOS


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photos = context.user_data.get("photos", [])
    if not photos:
        await update.message.reply_text("Сначала отправь хотя бы одно фото!")
        return WAITING_PHOTOS

    await update.message.reply_text(
        f"📐 Выбери формат ({len(photos)} фото):",
        reply_markup=format_keyboard()
    )
    return CHOOSING_FORMAT


async def choose_format(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    fmt = query.data.split(":", 1)[1]
    context.user_data["format"] = fmt

    await query.edit_message_text(
        f"Формат: {fmt} ✓\n\n# Выбери хештег:",
        reply_markup=hashtag_keyboard()
    )
    return CHOOSING_HASHTAG


async def choose_hashtag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    hashtag = query.data.split(":", 1)[1]
    fmt = context.user_data.get("format", "4:5")
    photos = context.user_data.get("photos", [])

    await query.edit_message_text(f"⚙️ Обрабатываю {len(photos)} фото...")

    for i, photo_bytes in enumerate(photos):
        try:
            img = Image.open(io.BytesIO(photo_bytes)).convert("RGB")
            result = process_image(img, fmt, hashtag)

            buf = io.BytesIO()
            result.save(buf, format="PNG", optimize=True)
            buf.seek(0)

            filename = f"1_{i+1}.png"
            await query.message.reply_document(
                document=buf,
                filename=filename,
                caption=f"1_{i+1}.png" if len(photos) > 1 else ""
            )
        except Exception as e:
            logger.error(f"Ошибка обработки фото {i+1}: {e}")
            await query.message.reply_text(f"❌ Ошибка с фото {i+1}: {e}")

    context.user_data.clear()
    await query.message.reply_text(
        "✅ Готово! Отправь новые фото или /start чтобы начать заново."
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Отменено. Напиши /start чтобы начать заново.")
    return ConversationHandler.END


def main():
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAITING_PHOTOS: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, receive_photos),
                CommandHandler("done", done),
            ],
            CHOOSING_FORMAT: [
                CallbackQueryHandler(choose_format, pattern="^fmt:"),
            ],
            CHOOSING_HASHTAG: [
                CallbackQueryHandler(choose_hashtag, pattern="^tag:"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv)
    app.run_polling()


if __name__ == "__main__":
    main()
