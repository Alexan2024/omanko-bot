"""Хендлеры основного диалога и навигация «Назад»."""
import io
import logging

from PIL import Image
from telegram import Update, InputMediaPhoto
from telegram.ext import ContextTypes, ConversationHandler

from omanko.config import (
    CHANNELS, CUSTOM_HASHTAG_CB, DARK_DEFAULT_IDX, DARK_LEVELS,
    STORE_COLOR_DARK, STORE_COLOR_LIGHT, STORE_GRAY_DEFAULT_IDX,
    STORE_GRAY_STEPS, _store_gray_value,
)
from omanko.keyboards import (
    back_keyboard, channel_keyboard, cover_dark_keyboard, dark_meter,
    format_keyboard, hashtag_keyboard, store_color_keyboard,
    store_gray_keyboard, type_keyboard, _store_gray_hex, _store_gray_meter,
)
from omanko.render import (
    process_collab, process_image, process_store, render_cover_feed,
    render_cover_story,
)
from omanko.states import (
    CHOOSING_CHANNEL, CHOOSING_FORMAT, CHOOSING_HASHTAG, CHOOSING_STORE_COLOR,
    CHOOSING_TYPE, COVER_DARK_SLIDER, STORE_COLOR_SLIDER,
    WAITING_CUSTOM_HASHTAG, WAITING_PARTNER_LOGO, WAITING_PHOTOS,
    WAITING_STORE_TEXT, WAITING_TITLE,
)
from omanko.storage import add_user, record_post

logger = logging.getLogger(__name__)


# ============ Хендлеры ============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_user(update.effective_chat.id)
    context.user_data.clear()
    await update.message.reply_text(
        "👋 Привет! Я Post Creator для ÖMANKÖ.\n\nЧто делаем?",
        reply_markup=type_keyboard()
    )
    return CHOOSING_TYPE


async def choose_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mode = query.data.split(":", 1)[1]
    context.user_data["mode"] = mode
    if mode == "collab":
        context.user_data["channel"] = "base"  # коллаб всегда на базовом вордмарке
        await query.edit_message_text(
            "Режим: *Коллаборация* 🤝\n\n"
            "Сначала пришли *логотип партнёра*:\n"
            "PNG, белый, без фона. Отправляй как *файл* (скрепка → Файл), "
            "чтобы не потерять прозрачность.",
            parse_mode="Markdown",
            reply_markup=back_keyboard(),
        )
        return WAITING_PARTNER_LOGO
    if mode == "store":
        context.user_data["channel"] = "base"  # Ö всегда базовый векторный
        await query.edit_message_text(
            photos_prompt_text(context),
            parse_mode="Markdown",
            reply_markup=back_keyboard(),
        )
        return WAITING_PHOTOS
    name = "Обложка" if mode == "cover" else "Брендинг"
    await query.edit_message_text(
        f"Режим: *{name}*\n\nТеперь выбери канал:",
        parse_mode="Markdown",
        reply_markup=channel_keyboard()
    )
    return CHOOSING_CHANNEL


def photos_prompt_text(context) -> str:
    """Текст шага «пришли фото» — общий для прямого хода и для возврата «Назад»."""
    mode = context.user_data.get("mode")
    n = len(context.user_data.get("photos", []))
    have = f"📂 Уже загружено: *{n}*. " if n else ""
    if mode == "collab":
        return (
            "🤝 *Коллаборация* — логотип партнёра принят.\n\n"
            "📎 Отправляй фото как *файл* (скрепка → Файл), чтобы качество не сжалось.\n\n"
            f"{have}Пришли фото, затем /done"
        )
    if mode == "store":
        return (
            "🛍 *ÖMANKÖ STORE*\n\n"
            "📎 Отправляй фото как *файл* (скрепка → Файл), чтобы качество не сжалось.\n\n"
            f"{have}Пришли фото, затем /done"
        )
    channel = context.user_data.get("channel", "base")
    note = ""
    if mode != "cover":
        note = "_(в Тип 1 логотип Ö общий для всех каналов)_\n\n"
    return (
        f"Канал: *{CHANNELS[channel]['title']}*\n\n"
        f"{note}"
        "📎 Отправляй фото как *файл* (скрепка → Файл), чтобы качество не сжалось.\n\n"
        f"{have}Пришли фото, затем /done"
    )


