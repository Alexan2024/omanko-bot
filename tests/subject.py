"""Единственная точка, которой тесты касаются кода бота.

Во время распила код переезжает из bot.py в пакет omanko/. Тесты эталонов
не должны меняться при каждом переносе — меняется только этот файл.

Сейчас: пакет omanko/ (с Задачи 6). Эталоны бьют в omanko.render напрямую,
минуя bot.py — то есть без telegram и без BOT_TOKEN.
"""
from omanko import config as _c
from omanko import imaging as _i
from omanko import render as _r

process_image = _r.process_image
process_collab = _r.process_collab
render_cover_feed = _r.render_cover_feed
render_cover_story = _r.render_cover_story
process_store = _r.process_store

load_semibold = _i.load_semibold
load_black = _i.load_black
load_bold = _i.load_bold

NO_HASHTAG = _c.NO_HASHTAG
STORE_COLOR_LIGHT = _c.STORE_COLOR_LIGHT
STORE_COLOR_DARK = _c.STORE_COLOR_DARK
store_gray_value = _c._store_gray_value
