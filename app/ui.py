"""Общие мелочи интерфейса: удалить сообщения, показать пост на экране, спросить текст.
Здесь же ссылка на бота для фоновых задач — её ставит main.py при запуске."""
from aiogram import Bot
from aiogram.fsm.context import FSMContext

from app import config, screen

BOT: Bot | None = None     # main.py → ui.BOT = bot


async def drop(bot: Bot, *ids) -> None:
    for mid in ids:
        if mid:
            try:
                await bot.delete_message(config.ADMIN_ID, mid)
            except Exception:
                pass


async def show_post(bot: Bot, **arg) -> None:
    """Показать пост на экране в текущем режиме просмотра (или в указанном)."""
    view, cur = await screen.current()
    base = cur if view == "list" else {"mode": "one"}
    await screen.show(bot, "list", **{**base, "kb": None, **arg})


async def ask(bot: Bot, state: FSMContext, st, text: str, **data) -> None:
    await state.set_state(st)
    m = await bot.send_message(config.ADMIN_ID, text)
    await state.update_data(prompt=m.message_id, **data)
    await screen.add_temp([m.message_id])
