"""Генератор тестовых исходников. Запускается один раз, результат коммитится.

Детерминирован: ни одного случайного числа, только градиенты и фигуры.
Перезапуск обязан давать байт в байт те же файлы.

Почему без шума: рендер реагирует на яркость участков кадра, а не на
зернистость. Пиксельный шум не добавляет покрытия, но делает PNG
несжимаемым — с ним фикстуры весили 17 МБ, а эталоны потянули бы на
сотни. Гладкий градиент плюс фигуры дают ту же вариативность яркости
за единицы процентов веса.

Запуск:
    docker run --rm --platform=linux/amd64 -v "$PWD:/app" -w /app \
        omanko-test python tests/make_fixtures.py
"""
import os

import numpy as np
from PIL import Image, ImageDraw

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _photo(w, h, top_rgb, bottom_rgb, shapes):
    """Вертикальный градиент плюс крупные фигуры — имитация кадра.

    Градиент нужен, чтобы get_average_color() в разных зонах давал разную
    яркость: это включает адаптивный подбор цвета логотипа и плотность
    градиента обложки. Фигуры добавляют локальный контраст в углах, где
    садятся логотип и хештег.

    shapes: список (вид, координаты, цвет) в долях от размера холста.
    """
    t = np.linspace(0.0, 1.0, h, dtype=np.float64)[:, None, None]
    top = np.array(top_rgb, dtype=np.float64)[None, None, :]
    bottom = np.array(bottom_rgb, dtype=np.float64)[None, None, :]
    grad = np.repeat(top * (1.0 - t) + bottom * t, w, axis=1)
    img = Image.fromarray(np.clip(grad, 0, 255).astype(np.uint8), mode="RGB")

    d = ImageDraw.Draw(img)
    for kind, (x0, y0, x1, y1), color in shapes:
        box = (int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h))
        getattr(d, kind)(box, fill=color)
    return img


def _partner_logo():
    """Белый силуэт на прозрачном фоне — как требует режим коллаборации."""
    img = Image.new("RGBA", (400, 160), (255, 255, 255, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((10, 30, 110, 130), fill=(255, 255, 255, 255))
    d.rectangle((140, 40, 260, 120), fill=(255, 255, 255, 255))
    d.polygon([(290, 130), (350, 30), (390, 130)], fill=(255, 255, 255, 255))
    return img


def main():
    os.makedirs(OUT, exist_ok=True)

    # Светлый кадр, уже 1920 — включает апскейл в адаптивном режиме.
    # Тёмное пятно в левом нижнем углу: там садится логотип брендинга,
    # и на нём проверяется вторая ветка выбора его цвета.
    _photo(1600, 1200, (232, 228, 220), (250, 248, 244), [
        ("ellipse", (0.02, 0.72, 0.30, 0.98), (40, 44, 52)),
        ("rectangle", (0.55, 0.10, 0.95, 0.40), (150, 140, 130)),
    ]).save(os.path.join(OUT, "light.png"), optimize=True)

    # Тёмный кадр — вторая ветка плотности градиента обложки.
    # Светлое пятно в углу логотипа — зеркальный случай к light.
    _photo(1600, 1200, (18, 20, 26), (44, 40, 38), [
        ("ellipse", (0.02, 0.72, 0.30, 0.98), (215, 210, 200)),
        ("rectangle", (0.55, 0.10, 0.95, 0.40), (90, 96, 110)),
    ]).save(os.path.join(OUT, "dark.png"), optimize=True)

    # Шире 1920 — адаптивный режим без апскейла. Светлый верх, тёмный низ:
    # заголовок обложки и логотип попадают в зоны разной яркости.
    _photo(2400, 1600, (240, 238, 232), (22, 20, 18), [
        ("rectangle", (0.30, 0.40, 0.70, 0.60), (128, 126, 122)),
        ("ellipse", (0.05, 0.05, 0.25, 0.25), (60, 58, 55)),
    ]).save(os.path.join(OUT, "mixed.png"), optimize=True)

    _partner_logo().save(os.path.join(OUT, "partner_logo.png"), optimize=True)

    total = 0
    for f in sorted(os.listdir(OUT)):
        size = os.path.getsize(os.path.join(OUT, f))
        total += size
        print(f"  {f:<20} {size / 1024:>8.1f} КБ")
    print(f"  {'всего':<20} {total / 1024:>8.1f} КБ")


if __name__ == "__main__":
    main()
