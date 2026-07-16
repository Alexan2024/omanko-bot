"""Рассылка, подписка на статистику и еженедельный отчёт."""
import asyncio
import io
import logging
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Forbidden, RetryAfter
from telegram.ext import ContextTypes, ConversationHandler

from omanko.config import MSK, REPORT_HOUR_MSK
from omanko.paths import STORAGE_PERSISTENT
from omanko.settings import ADMIN_ID, SUBSCRIBE_CMD, UNSUBSCRIBE_CMD
from omanko.states import BROADCAST_CONFIRM, BROADCAST_MSG
from omanko.stats_card import render_stats_card
from omanko.stats_report import build_weekly_report
from omanko.storage import (
    load_stats, load_subscribers, load_users, remove_users, save_subscribers,
)

logger = logging.getLogger(__name__)


# ============ Рассылка ============
async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    # Пока ADMIN_ID не задан — отвечаем всем (разовая настройка: так ты узнаёшь
    # свой ID). Как только ADMIN_ID прописан — команда отвечает только тебе,
    # для остальных её как будто не существует.
    if ADMIN_ID != 0 and uid != ADMIN_ID:
        return
    await update.message.reply_text(
        f"Твой Telegram ID: `{uid}`\n\n"
        "Чтобы включить рассылку, добавь его в Railway: "
        "Variables → ADMIN_ID → этот номер, затем передеплой.",
        parse_mode="Markdown"
    )


async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    # Любой, кроме админа, — молча игнорируем, чтобы для остальных
    # пользователей ничего не менялось. (Пока ADMIN_ID == 0, не совпадёт
    # ни с кем: сначала задай ADMIN_ID, потом пользуйся рассылкой.)
    if uid != ADMIN_ID:
        return ConversationHandler.END
    n = len(load_users())
    await update.message.reply_text(
        f"📣 Рассылка по {n} пользователям.\n\n"
        "Пришли сообщение, которое разослать (текст, фото, что угодно — "
        "уйдёт как есть). /cancel — отмена."
    )
    return BROADCAST_MSG


async def broadcast_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["bc_chat"] = update.effective_chat.id
    context.user_data["bc_msg"] = update.message.message_id
    n = len(load_users())
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ Отправить ({n})", callback_data="bc:go"),
        InlineKeyboardButton("❌ Отмена", callback_data="bc:no"),
    ]])
    await update.message.reply_text(
        f"Сообщение выше уйдёт {n} пользователям. Отправляем?",
        reply_markup=kb
    )
    return BROADCAST_CONFIRM


async def broadcast_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "bc:no":
        context.user_data.clear()
        await query.edit_message_text("Рассылка отменена.")
        return ConversationHandler.END

    src_chat = context.user_data.get("bc_chat")
    src_msg = context.user_data.get("bc_msg")
    users = load_users()
    await query.edit_message_text(f"📤 Рассылаю {len(users)} пользователям...")

    sent = failed = 0
    dead = []
    for target in list(users):
        try:
            await context.bot.copy_message(chat_id=target, from_chat_id=src_chat, message_id=src_msg)
            sent += 1
        except RetryAfter as e:
            await asyncio.sleep(int(e.retry_after) + 1)
            try:
                await context.bot.copy_message(chat_id=target, from_chat_id=src_chat, message_id=src_msg)
                sent += 1
            except Exception:
                failed += 1
        except Forbidden:
            # пользователь заблокировал бота — убираем из базы
            failed += 1
            dead.append(target)
        except Exception as e:
            failed += 1
            logger.error(f"Рассылка для {target}: {e}")
        await asyncio.sleep(0.05)  # бережём лимиты Telegram (~30/сек)

    if dead:
        remove_users(dead)

    context.user_data.clear()
    report = f"✅ Готово.\nДоставлено: {sent}\nНе доставлено: {failed}"
    if dead:
        report += f"\nУбрал заблокировавших: {len(dead)}"
    await query.message.reply_text(report)
    return ConversationHandler.END