async def receive_partner_logo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Коллаборация: приём белого PNG-логотипа партнёра (как файл или фото)."""
    data = None
    doc = update.message.document
    if doc and (doc.mime_type or "").startswith("image/"):
        file = await doc.get_file()
        data = bytes(await file.download_as_bytearray())
    elif update.message.photo:
        file = await update.message.photo[-1].get_file()
        data = bytes(await file.download_as_bytearray())

    if not data:
        await update.message.reply_text(
            "Это не похоже на картинку 🙃 Пришли *PNG* логотипа партнёра "
            "(лучше как файл, чтобы сохранить прозрачность).",
            parse_mode="Markdown", reply_markup=back_keyboard())
        return WAITING_PARTNER_LOGO
    try:
        Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:
        await update.message.reply_text(
            "Не смог открыть это как изображение. Пришли PNG ещё раз.",
            reply_markup=back_keyboard())
        return WAITING_PARTNER_LOGO

    context.user_data["partner_logo"] = data
    await update.message.reply_text(
        photos_prompt_text(context),
        parse_mode="Markdown", reply_markup=back_keyboard())
    return WAITING_PHOTOS


async def choose_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    channel = query.data.split(":", 1)[1]
    if channel not in CHANNELS:
        channel = "base"
    context.user_data["channel"] = channel
    await query.edit_message_text(
        photos_prompt_text(context),
        parse_mode="Markdown",
        reply_markup=back_keyboard(),
    )
    return WAITING_PHOTOS


async def receive_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photos = context.user_data.setdefault("photos", [])
    if update.message.photo:
        file = await update.message.photo[-1].get_file()
        photos.append(bytes(await file.download_as_bytearray()))
    elif update.message.document and update.message.document.mime_type.startswith("image/"):
        file = await update.message.document.get_file()
        photos.append(bytes(await file.download_as_bytearray()))
    await update.message.reply_text(f"✅ {len(photos)} фото. Ещё или /done")
    return WAITING_PHOTOS


TITLE_PROMPT = ("✍️ Пришли *текст заголовка*.\n"
                "Переносы строк ставь сам — как нужно на обложке.")

STORE_TEXT_PROMPT = ("✍️ Пришли *текст подписи* для STORE — 2 строки.\n"
                     "Перенос между строками ставь сам (Enter).")


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photos = context.user_data.get("photos", [])
    if not photos:
        await update.message.reply_text("Сначала отправь хотя бы одно фото!")
        return WAITING_PHOTOS
    if context.user_data.get("mode") == "cover":
        await update.message.reply_text(
            TITLE_PROMPT, parse_mode="Markdown", reply_markup=back_keyboard()
        )
        return WAITING_TITLE
    if context.user_data.get("mode") == "store":
        await update.message.reply_text(
            STORE_TEXT_PROMPT, parse_mode="Markdown", reply_markup=back_keyboard()
        )
        return WAITING_STORE_TEXT
    await update.message.reply_text(
        f"📐 Выбери формат ({len(photos)} фото):", reply_markup=format_keyboard()
    )
    return CHOOSING_FORMAT


async def receive_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = (update.message.text or "").strip("\n")
    if not title.strip():
        await update.message.reply_text("Заголовок пустой — пришли текст ещё раз.")
        return WAITING_TITLE
    context.user_data["title"] = title
    await update.message.reply_text(
        "📐 Выбери формат ленты (сторис IG и TG добавлю автоматически):",
        reply_markup=format_keyboard()
    )
    return CHOOSING_FORMAT


async def choose_format(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["format"] = query.data.split(":", 1)[1]
    if context.user_data.get("mode") == "collab":
        return await generate_collab(update, context)
    channel = context.user_data.get("channel", "base")
    await query.edit_message_text("Выбери хештег:", reply_markup=hashtag_keyboard(channel))
    return CHOOSING_HASHTAG


async def generate_collab(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Коллаборация: рендер всех фото с нижней строкой «ÖMANKÖ × партнёр».
    Хештегов нет — генерим сразу после выбора формата."""
    query = update.callback_query
    fmt = context.user_data.get("format", "4:5")
    photos = context.user_data.get("photos", [])
    partner = context.user_data.get("partner_logo")

    await query.edit_message_text(f"⚙️ Обрабатываю {len(photos)} фото...")

    if not partner:
        await query.message.reply_text(
            "Потерялся логотип партнёра 😅 Начни заново через /start.")
        context.user_data.clear()
        return ConversationHandler.END

    ok = 0
    for i, photo_bytes in enumerate(photos):
        try:
            img = Image.open(io.BytesIO(photo_bytes)).convert("RGB")
            result = process_collab(img, fmt, partner)
            buf = io.BytesIO()
            result.save(buf, format="JPEG", quality=92)
            buf.seek(0)
            await query.message.reply_document(document=buf, filename=f"collab_{i+1}.jpg")
            ok += 1
        except Exception as e:
            logger.error(f"Ошибка коллаб-фото {i+1}: {e}")
            await query.message.reply_text(f"❌ Ошибка с фото {i+1}: {e}")

    record_post(context.user_data.get("channel", "base"), "collab", ok)
    context.user_data.clear()
    await query.message.reply_text("✅ Готово! /start чтобы начать заново.")
    return ConversationHandler.END


