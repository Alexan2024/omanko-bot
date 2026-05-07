import os
import io
import logging
from PIL import Image, ImageDraw, ImageFont
import numpy as np
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, ConversationHandler
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("BOT_TOKEN")

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

LOGO_W = 56
LOGO_H = 71
LOGO_LEFT = 92
LOGO_BOTTOM = 70
HASHTAG_RIGHT = 80
HASHTAG_BOTTOM = 79
HASHTAG_SIZE = 51
BRIGHTNESS_OFFSET = 45
ALPHA = 0.75


def draw_logo(canvas: Image.Image, x: int, y: int, w: int, h: int, color: tuple):
    """Рисует логотип Ö через Pillow — без cairosvg."""
    logo = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(logo)
    r, g, b = color
    a = int(255 * ALPHA)
    fill = (r, g, b, a)
    sx = w / 365
    sy = h / 459
    # Внешнее кольцо
    outer = [0, int(94*sy), w - 1, h - 1]
    inner = [int(84*sx), int(179*sy), int(280*sx), int(375*sy)]
    d.ellipse(outer, fill=fill)
    d.ellipse(inner, fill=(0, 0, 0, 0))
    # Левая точка умлаута
    d.ellipse([int(79*sx), int(0*sy), int(164*sx), int(85*sy)], fill=fill)
    # Правая точка умлаута
    d.ellipse([int(201*sx), int(0*sy), int(286*sx), int(85*sy)], fill=fill)
    canvas.paste(logo, (x, y), logo)


def get_average_color(img: Image.Image, x: int, y: int, w: int, h: int):
    x2 = min(x + w, img.width)
    y2 = min(y + h, img.height)
    region = img.crop((x, y, x2, y2)).convert("RGB")
    arr = np.array(region).reshape(-1, 3).mean(axis=0)
    return float(arr[0]), float(arr[1]), float(arr[2])


def adjust_brightness(r, g, b, percent):
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


def process_image(img: Image.Image, format_key: str, hashtag: str) -> Image.Image:
    fmt = FORMATS[format_key]
    if fmt is None:
        # Адаптивный: сохраняем пропорции, но минимальная ширина 1920px
        if img.width < 1920:
            scale = 1920 / img.width
            canvas_w = 1920
            canvas_h = int(img.height * scale)
        else:
            canvas_w, canvas_h = img.size
    else:
        canvas_w, canvas_h = fmt

    # Все размеры элементов масштабируем от ширины канваса
    scale = canvas_w / 1920
    logo_w = int(LOGO_W * scale)
    logo_h = int(LOGO_H * scale)
    logo_x = int(LOGO_LEFT * scale)
    logo_y = canvas_h - int(LOGO_BOTTOM * scale) - logo_h
    hashtag_right = int(HASHTAG_RIGHT * scale)
    hashtag_bottom = int(HASHTAG_BOTTOM * scale)
    hashtag_size = int(HASHTAG_SIZE * scale)

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

    # ЛОГОТИП
    r, g, b = get_average_color(canvas, logo_x, logo_y, logo_w, logo_h)
    brightness = (r * 299 + g * 587 + b * 114) / 1000
    percent = BRIGHTNESS_OFFSET if brightness < 128 else -BRIGHTNESS_OFFSET
    logo_color = adjust_brightness(r, g, b, percent)
    canvas_rgba = canvas.convert("RGBA")
    draw_logo(canvas_rgba, logo_x, logo_y, logo_w, logo_h, logo_color)
    canvas = canvas_rgba.convert("RGB")

    # ХЕШТЕГ
    if hashtag and hashtag != "— Без хештега —":
        sample_x = max(0, canvas_w - hashtag_right - int(200 * scale))
        sample_y = max(0, canvas_h - hashtag_bottom - hashtag_size)
        hr, hg, hb = get_average_color(canvas, sample_x, sample_y, int(200 * scale), hashtag_size + 20)
        h_brightness = (hr * 299 + hg * 587 + hb * 114) / 1000
        h_percent = BRIGHTNESS_OFFSET if h_brightness < 128 else -BRIGHTNESS_OFFSET
        hcr, hcg, hcb = adjust_brightness(hr, hg, hb, h_percent)

        overlay = canvas.convert("RGBA")
        draw = ImageDraw.Draw(overlay)

        try:
            font_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Nunito-SemiBold.ttf")
            if not os.path.exists(font_path):
                raise FileNotFoundError(f"Шрифт не найден: {font_path}")
            font = ImageFont.truetype(font_path, hashtag_size)
            logger.info(f"Шрифт загружен: {font_path}, размер {hashtag_size}px")
        except Exception as e:
            logger.error(f"Ошибка шрифта: {e}. Ищем системный...")
            # Ищем любой системный ttf шрифт
            system_fonts = [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
            ]
            font = None
            for sf in system_fonts:
                if os.path.exists(sf):
                    font = ImageFont.truetype(sf, hashtag_size)
                    logger.info(f"Используем системный шрифт: {sf}")
                    break
            if font is None:
                logger.error("Системный шрифт не найден, используем load_default")
                font = ImageFont.load_default(size=hashtag_size)

        fill = (hcr, hcg, hcb, int(255 * ALPHA))
        spacing = int(hashtag_size * (-0.007))

        total_w = 0
        char_widths = []
        for ch in hashtag:
            bbox = draw.textbbox((0, 0), ch, font=font)
            w = bbox[2] - bbox[0]
            char_widths.append(w)
            total_w += w + spacing
        total_w -= spacing

        tx = canvas_w - hashtag_right - total_w
        ty = canvas_h - hashtag_bottom - hashtag_size

        cx = tx
        for ch, cw in zip(hashtag, char_widths):
            draw.text((cx, ty), ch, font=font, fill=fill)
            cx += cw + spacing

        canvas = overlay.convert("RGB")

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
    for tag in HASHTAGS:
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
        "📎 Отправляй фото как *файл* (скрепка → Файл), а не как обычное фото — так качество сохранится.\n\n"
        "Отправь фото, затем /done",
        parse_mode="Markdown"
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
    await update.message.reply_text(f"✅ {len(photos)} фото. Ещё или /done")
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
    context.user_data["format"] = query.data.split(":", 1)[1]
    await query.edit_message_text("# Выбери хештег:", reply_markup=hashtag_keyboard())
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
            result.save(buf, format="JPEG", quality=92)
            buf.seek(0)
            await query.message.reply_document(document=buf, filename=f"1_{i+1}.jpg")
        except Exception as e:
            logger.error(f"Ошибка фото {i+1}: {e}")
            await query.message.reply_text(f"❌ Ошибка с фото {i+1}: {e}")

    context.user_data.clear()
    await query.message.reply_text("✅ Готово! /start чтобы начать заново.")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Отменено. /start чтобы начать заново.")
    return ConversationHandler.END


def main():
    app = (
        Application.builder()
        .token(TOKEN)
        .read_timeout(120)
        .write_timeout(120)
        .connect_timeout(30)
        .build()
    )
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAITING_PHOTOS: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, receive_photos),
                CommandHandler("done", done),
            ],
            CHOOSING_FORMAT: [CallbackQueryHandler(choose_format, pattern="^fmt:")],
            CHOOSING_HASHTAG: [CallbackQueryHandler(choose_hashtag, pattern="^tag:")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv)
    app.run_polling()


if __name__ == "__main__":
    main()
