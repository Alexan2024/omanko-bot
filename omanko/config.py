"""Все настраиваемые числа проекта.

Сюда, и только сюда, вносятся правки вида «подвинь логотип на 4 пикселя»
или «сделай хештег поменьше». Файл намеренно не дробится по темам: держать
все числа в одном месте важнее, чем тематическая раскладка.

Все размеры заданы в опорных единицах канваса 1920px и масштабируются
коэффициентом scale в местах использования.
"""
from datetime import timezone, timedelta

# ============ Статистика: время и лимиты ============
MSK = timezone(timedelta(hours=3))  # Москва — UTC+3, без переходов на летнее
REPORT_HOUR_MSK = 19  # час отправки еженедельного отчёта по пятницам (МСК)
_STATS_CAP = 5000  # держим файл в узде: храним последние N событий
_RU_MONTHS = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"]

FORMATS = {
    "4:5": (1920, 2400),
    "2:3": (1920, 2880),
    "1:1": (1920, 1920),
    "3:2": (1920, 1280),
    "Адаптивный": None,
}

# Кнопка «без хештега» (значение-метка, проверяется в рендере) и метка «свой хештег»
NO_HASHTAG = "— Без хештега —"
CUSTOM_HASHTAG_CB = "__custom__"

# Общий набор тегов для всех каналов
COMMON_HASHTAGS = [
    "#art", "#archives", "#community", "#item", "#paper",
    "#space", "#style", "#cinema", "#architecture", "#cars", "#fashion"
]

# Хештеги по каналам: общий набор, у гастро — общий + четыре своих.
# Править теги в одном месте: общий — в COMMON_HASHTAGS, гастро-специфику — в его строке.
CHANNEL_HASHTAGS = {
    "base":   COMMON_HASHTAGS,
    "news":   COMMON_HASHTAGS,
    "beauty": COMMON_HASHTAGS,
    "music":  COMMON_HASHTAGS,
    "agency": COMMON_HASHTAGS,
    "gastro": COMMON_HASHTAGS + ["#books", "#recommendation", "#interior", "#movies", "#moscow"],
}

# ============ Тип 1 (брендинг) — БЕЗ ИЗМЕНЕНИЙ ============
LOGO_W = 56
LOGO_H = 71
LOGO_LEFT = 92
LOGO_BOTTOM = 70
HASHTAG_RIGHT = 80
HASHTAG_BOTTOM = 79
HASHTAG_SIZE = 51
BRIGHTNESS_OFFSET = 45
ALPHA = 0.95

# ============ Коллаборация — параметры ============
# Нижняя строка «ÖMANKÖ × партнёр». Координаты в опорных единицах канваса 1920px.
COLLAB_WORDMARK_W = 327          # полный вордмарк ÖMANKÖ (ширина)
COLLAB_WORDMARK_H = 71           # высота (нативные пропорции вордмарка: 71/327 ≈ 0.219)
COLLAB_GAP = 43                  # пропуск между элементами (лого — × — лого)
COLLAB_X_GLYPH = "×"             # разделитель. Поставь "x", если нужна именно буква
COLLAB_X_SIZE = HASHTAG_SIZE     # 51 — шрифт/размер как у хештегов в брендинге
COLLAB_PARTNER_H = 58            # высота лого партнёра (ширина — пропорционально)
COLLAB_BOTTOM = LOGO_BOTTOM      # 70 — отступ снизу как у брендинга
COLLAB_WORDMARK_CENTER_DROP = 9  # центр вордмарка для выравнивания — на 9px ниже геометрического
# Цвет строки: True — адаптивный под фон (как брендинг); False — всегда белый
COLLAB_ADAPTIVE = True

