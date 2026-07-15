"""Единственная точка, которой тесты касаются кода бота.

Во время распила код переезжает из bot.py в пакет omanko/. Тесты эталонов
не должны меняться при каждом переносе — меняется только этот файл.

Сейчас: монолит bot.py (до Задачи 6).
"""
import bot as _m

process_image = _m.process_image
process_collab = _m.process_collab
render_cover_feed = _m.render_cover_feed
render_cover_story = _m.render_cover_story
process_store = _m.process_store

load_semibold = _m.load_semibold
load_black = _m.load_black
load_bold = _m.load_bold

NO_HASHTAG = _m.NO_HASHTAG
STORE_COLOR_LIGHT = _m.STORE_COLOR_LIGHT
STORE_COLOR_DARK = _m.STORE_COLOR_DARK
store_gray_value = _m._store_gray_value