async def receive_store_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """STORE: приём подписи (до 2 строк) → переход к выбору цвета графики."""
    text = (update.message.text or "").strip("\n")
    if not text.strip():
        await update.message.reply_text(
            "Текст пустой — пришли подпись ещё раз (2 строки).",
            reply_markup=back_keyboard())
        return WAITING_STORE_TEXT
    context.user_data["store_text"] = text
    await update.message.reply_text(
        "🎨 *Цвет графики* (логотип + текст):",
        parse_mode="Markdown", reply_markup=store_color_keyboard())
    return CHOOSING_STORE_COLOR


def _render_store_preview(context: ContextTypes.DEFAULT_TYPE, idx: int) -> bytes:
    """Превью первого фото на текущем оттенке слайдера (уменьшенное, для скорости)."""
    photos = context.user_data.get("photos", [])
    text = context.user_data.get("store_text", "")
    img = Image.open(io.BytesIO(photos[0])).convert("RGB")
    res = process_store(img, text, color=_store_gray_value(idx))
    res.thumbnail((1200, 1500), Image.LANCZOS)
    buf = io.BytesIO()
    res.save(buf, format="JPEG", quality=85)
    buf.seek(0)
    return buf.getvalue()


def _store_slider_caption(idx: int) -> str:
    return (f"🎚 Оттенок графики (ЧБ)\n{_store_gray_meter(idx)}\n"
            f"`{_store_gray_hex(idx)}`")


async def choose_store_color(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]

    if choice in ("adaptive", "light", "dark"):
        context.user_data["store_color"] = {
            "adaptive": None,
            "light": STORE_COLOR_LIGHT,
            "dark": STORE_COLOR_DARK,
        }[choice]
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text("⚙️ Обрабатываю фото...")
        return await generate_store(query.message, context)

    # custom → ЧБ-слайдер с живым превью
    idx = STORE_GRAY_DEFAULT_IDX
    context.user_data["store_gray_idx"] = idx
    preview = _render_store_preview(context, idx)
    if query.message.photo:
        await query.edit_message_media(
            InputMediaPhoto(media=io.BytesIO(preview),
                            caption=_store_slider_caption(idx), parse_mode="Markdown"),
            reply_markup=store_gray_keyboard(idx))
    else:
        await query.message.reply_photo(
            photo=io.BytesIO(preview), caption=_store_slider_caption(idx),
            parse_mode="Markdown", reply_markup=store_gray_keyboard(idx))
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
    return STORE_COLOR_SLIDER