# ============ Обложка — параметры ============
# Заголовок (Nunito Sans Black)
COVER_TITLE_SIZE_FEED = 135      # абсолютный размер на всех соотношениях ленты
COVER_TITLE_LS_FEED = -0.03      # letter-spacing -3%
COVER_TITLE_SIZE_STORY = 77      # на канвас 1080x1920
COVER_TITLE_LS_STORY = -0.06     # letter-spacing -6%
COVER_TITLE_BOTTOM_IG = 452      # 424 + 28 (поднят выше)
COVER_TITLE_BOTTOM_TG = 382      # 354 + 28 (поднят выше)
COVER_LINE_SPACING = 1.08        # межстрочный множитель

# Вордмарк ÖMANKÖ (всегда белый)
WORDMARK_W_FEED = 326            # низ ленты, отступ снизу 65
WORDMARK_BOTTOM_FEED = 65
WORDMARK_W_STORY = 195           # верх сторис, отступ сверху 168
WORDMARK_TOP_STORY = 168

# Бабл с хештегом
BUBBLE_TEXT_SIZE = HASHTAG_SIZE  # 51 — как в обычных постах
# Лента: бабл сверху по центру
FEED_BUBBLE_PAD_X = 48           # горизонтальный паддинг текста в бабле (лента)
# Сторис IG: бабл под заголовком
IG_BUBBLE_BOTTOM = 215
IG_BUBBLE_W = 387
IG_BUBBLE_H = 135
IG_BUBBLE_RADIUS = 41
# Сторис TG: бабл под заголовком
TG_BUBBLE_BOTTOM = 161
TG_BUBBLE_W = 430
TG_BUBBLE_H = 115
TG_BUBBLE_RADIUS = 17

# Бабл в ленте: тёмный, почти непрозрачный, с хештегом внутри
FEED_BUBBLE_ALPHA = 0.85
FEED_BUBBLE_FILL = (0, 0, 0)
# Бабл в сторис: ПУСТОЙ (без хештега), цвет инвертный к фону:
#   тёмный фон → светлый бабл, светлый фон → тёмный бабл
STORY_BUBBLE_ALPHA = 0.50

# Градиент под заголовком: чёрный снизу вверх, адаптивный
GRAD_ALPHA_DARK = 0.18           # фон тёмный → слабый градиент
GRAD_ALPHA_LIGHT = 0.62          # фон светлый → плотный
GRAD_ALPHA_CEIL = 0.99           # потолок плотности (почти полная чернота на максимуме)
GRAD_RISE_STORY = 900            # высота градиента над низом (на 1080w)

# Ручной регулятор затемнения обложек: число = СДВИГ плотности относительно
# адаптивной базы (1.0 = база без сдвига = текущее поведение). Сдвиг работает
# одинаково сильно и на тёмном, и на светлом фоне — в отличие от множителя.
DARK_LEVELS = [0.4, 0.7, 1.0, 1.4, 1.8]
DARK_DEFAULT_IDX = 2
DARK_LEVEL_NAMES = ["min", "светлее", "норма", "темнее", "max"]

STORY_SIZE = (1080, 1920)

# Пер-ратио геометрия обложек (ленты). Размеры абсолютные на своём канвасе.
# bubble_top — отступ бабла от верха; title_bottom — отступ заголовка от низа.
COVER_FORMATS = {
    "4:5": dict(size=(1920, 2400), bubble_h=126, bubble_top=68, title_bottom=365),
    "2:3": dict(size=(1920, 2560), bubble_h=126, bubble_top=68, title_bottom=385),
    "1:1": dict(size=(2400, 2400), bubble_h=158, bubble_top=85, title_bottom=411),
    "3:2": dict(size=(3600, 2400), bubble_h=126, bubble_top=68, title_bottom=440),
}
# Адаптивный режим обложки использует параметры 4:5
COVER_DEFAULT = dict(bubble_h=126, bubble_top=68, title_bottom=365)

