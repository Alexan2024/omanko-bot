"""Матрица случаев рендера для эталонных тестов.

Покрытие подобрано по веткам кода, а не по числу картинок:
  - светлый и тёмный кадр — обе ветки выбора цвета логотипа
    (brightness_of() < 128) и плотности градиента;
  - кадр шире и уже 1920 — обе ветки адаптивного канваса;
  - хештег и NO_HASHTAG — обе ветки отрисовки бабла;
  - каналы со своей геометрией (news/beauty/gastro/music) и без (base);
  - dark_level из краёв DARK_LEVELS и из середины;
  - store с фиксированным цветом и с color=None (адаптивный).
"""
import os

from PIL import Image

import subject as S

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _fx(name):
    """Свежая копия исходника на каждый вызов — на случай мутации внутри рендера."""
    with Image.open(os.path.join(FIX, f"{name}.png")) as im:
        return im.convert("RGB").copy()


def _partner():
    with open(os.path.join(FIX, "partner_logo.png"), "rb") as f:
        return f.read()


TITLE_SHORT = "ЗАГОЛОВОК"

# Перенос обязателен: draw_centered_title() режет текст по "\n" и НЕ переносит
# сам. Без явного "\n" заголовок остаётся одной строкой, множитель
# COVER_LINE_SPACING в расчёт не входит — и ошибка в нём прошла бы мимо тестов.
TITLE_TWO = "ЗАГОЛОВОК\nВ ДВЕ СТРОКИ"

STORE_TEXT = "КЕРАМИЧЕСКАЯ ВАЗА\n12 000 ₽"

CASES = {
    # --- Тип 1 (брендинг) ---
    "type1_base_4x5_tag": lambda: S.process_image(_fx("light"), "4:5", "#art", "base"),
    "type1_base_1x1_notag": lambda: S.process_image(_fx("light"), "1:1", S.NO_HASHTAG, "base"),
    "type1_news_4x5_dark": lambda: S.process_image(_fx("dark"), "4:5", "#art", "news"),
    "type1_beauty_3x2_dark": lambda: S.process_image(_fx("dark"), "3:2", "#style", "beauty"),
    "type1_music_2x3_light": lambda: S.process_image(_fx("light"), "2:3", "#cinema", "music"),
    "type1_gastro_adaptive_wide": lambda: S.process_image(_fx("mixed"), "Адаптивный", "#moscow", "gastro"),

    # --- Коллаборация ---
    "collab_4x5_light": lambda: S.process_collab(_fx("light"), "4:5", _partner()),
    "collab_adaptive_wide": lambda: S.process_collab(_fx("mixed"), "Адаптивный", _partner()),

    # --- Обложка: лента (channel не принимает — вордмарк всегда базовый) ---
    "cover_feed_4x5_norm": lambda: S.render_cover_feed(_fx("light"), "4:5", TITLE_TWO, "#art", 1.0),
    "cover_feed_1x1_notag_dark": lambda: S.render_cover_feed(_fx("dark"), "1:1", TITLE_SHORT, S.NO_HASHTAG, 1.0),
    "cover_feed_3x2_max": lambda: S.render_cover_feed(_fx("light"), "3:2", TITLE_SHORT, "#art", 1.8),
    "cover_feed_adaptive_min_wide": lambda: S.render_cover_feed(_fx("mixed"), "Адаптивный", TITLE_SHORT, "#art", 0.4),

    # --- Обложка: сторис ---
    "cover_story_ig_base_norm": lambda: S.render_cover_story(_fx("light"), "ig", TITLE_SHORT, "#art", "base", 1.0),
    "cover_story_tg_base_norm": lambda: S.render_cover_story(_fx("light"), "tg", TITLE_SHORT, "#art", "base", 1.0),
    "cover_story_ig_beauty_max_dark": lambda: S.render_cover_story(_fx("dark"), "ig", TITLE_TWO, "#style", "beauty", 1.8),
    "cover_story_tg_gastro_norm_wide": lambda: S.render_cover_story(_fx("mixed"), "tg", TITLE_SHORT, "#moscow", "gastro", 1.0),
    "cover_story_ig_news_min_dark": lambda: S.render_cover_story(_fx("dark"), "ig", TITLE_SHORT, "#art", "news", 0.4),

    # --- STORE ---
    "store_light_preset": lambda: S.process_store(_fx("light"), STORE_TEXT, S.STORE_COLOR_LIGHT),
    "store_dark_preset": lambda: S.process_store(_fx("dark"), STORE_TEXT, S.STORE_COLOR_DARK),
    "store_gray_mid": lambda: S.process_store(_fx("mixed"), STORE_TEXT, S.store_gray_value(7)),
    "store_adaptive_color": lambda: S.process_store(_fx("dark"), STORE_TEXT, None),
}
