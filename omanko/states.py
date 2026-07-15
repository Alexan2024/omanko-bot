"""Константы состояний диалогов python-telegram-bot."""

# ============ Состояния диалога ============
CHOOSING_TYPE = 0
WAITING_PHOTOS = 1
CHOOSING_FORMAT = 2
CHOOSING_HASHTAG = 3
WAITING_TITLE = 4
CHOOSING_CHANNEL = 5
WAITING_PARTNER_LOGO = 6
WAITING_CUSTOM_HASHTAG = 7
WAITING_STORE_TEXT = 8
CHOOSING_STORE_COLOR = 9
STORE_COLOR_SLIDER = 10
COVER_DARK_SLIDER = 11

# Состояния рассылки (отдельный диалог, значения не пересекаются с основным)
BROADCAST_MSG = 100
BROADCAST_CONFIRM = 101