# ============ ÖMANKÖ STORE ============
# Витрина магазина: фикс. вертикаль 2000×2500, фон-фото (cover-fit),
# угловой Ö (адаптивный цвет, как в брендинге) + подпись в 2 строки тем же
# цветом. Шрифт Nunito BOLD (не Sans), мелкий. Все значения — абсолютные px
# на холсте 2000×2500.
STORE_SIZE = (2000, 2500)
STORE_LOGO_W = 59
STORE_LOGO_H = 74
STORE_LOGO_LEFT = 95            # отступ лого слева
STORE_LOGO_BOTTOM = 76          # отступ лого снизу
STORE_TEXT_GAP = 20            # отступ текста от правого края лого
STORE_TEXT_SIZE = 15.62        # кегль подписи (Nunito Bold)
STORE_TEXT_BOTTOM = 86          # низ нижней строки от низа холста
STORE_LINE_HEIGHT = 0.9        # межстрочный интервал = 90% кегля

# Цвет графики STORE: None — адаптивный (как в брендинге), либо фикс. (r,g,b).
STORE_COLOR_LIGHT = (0xE4, 0xE4, 0xE5)   # светлый пресет #E4E4E5
STORE_COLOR_DARK = (0x68, 0x68, 0x68)    # тёмный пресет  #686868
# ЧБ-слайдер: позиции 0..STORE_GRAY_STEPS, белый (255) → чёрный (0).
STORE_GRAY_STEPS = 14
STORE_GRAY_DEFAULT_IDX = 7

# ============ Каналы сетки ============
# У каждого канала ДВА варианта лого (белые PNG, прозрачный фон):
#   type1_logo  — для режима «Тип 1» (угловой логотип внизу слева)
#   story_logo  — для обложек, используется ТОЛЬКО в сторис (IG/TG)
# Геометрия:
#   type1_box = (w, h, left, bottom) в координатах канваса 1920px (как LOGO_*),
#               масштабируется вместе с лентой.
#   story_box = (w, h) в координатах сторис 1080×1920; отступ сверху общий
#               (WORDMARK_TOP_STORY), лого центрируется по горизонтали.
# None в поле лого/бокса => базовое поведение:
#   type1 None  -> рисуем векторный Ö (адаптивный, размеры LOGO_*)
#   story None  -> широкий вордмарк ÖMANKÖ (как раньше)
# В ЛЕНТЕ обложки вордмарк ВСЕГДА базовый ÖMANKÖ (по каналу не меняется).
CHANNELS = {
    "base":   {"title": "основа ÖMANKÖ",
               "type1_logo": None, "type1_box": None,
               "story_logo": None, "story_box": None},
    "news":   {"title": "Ö NEWS",
               "type1_logo": "logo_type1_news.png",   "type1_box": (72, 112, 91, 47),
               "story_logo": "logo_cover_news.png",    "story_box": (196, 81)},
    "beauty": {"title": "Ö BEAUTY",
               "type1_logo": "logo_type1_beauty.png", "type1_box": (72, 107, 91, 52),
               "story_logo": "logo_cover_beauty.png",  "story_box": (196, 81)},
    "music":  {"title": "Ö MUSIC",
               "type1_logo": "logo_type1_music.png",  "type1_box": (89, 107, 91, 52),
               "story_logo": "logo_cover_music.png",   "story_box": (196, 81)},
    "agency": {"title": "Ö AGENCY",  # спека пока не задана — базовое поведение
               "type1_logo": None, "type1_box": None,
               "story_logo": None, "story_box": None},
    "gastro": {"title": "Ö GASTRO",
               "type1_logo": "logo_type1_gastro.png", "type1_box": (75, 119, 91, 40),
               "story_logo": "logo_cover_gastro.png",  "story_box": (196, 81)},
}

BASE_WORDMARK_FILE = "wordmark_white.png"  # широкий ÖMANKÖ: лента + база сторис
BASE_O_LOGO_FILE = "main_o.png"            # угловой логотип Ö (брендинг базы/agency + стор)

# ---- STORE: выбор цвета графики ----
def _store_gray_value(idx: int):
    """Позиция слайдера → серый (v,v,v). idx 0 = белый (255), max = чёрный (0)."""
    idx = max(0, min(STORE_GRAY_STEPS, idx))
    v = round(255 * (STORE_GRAY_STEPS - idx) / STORE_GRAY_STEPS)
    return (v, v, v)