async def weekly_stats_job(context: ContextTypes.DEFAULT_TYPE):
    """Раз в день срабатывает в REPORT_HOUR_MSK:00 МСК; шлём отчёт только по
    пятницам — админу и всем подписчикам. Заблокировавших бот убираем из
    подписки (админа не трогаем)."""
    now = datetime.now(MSK)
    if now.weekday() != 4:  # 4 = пятница (Пн=0 … Вс=6)
        return
    recipients = load_subscribers()
    if ADMIN_ID != 0:
        recipients.add(ADMIN_ID)
    if not recipients:
        logger.info("Еженедельный отчёт: ни подписчиков, ни ADMIN_ID — пропускаю.")
        return

    all_events = load_stats()
    report = build_weekly_report(all_events, until=now)
    card = render_stats_card(all_events, until=now)  # PNG-байты или None
    dead = []
    for target in list(recipients):
        try:
            await context.bot.send_message(chat_id=target, text=report, parse_mode="Markdown")
        except Forbidden:
            dead.append(target)
            continue  # заблокировал бот — карточку даже не пытаемся слать
        except RetryAfter as e:
            await asyncio.sleep(int(e.retry_after) + 1)
            try:
                await context.bot.send_message(chat_id=target, text=report, parse_mode="Markdown")
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Еженедельный отчёт для {target}: {e}")
        # Карточка отправляется отдельным сообщением после текста (свежий
        # BytesIO на каждого получателя — Telegram «вычитывает» поток).
        if card:
            try:
                await context.bot.send_photo(chat_id=target, photo=io.BytesIO(card))
            except Forbidden:
                if target not in dead:
                    dead.append(target)
            except RetryAfter as e:
                await asyncio.sleep(int(e.retry_after) + 1)
            except Exception as e:
                logger.error(f"Карточка статистики для {target}: {e}")
        await asyncio.sleep(0.05)  # бережём лимиты Telegram

    if dead:
        subs = load_subscribers()
        subs -= set(dead)
        save_subscribers(subs)
        logger.info(f"Еженедельный отчёт: убрал заблокировавших из подписки: {len(dead)}.")


def _stats_allowed(uid: int, chat_id: int) -> bool:
    """Кому доступна статистика: пока ADMIN_ID не задан — всем (для настройки),
    затем — админу и подписчикам скрытой команды."""
    if ADMIN_ID == 0:
        return True
    if uid == ADMIN_ID:
        return True
    return chat_id in load_subscribers()


async def stats_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Скрытая команда подписки. Кто её знает — тот подписывается на пятничный
    отчёт и получает доступ к /stats."""
    chat_id = update.effective_chat.id
    subs = load_subscribers()
    if chat_id in subs:
        await update.message.reply_text(
            f"📊 Ты уже в деле — сводка прилетает по пятницам в "
            f"{REPORT_HOUR_MSK}:00 МСК. И /stats тоже твоя 😎"
        )
        return
    subs.add(chat_id)
    save_subscribers(subs)
    await update.message.reply_text(
        "📊 *Подписка оформлена!*\n\n"
        f"Каждую пятницу в *{REPORT_HOUR_MSK}:00 МСК* тебе будет прилетать "
        "сводка по ÖMANKÖ — сколько постов и фото сделано за неделю.\n\n"
        "Бонусом открыл доступ к /stats — зови в любой момент 🔥\n\n"
        f"Передумаешь — /{UNSUBSCRIBE_CMD}",
        parse_mode="Markdown"
    )


async def stats_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отписка от еженедельного отчёта (и от доступа к /stats)."""
    chat_id = update.effective_chat.id
    subs = load_subscribers()
    if chat_id not in subs:
        return  # тихо — команда скрытая, незнакомцам реагировать незачем
    subs.discard(chat_id)
    save_subscribers(subs)
    await update.message.reply_text(
        f"Отписал от еженедельной сводки. Захочешь обратно — /{SUBSCRIBE_CMD} 👋"
    )


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика по запросу (за последние 7 дней) + состояние хранилища.
    Доступна админу и подписчикам; для остальных команды как будто нет."""
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    if not _stats_allowed(uid, chat_id):
        return
    all_events = load_stats()
    report = build_weekly_report(all_events)
    storage = ("🟢 постоянное (Railway Volume) — переживёт деплой"
               if STORAGE_PERSISTENT else
               "🔴 ВРЕМЕННОЕ — данные обнулятся при следующем деплое. "
               "Подключи Volume в Railway (mount path любой, бот подхватит сам).")
    await update.message.reply_text(
        f"{report}\n\n_Хранилище: {storage}_",
        parse_mode="Markdown"
    )
    # Визуальная карточка отдельным сообщением (если есть что показывать).
    card = render_stats_card(all_events)
    if card:
        try:
            await update.message.reply_photo(photo=io.BytesIO(card))
        except Exception as e:
            logger.error(f"Не смог отправить карточку статистики: {e}")