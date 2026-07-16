"""Чтение и запись состояния: пользователи, подписчики, журнал событий.

Базы данных нет — состояние лежит в трёх JSON-файлах. Запись идёт через
временный файл с os.replace, чтобы падение на середине не било данные.
"""
import json
import logging
import os
from datetime import datetime, timezone

from omanko.config import _STATS_CAP
from omanko.paths import USERS_FILE, STATS_FILE, SUBS_FILE

logger = logging.getLogger(__name__)


def load_users() -> set:
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return set(int(x) for x in json.load(f))
    except Exception:
        return set()


def save_users(users) -> None:
    try:
        tmp = USERS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(users), f)
        os.replace(tmp, USERS_FILE)
    except Exception as e:
        logger.error(f"Не смог сохранить список пользователей: {e}")


def add_user(chat_id: int) -> None:
    users = load_users()
    if chat_id not in users:
        users.add(chat_id)
        save_users(users)


def remove_users(ids) -> None:
    users = load_users()
    users -= set(ids)
    save_users(users)

# ============ Статистика производства ============
# Один завершённый цикл (нажал /start → выбрал тип/канал → прислал фото →
# получил картинки) = один «пост». В цикле может быть несколько фото — это
# «обработанные фотографии». Каждое событие пишем в stats.json одной строкой:
# дата (UTC, ISO), канал, режим (type1/cover), сколько фото реально обработано.

def load_subscribers() -> set:
    try:
        with open(SUBS_FILE, "r", encoding="utf-8") as f:
            return set(int(x) for x in json.load(f))
    except Exception:
        return set()


def save_subscribers(subs) -> None:
    try:
        tmp = SUBS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(subs), f)
        os.replace(tmp, SUBS_FILE)
    except Exception as e:
        logger.error(f"Не смог сохранить подписчиков статистики: {e}")


def load_stats() -> list:
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def save_stats(events) -> None:
    try:
        tmp = STATS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(events, f, ensure_ascii=False)
        os.replace(tmp, STATS_FILE)
    except Exception as e:
        logger.error(f"Не смог сохранить статистику: {e}")


def record_post(channel: str, mode: str, n_photos: int) -> None:
    if n_photos <= 0:
        return
    events = load_stats()
    events.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "channel": channel,
        "mode": mode if mode in ("type1", "cover", "store") else "type1",
        "photos": int(n_photos),
    })
    if len(events) > _STATS_CAP:
        events = events[-_STATS_CAP:]
    save_stats(events)
