"""Сборка приложения: ConversationHandler, команды, джобы, polling."""
import logging
from datetime import time as dtime

from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ConversationHandler
)

from omanko.admin import (
    broadcast_confirm, broadcast_receive, broadcast_start, myid, stats_cmd,
    stats_subscribe, stats_unsubscribe, weekly_stats_job,
)
from omanko.config import MSK, REPORT_HOUR_MSK
from omanko.handlers import (
    back_cover_slider, back_from_format, back_store_color_to_text,
    back_store_slider_to_color, back_to_channel, back_to_format,
    back_to_photos, back_to_type, cancel, choose_channel, choose_format,
    choose_hashtag, choose_store_color, choose_type, cover_dark_slider,
    done, receive_custom_hashtag, receive_partner_logo, receive_photos,
    receive_store_text, receive_title, start, store_gray_slider,
)
from omanko.paths import DATA_DIR, STORAGE_PERSISTENT
from omanko.settings import SUBSCRIBE_CMD, TOKEN, UNSUBSCRIBE_CMD
from omanko.states import (
    BROADCAST_CONFIRM, BROADCAST_MSG, CHOOSING_CHANNEL, CHOOSING_FORMAT,
    CHOOSING_HASHTAG, CHOOSING_STORE_COLOR, CHOOSING_TYPE, COVER_DARK_SLIDER,
    STORE_COLOR_SLIDER, WAITING_CUSTOM_HASHTAG, WAITING_PARTNER_LOGO,
    WAITING_PHOTOS, WAITING_STORE_TEXT, WAITING_TITLE,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main():
    app = (
        Application.builder()
        .token(TOKEN)
        .read_timeout(120).write_timeout(120).connect_timeout(30)
        .build()
    )
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSING_TYPE: [CallbackQueryHandler(choose_type, pattern="^type:")],
            CHOOSING_CHANNEL: [
                CallbackQueryHandler(choose_channel, pattern="^channel:"),
                CallbackQueryHandler(back_to_type, pattern="^nav:back$"),
            ],
            WAITING_PARTNER_LOGO: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, receive_partner_logo),
                CallbackQueryHandler(back_to_type, pattern="^nav:back$"),
            ],
            WAITING_PHOTOS: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, receive_photos),
                CommandHandler("done", done),
                CallbackQueryHandler(back_to_channel, pattern="^nav:back$"),
            ],
            WAITING_TITLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_title),
                CallbackQueryHandler(back_to_photos, pattern="^nav:back$"),
            ],
            CHOOSING_FORMAT: [
                CallbackQueryHandler(choose_format, pattern="^fmt:"),
                CallbackQueryHandler(back_from_format, pattern="^nav:back$"),
            ],
            CHOOSING_HASHTAG: [
                CallbackQueryHandler(choose_hashtag, pattern="^tag:"),
                CallbackQueryHandler(back_to_format, pattern="^nav:back$"),
            ],
            WAITING_CUSTOM_HASHTAG: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_custom_hashtag),
                CallbackQueryHandler(back_to_format, pattern="^nav:back$"),
            ],
            WAITING_STORE_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_store_text),
                CallbackQueryHandler(back_to_photos, pattern="^nav:back$"),
            ],
            CHOOSING_STORE_COLOR: [
                CallbackQueryHandler(choose_store_color, pattern="^scol:"),
                CallbackQueryHandler(back_store_color_to_text, pattern="^nav:back$"),
            ],
            STORE_COLOR_SLIDER: [
                CallbackQueryHandler(store_gray_slider, pattern="^sgray:"),
                CallbackQueryHandler(back_store_slider_to_color, pattern="^nav:back$"),
            ],
            COVER_DARK_SLIDER: [
                CallbackQueryHandler(cover_dark_slider, pattern="^cdark:"),
                CallbackQueryHandler(back_cover_slider, pattern="^nav:back$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start)],
    )
    bc_conv = ConversationHandler(
        entry_points=[CommandHandler("broadcast", broadcast_start)],
        states={
            BROADCAST_MSG: [MessageHandler(~filters.COMMAND, broadcast_receive)],
            BROADCAST_CONFIRM: [CallbackQueryHandler(broadcast_confirm, pattern="^bc:")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler(SUBSCRIBE_CMD, stats_subscribe))
    app.add_handler(CommandHandler(UNSUBSCRIBE_CMD, stats_unsubscribe))
    app.add_handler(bc_conv)
    app.add_handler(conv)

    logger.info(
        f"Хранилище: {DATA_DIR} "
        f"({'постоянное (Volume)' if STORAGE_PERSISTENT else 'ВРЕМЕННОЕ — нужен Volume!'})"
    )
    if app.job_queue:
        app.job_queue.run_daily(
            weekly_stats_job,
            time=dtime(hour=REPORT_HOUR_MSK, minute=0, tzinfo=MSK),
        )
        logger.info(f"Еженедельный отчёт: запланирован на пятницу {REPORT_HOUR_MSK}:00 МСК.")
    else:
        logger.warning(
            "JobQueue недоступна — еженедельный отчёт не запустится. "
            "Нужно: python-telegram-bot[job-queue] в requirements.txt."
        )

    app.run_polling()