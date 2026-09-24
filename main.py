import asyncio
import json
import logging
import math
import shutil
import time
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand, ErrorEvent
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app import (attribution, config, curator, db, growth, instagram, pipeline, repeats, reports, request, screen,
                 slots, stats, stories, taste, ui)
from app import dates, reels
from app.bot import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ahmag")

_last_alert: dict[str, float] = {}


def guarded(bot: Bot, name: str, fn, *args):
    """Задача расписания: ошибка не роняет бота, а приходит автору (не чаще раза в час на задачу)."""
    async def job():
        try:
            await fn(*args)
        except Exception as exc:
            log.exception(name)
            if time.time() - _last_alert.get(name, 0) > 3600:
                _last_alert[name] = time.time()
                try:
                    await bot.send_message(config.ADMIN_ID, f"⚠️ Сбой в задаче «{name}»: {curator.explain(exc)}"[:1000])
                except Exception:
                    log.exception("alert")
    return job


async def collect(bot: Bot):
    before = await db.get_setting("api_error")
    await pipeline.run_collection(manual=False)
    request.after_collection()   # запас меньше нормы — из отложенных соберётся один пост
    after = await db.get_setting("api_error")
    # об ошибке доступа к Claude сообщаем один раз, а не при каждом сборе
    if after and (not before or before["text"] != after["text"]):
        await screen.notify(bot, f"⚠️ {after['text']}")
    screen.refresh_soon(bot)


async def poll(bot: Bot):
    """Каждые 10 минут: забрать готовые пакеты оценки."""
    closed = await pipeline.poll_batches()
    if not closed:
        return
    for c in closed:
        if c["manual"]:
            await screen.notify(bot, f"📦 Оценка готова: в запас +{c['made']}"
                                     + (f", не вышло {c['failed']}" if c["failed"] else "") + ".",
                                [("🏠 Экран", "n:home")])
    screen.refresh_soon(bot)


async def digest(bot: Bot):
    await screen.notify(bot, await reports.digest_text(7), [("🏠 Экран", "n:home")])


async def cleanup():
    """Удаляет файлы давно решённых постов и отсеянных кандидатов, чтобы диск не рос бесконечно."""
    root = config.IMG_DIR.resolve()
    for r in await db.finished_before(config.IMAGE_TTL_DAYS):
        images = json.loads(r["images"] or "[]")
        if images:
            folder = Path(images[0]).parent.resolve()
            if folder != root and root in folder.parents:
                shutil.rmtree(folder, ignore_errors=True)
    for c in await db.candidates("skipped", limit=2000):
        shutil.rmtree(config.IMG_DIR / f"c{c['id']}", ignore_errors=True)


async def startup(bot: Bot):
    """После перезапуска: забрать пакеты, при пустом запасе — собрать, освежить экран."""
    await poll(bot)
    await repeats.backfill()          # отпечатки для защиты от повторов у постов, собранных до v4
    await attribution.snapshot(bot)   # первая точка кривой подписчиков
    await instagram.check(bot)        # статус для кнопки «📸 Instagram» на пульте
    await instagram.refresh_token()
    await instagram.process_pending(bot)
    last = await db.get_setting("last_collect")
    stale = not last or (datetime.fromisoformat(db.now()) - datetime.fromisoformat(last)).total_seconds() > 6 * 3600
    if stale and await db.count_ready() + await db.batched_count() < pipeline.target_stock():
        await collect(bot)
    # полуавтомат после перезапуска: если на сегодня плана нет, собираем его на оставшиеся слоты
    if await slots.mode() == "semi" and not await slots.paused():
        today = await slots.day_state(0)
        if not any(x["state"] in ("offered", "approved", "announced") for x in today if x["dt"] > slots._now()):
            made, missing = await slots.build_plan(0)
            if made:
                await screen.notify(bot, slots.plan_summary(made, missing, "сегодня"), [("📥 Разобрать", "n:inbox")])
    screen.refresh_soon(bot)