async def store_gray_slider(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    action = query.data.split(":", 1)[1]

    if action == "noop":
        await query.answer()
        return STORE_COLOR_SLIDER

    if action == "apply":
        idx = context.user_data.get("store_gray_idx", STORE_GRAY_DEFAULT_IDX)
        context.user_data["store_color"] = _store_gray_value(idx)
        await query.answer("Применяю…")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text("⚙️ Обрабатываю фото...")
        return await generate_store(query.message, context)

    idx = context.user_data.get("store_gray_idx", STORE_GRAY_DEFAULT_IDX)
    new_idx = max(0, min(STORE_GRAY_STEPS, idx + (1 if action == "up" else -1)))
    if new_idx == idx:
        await query.answer("Дальше некуда 🙂")
        return STORE_COLOR_SLIDER
    context.user_data["store_gray_idx"] = new_idx
    await query.answer()
    preview = _render_store_preview(context, new_idx)
    try:
        await query.edit_message_media(
            InputMediaPhoto(media=io.BytesIO(preview),
                            caption=_store_slider_caption(new_idx), parse_mode="Markdown"),
            reply_markup=store_gray_keyboard(new_idx))
    except Exception as e:
        logger.error(f"STORE слайдер: не смог обновить превью: {e}")
    return STORE_COLOR_SLIDER


async def back_store_color_to_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.message.photo:
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.message.reply_text(
            STORE_TEXT_PROMPT, parse_mode="Markdown", reply_markup=back_keyboard())
    else:
        await query.edit_message_text(
            STORE_TEXT_PROMPT, parse_mode="Markdown", reply_markup=back_keyboard())
    return WAITING_STORE_TEXT


async def back_store_slider_to_color(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await query.edit_message_caption(
            caption="🎨 *Цвет графики* (логотип + текст):",
            parse_mode="Markdown", reply_markup=store_color_keyboard())
    except Exception:
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.message.reply_text(
            "🎨 *Цвет графики* (логотип + текст):",
            parse_mode="Markdown", reply_markup=store_color_keyboard())
    return CHOOSING_STORE_COLOR


async def generate_store(message, context: ContextTypes.DEFAULT_TYPE):
    """STORE: рендер всех фото в формат витрины 2000×2500 и отправка файлами."""
    photos = context.user_data.get("photos", [])
    text = context.user_data.get("store_text", "")
    color = context.user_data.get("store_color")  # None=адаптивный или (r,g,b)
    ok = 0
    for i, photo_bytes in enumerate(photos):
        try:
            img = Image.open(io.BytesIO(photo_bytes)).convert("RGB")
            result = process_store(img, text, color=color)
            buf = io.BytesIO()
            result.save(buf, format="JPEG", quality=92)
            buf.seek(0)
            await message.reply_document(document=buf, filename=f"store_{i+1}.jpg")
            ok += 1
        except Exception as e:
            logger.error(f"Ошибка стор-фото {i+1}: {e}")
            await message.reply_text(f"❌ Ошибка с фото {i+1}: {e}")

    record_post(context.user_data.get("channel", "base"), "store", ok)
    context.user_data.clear()
    await message.reply_text("✅ Готово! /start чтобы начать заново.")
    return ConversationHandler.END


async def _send_covers(message, photos, title, hashtag, fmt, channel, dark_idx) -> int:
    """Рендер + отправка обложек (feed/ig/tg) для всех фото на заданном уровне
    затемнения. Используется и при первой генерации, и при ручной регулировке.
    Возвращает число успешно обработанных фото."""
    level = DARK_LEVELS[dark_idx]
    ok = 0
    for i, photo_bytes in enumerate(photos):
        try:
            img = Image.open(io.BytesIO(photo_bytes)).convert("RGB")
            feed = render_cover_feed(img, fmt, title, hashtag, dark_level=level)
            ig = render_cover_story(img, "ig", title, hashtag, channel=channel, dark_level=level)
            tg = render_cover_story(img, "tg", title, hashtag, channel=channel, dark_level=level)
            for result, suffix in ((feed, "feed"), (ig, "ig"), (tg, "tg")):
                buf = io.BytesIO()
                result.save(buf, format="JPEG", quality=92)
                buf.seek(0)
                await message.reply_document(document=buf, filename=f"cover_{i+1}_{suffix}.jpg")
            ok += 1
        except Exception as e:
            logger.error(f"Ошибка обложки {i+1}: {e}")
            await message.reply_text(f"❌ Ошибка с фото {i+1}: {e}")
    return ok


def _render_cover_preview(context: ContextTypes.DEFAULT_TYPE, idx: int) -> bytes:
    """Превью обложки (лента, первое фото) на текущем уровне затемнения —
    уменьшенное, для скорости. По нему пользователь подбирает плотность."""
    sess = context.user_data["cover_session"]
    img = Image.open(io.BytesIO(sess["photos"][0])).convert("RGB")
    res = render_cover_feed(img, sess["fmt"], sess["title"], sess["hashtag"],
                            dark_level=DARK_LEVELS[idx])
    res.thumbnail((1200, 1500), Image.LANCZOS)
    buf = io.BytesIO()
    res.save(buf, format="JPEG", quality=85)
    buf.seek(0)
    return buf.getvalue()


def _cover_slider_caption(idx: int) -> str:
    return ("🎚 Затемнение градиента — подбери под кадр.\n"
            "Превью на первом фото 👇\n\n"
            f"*Уровень:* {dark_meter(idx)}")


async def _render_with_hashtag(message, context: ContextTypes.DEFAULT_TYPE, hashtag: str):
    """Общий рендер для обоих путей выбора хештега (кнопка из списка / свой текст).
    Сообщение со статусом «Обрабатываю…» каждый путь шлёт сам — здесь только рендер."""
    fmt = context.user_data.get("format", "4:5")
    photos = context.user_data.get("photos", [])
    mode = context.user_data.get("mode", "type1")
    channel = context.user_data.get("channel", "base")

    if mode == "cover":
        title = context.user_data.get("title", "")
        idx = DARK_DEFAULT_IDX
        # Не генерируем сразу: сначала слайдер затемнения с живым превью.
        # Параметры держим в сессии — по ним рисуем превью и финальный рендер.
        context.user_data["cover_session"] = {
            "photos": photos, "title": title, "hashtag": hashtag,
            "fmt": fmt, "channel": channel, "dark_idx": idx,
        }
        preview = _render_cover_preview(context, idx)
        await message.reply_photo(
            photo=io.BytesIO(preview), caption=_cover_slider_caption(idx),
            parse_mode="Markdown", reply_markup=cover_dark_keyboard(idx))
        return COVER_DARK_SLIDER

    # ---- Тип 1 (брендинг) ----
    ok = 0
    for i, photo_bytes in enumerate(photos):
        try:
            img = Image.open(io.BytesIO(photo_bytes)).convert("RGB")
            result = process_image(img, fmt, hashtag, channel=channel)
            buf = io.BytesIO()
            result.save(buf, format="JPEG", quality=92)
            buf.seek(0)
            await message.reply_document(document=buf, filename=f"1_{i+1}.jpg")
            ok += 1
        except Exception as e:
            logger.error(f"Ошибка фото {i+1}: {e}")
            await message.reply_text(f"❌ Ошибка с фото {i+1}: {e}")

    record_post(channel, mode, ok)
    context.user_data.clear()
    await message.reply_text("✅ Готово! /start чтобы начать заново.")
    return ConversationHandler.END


async def choose_hashtag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tag = query.data.split(":", 1)[1]

    # Свой хештег — уводим в ввод текста, рендер будет после ввода
    if tag == CUSTOM_HASHTAG_CB:
        await query.edit_message_text(
            "✍️ Кидай свой хештег одним словом 🔥\n"
            "Можно с # или без — решётку добавлю сам. Например: лето"
        )
        return WAITING_CUSTOM_HASHTAG

    photos = context.user_data.get("photos", [])
    await query.edit_message_text(f"⚙️ Обрабатываю {len(photos)} фото...")
    return await _render_with_hashtag(query.message, context, tag)


async def receive_custom_hashtag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = (update.message.text or "").strip()
    token = raw.split()[0] if raw.split() else ""
    token = token.lstrip("#").strip()
    if not token:
        await update.message.reply_text("Пустой хештег — пришли ещё раз, например: лето")
        return WAITING_CUSTOM_HASHTAG
    hashtag = "#" + token

    photos = context.user_data.get("photos", [])
    await update.message.reply_text(f"⚙️ Обрабатываю {len(photos)} фото с {hashtag}...")
    return await _render_with_hashtag(update.message, context, hashtag)


async def cover_dark_slider(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Слайдер затемнения обложки с превью. Двигает уровень и перерисовывает
    превью на первом фото; по ✅ Сгенерировать — финальный рендер всех кадров
    (лента + IG + TG) на выбранном уровне затемнения."""
    query = update.callback_query
    action = query.data.split(":", 1)[1]

    sess = context.user_data.get("cover_session")
    if not sess:
        await query.answer("Сессия устарела — сделай новый пост через /start",
                           show_alert=True)
        return COVER_DARK_SLIDER

    if action == "noop":
        await query.answer()
        return COVER_DARK_SLIDER

    if action == "apply":
        idx = sess["dark_idx"]
        await query.answer("Генерирую…")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text("⚙️ Рендерю обложки и сторис...")
        ok = await _send_covers(query.message, sess["photos"], sess["title"],
                                sess["hashtag"], sess["fmt"], sess["channel"], idx)
        record_post(sess["channel"], "cover", ok)
        context.user_data.clear()
        await query.message.reply_text("✅ Готово! /start чтобы начать заново.")
        return ConversationHandler.END

    idx = sess["dark_idx"]
    new_idx = max(0, min(len(DARK_LEVELS) - 1, idx + (1 if action == "up" else -1)))
    if new_idx == idx:
        await query.answer("Дальше некуда 🙂")
        return COVER_DARK_SLIDER
    sess["dark_idx"] = new_idx
    await query.answer()
    preview = _render_cover_preview(context, new_idx)
    try:
        await query.edit_message_media(
            InputMediaPhoto(media=io.BytesIO(preview),
                            caption=_cover_slider_caption(new_idx), parse_mode="Markdown"),
            reply_markup=cover_dark_keyboard(new_idx))
    except Exception as e:
        logger.error(f"COVER слайдер: не смог обновить превью: {e}")
    return COVER_DARK_SLIDER


async def back_cover_slider(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Назад со слайдера затемнения → к выбору хештега."""
    query = update.callback_query
    await query.answer()
    channel = context.user_data.get("channel", "base")
    try:
        await query.message.delete()
    except Exception:
        pass
    await query.message.reply_text(
        "Выбери хештег:", reply_markup=hashtag_keyboard(channel))
    return CHOOSING_HASHTAG


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Отменено. /start чтобы начать заново.")
    return ConversationHandler.END


# ============ Навигация «Назад» ============
async def back_to_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Что делаем?", reply_markup=type_keyboard())
    return CHOOSING_TYPE


async def back_to_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mode = context.user_data.get("mode", "type1")
    if mode == "collab":
        await query.edit_message_text(
            "Режим: *Коллаборация* 🤝\n\n"
            "Пришли *логотип партнёра*: PNG, белый, без фона "
            "(лучше как файл).",
            parse_mode="Markdown", reply_markup=back_keyboard())
        return WAITING_PARTNER_LOGO
    if mode == "store":
        await query.edit_message_text("Что делаем?", reply_markup=type_keyboard())
        return CHOOSING_TYPE
    name = "Обложка" if mode == "cover" else "Брендинг"
    await query.edit_message_text(
        f"Режим: *{name}*\n\nТеперь выбери канал:",
        parse_mode="Markdown", reply_markup=channel_keyboard())
    return CHOOSING_CHANNEL


async def back_to_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        photos_prompt_text(context), parse_mode="Markdown", reply_markup=back_keyboard())
    return WAITING_PHOTOS


async def back_from_format(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Из выбора формата назад: в обложке → к заголовку, в Тип 1 → к фото."""
    query = update.callback_query
    await query.answer()
    if context.user_data.get("mode") == "cover":
        await query.edit_message_text(
            TITLE_PROMPT, parse_mode="Markdown", reply_markup=back_keyboard())
        return WAITING_TITLE
    await query.edit_message_text(
        photos_prompt_text(context), parse_mode="Markdown", reply_markup=back_keyboard())
    return WAITING_PHOTOS


async def back_to_format(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    photos = context.user_data.get("photos", [])
    await query.edit_message_text(
        f"📐 Выбери формат ({len(photos)} фото):", reply_markup=format_keyboard())
    return CHOOSING_FORMAT