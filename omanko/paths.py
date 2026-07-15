"""Пути: корень репозитория с ассетами и папка данных.

Модуль намеренно не знает про BOT_TOKEN и прочие секреты: его импортирует
imaging.py, а через него — render.py, который обязан работать в тестах без
переменных окружения.
"""
import os

# Корень репозитория: на два уровня вверх от omanko/paths.py.
# ВНИМАНИЕ: не упрощать до dirname(abspath(__file__)) — это даст omanko/,
# шрифты и логотипы лежат в корне, и load_*() молча откатятся на DejaVu.
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve_data_dir():
    """Где хранить users.json и stats.json так, чтобы пережило передеплой.
    Railway сам выставляет RAILWAY_VOLUME_MOUNT_PATH, когда к сервису подключён
    Volume — это самый надёжный признак постоянного хранилища. Если его нет,
    пробуем /data (на случай ручного монтирования), иначе пишем рядом с ботом —
    но это эфемерно: при следующем деплое всё обнулится.
    Возвращает (папка, постоянное_ли)."""
    vol = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    candidates = ([(vol, True)] if vol else []) + [("/data", False), (BASE, False)]
    for d, persistent in candidates:
        try:
            if os.path.isdir(d) and os.access(d, os.W_OK):
                return d, persistent
        except Exception:
            pass
    return BASE, False


DATA_DIR, STORAGE_PERSISTENT = _resolve_data_dir()
USERS_FILE = os.path.join(DATA_DIR, "users.json")
STATS_FILE = os.path.join(DATA_DIR, "stats.json")
SUBS_FILE = os.path.join(DATA_DIR, "stats_subs.json")
