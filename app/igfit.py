"""Карусель Instagram без полей.

Instagram держит всю карусель в одной пропорции, поэтому раньше кадры другой формы
вписывались целиком и получали поля — чаще всего тонкую рамку в пару десятков пикселей.

Теперь иначе:
  1. Пропорция поста считается по медиане пропорций его фотографий (зажата в 4:5…1.91:1),
     а не по первому кадру: набор из горизонталей 3:2 даёт ровно 3:2 и ни одной обрезки.
  2. Кадр, теряющий при обрезке до этой пропорции не больше 15% площади, просто режется.
  3. Кадр, теряющий больше, в карусель не идёт. После отсева медиана считается заново
     по оставшимся — обычно это убирает и часть обрезок.
  4. Обложка (первый кадр) не выбрасывается никогда: если из общей пропорции выпадает она,
     пропорция считается по ней, а не вписывающиеся кадры уходят.
  5. Если после отсева осталось меньше трёх кадров, пропорция берётся по обложке и режутся все —
     но только пока самый тяжёлый кроп не превышает 30%. Если превышает, карусель просто
     становится короче: пост из одного-двух кадров лучше, чем кадр, разрезанный пополам.

Полей не остаётся ни в одном случае, IG_PAD_COLOR больше ни на что не влияет.
В Telegram уходит полный набор фото в своих пропорциях — этот модуль трогает только Instagram.

Здесь только расчёт: plan — пропорция и какие кадры идут, crop — обрезка. Сами фото готовит
instagram._photos; обрезка делается до instagram._fit, поэтому логотип встаёт в угол кадра."""
import logging
import os
from pathlib import Path

from PIL import Image, ImageOps

log = logging.getLogger(__name__)

MAX_CROP = float(os.getenv("IG_MAX_CROP", "0.15"))      # доля площади, которую можно срезать
MIN_PHOTOS = int(os.getenv("IG_MIN_PHOTOS", "3"))       # меньше — пропорция по обложке, режем всех
HARD_CROP = float(os.getenv("IG_HARD_CROP", "0.30"))    # потолок для такой вынужденной обрезки
TOP_BIAS = float(os.getenv("IG_TOP_BIAS", "0.35"))      # вертикальный срез: 0.5 по центру, меньше — ближе к верху
IG_MIN, IG_MAX = 0.8, 1.91                              # пределы Instagram: 4:5 … 1.91:1



# ======================= расчёт =======================

def _clamp(r: float) -> float:
    return min(max(r, IG_MIN), IG_MAX)


def read_ratio(path: Path) -> float:
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        return im.width / im.height


def _loss(r: float, target: float) -> float:
    """Доля площади, теряемая при обрезке кадра пропорции r до target."""
    return 1 - min(r, target) / max(r, target)


def _median(values: list[float]) -> float:
    v = sorted(values)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def plan(ratios: list[float]) -> tuple[float, list[int]]:
    """Пропорции кадров → (пропорция карусели, номера кадров, которые в неё идут)."""
    n = len(ratios)
    if n == 1:
        return _clamp(ratios[0]), [0]

    cover = _clamp(ratios[0])
    target = _clamp(_median(ratios))
    if _loss(ratios[0], target) > MAX_CROP:              # обложка выпадает — пропорция по ней
        target = cover
    else:
        first = [i for i in range(n) if _loss(ratios[i], target) <= MAX_CROP]
        if len(first) < n:                               # пересчёт медианы по оставшимся
            again = _clamp(_median([ratios[i] for i in first]))
            if _loss(ratios[0], again) <= MAX_CROP:
                target = again

    keep = [i for i in range(n) if _loss(ratios[i], target) <= MAX_CROP]
    if 0 not in keep:                                    # кадр вне пределов Instagram, но это обложка
        keep.insert(0, 0)

    if n >= MIN_PHOTOS and len(keep) < MIN_PHOTOS:       # карусель обмелела
        worst = max(_loss(r, cover) for r in ratios)
        if worst <= HARD_CROP:                           # дотянуть всех до обложки — терпимо
            return cover, list(range(n))
    return target, keep


def crop(im: Image.Image, target: float) -> Image.Image:
    """Кадр строго под пропорцию: по горизонтали — по центру, по вертикали — со смещением к верху."""
    im = ImageOps.exif_transpose(im).convert("RGB")
    w, h = im.size
    r = w / h
    if abs(r - target) / target < 0.002:
        return im
    if r > target:                                       # шире цели — режем бока
        nw, nh = max(1, round(h * target)), h
        x, y = (w - nw) // 2, 0
    else:                                                # выше цели — режем верх и низ
        nw, nh = w, max(1, round(w / target))
        x, y = 0, round((h - nh) * TOP_BIAS)
    return im.crop((x, y, x + nw, y + nh))