async def main():
    await db.init()
    await repeats.init()
    await stats.init()
    await stories.init()
    await taste.init()
    await dates.init()
    await reels.init()
    await growth.init()
    await instagram.init()
    screen.banner()
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    ui.BOT = bot                      # для уведомлений из фоновых задач
    growth.install(bot)               # виды раздела «📈 Рост»
    dates.install(bot)                # экран «📅 Даты»
    reels.install(bot)                # экран «🎬 Рилсы»
    taste.install(bot)                # экран «🧠 Вкус»
    stats.install(bot)                # экран «🏆 Что заходит»
    instagram.install(bot)            # вид «📸 Instagram»; сам пост уходит туда из cards.publish_post
    dp = Dispatcher()
    dp.include_router(attribution.router)   # вступления и выходы в канале
    dp.include_router(growth.router)        # раздел «Рост» — раньше основного, чтобы кнопки g:… не ушли в «старые»
    dp.include_router(instagram.router)     # кнопки ig:…
    dp.include_router(router)

    @dp.error()
    async def on_error(event: ErrorEvent) -> None:
        """Ошибка в кнопке или команде приходит сообщением, а не пропадает в логе."""
        exc = event.exception
        log.exception("Ошибка обработчика", exc_info=exc)
        text = f"⚠️ {curator.explain(exc)}"
        cb = event.update.callback_query
        try:
            if cb:
                await cb.answer("Не получилось", show_alert=False)
                await bot.send_message(cb.from_user.id, text)
            elif event.update.message:
                await event.update.message.answer(text)
        except Exception:
            log.exception("Не смог сообщить об ошибке")

    sched = AsyncIOScheduler(timezone=config.TZ_NAME, job_defaults={"misfire_grace_time": 600, "coalesce": True})
    for h, m in config.COLLECT_TIMES:
        sched.add_job(guarded(bot, "сбор", collect, bot), "cron", hour=h, minute=m, id=f"collect_{h}_{m}",
                      max_instances=1)
    sched.add_job(guarded(bot, "оценка пакетом", poll, bot), "interval", minutes=10, id="poll", max_instances=1)
    per_delivery = math.ceil(config.DAILY_MAX / max(1, len(config.DELIVERY_HOURS)))
    for h in config.DELIVERY_HOURS:
        sched.add_job(guarded(bot, "входящие", slots.deliver_manual, bot, per_delivery), "cron",
                      hour=h, minute=0, id=f"deliver_{h}")
    for h, m, fmt in config.SLOTS:
        pre = (h * 60 + m - config.SLOT_LEAD_MIN) % 1440
        sched.add_job(guarded(bot, f"анонс {h:02d}:{m:02d}", slots.prepare, bot, h, m, fmt),
                      "cron", hour=pre // 60, minute=pre % 60, id=f"prep_{h}_{m}")
        sched.add_job(guarded(bot, f"слот {h:02d}:{m:02d}", slots.fire, bot, h, m, fmt),
                      "cron", hour=h, minute=m, id=f"fire_{h}_{m}")
    ph, pm = config.PLAN_TIME
    sched.add_job(guarded(bot, "план на завтра", slots.evening, bot), "cron", hour=ph, minute=pm, id="evening")
    sched.add_job(guarded(bot, "срок входящих", slots.expire_inbox, bot), "interval", hours=1, id="expire")
    sched.add_job(guarded(bot, "дайджест", digest, bot), "cron",
                  day_of_week=config.DIGEST_DOW, hour=config.DIGEST_HOUR, id="digest")
    sched.add_job(guarded(bot, "очистка", cleanup), "cron", hour=4, minute=30, id="cleanup")
    growth.schedule(sched, bot, guarded)
    instagram.schedule(sched, bot, guarded)
    stats.schedule(sched, bot, guarded)
    taste.schedule(sched, bot, guarded)
    dates.schedule(sched, bot, guarded)
    reels.schedule(sched, bot, guarded)
    sched.start()

    await instagram.start_server()   # Instagram забирает фото по публичной ссылке
    await slots.reschedule()   # посты, чей слот прошёл или исчез из расписания, пока бот не работал
    await bot.set_my_commands([
        BotCommand(command="menu", description="Экран бота"),
        BotCommand(command="next", description="Следующий пост"),
        BotCommand(command="stats", description="Где сейчас посты"),
        BotCommand(command="diag", description="Проверить, всё ли работает"),
        BotCommand(command="date", description="Пост к дате: /date 9.03 о чём"),
        BotCommand(command="cancel", description="Отменить ввод"),
    ])
    asyncio.create_task(guarded(bot, "запуск", startup, bot)())
    log.info("AHMAG curator v%s запущен · режим %s · слоты %s · слот находки %s · страховка %s", config.VERSION,
             await slots.mode(), config.SLOTS, "%02d:%02d" % slots.find_slot() if slots.find_slot() else "нет",
             "вкл" if config.SEMI_FALLBACK else "выкл")
    # chat_member приходит только если явно запрошен — список собирается по подключённым обработчикам
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
