"""Сторож мины BASE.

Шрифты грузятся от BASE = корень репозитория. Если BASE сломан (например,
указывает на omanko/ вместо корня), load_*() ловят исключение, пишут в лог
и молча откатываются на DejaVu — а он в Docker-образе установлен. Бот при
этом работает и рисует чужой гарнитурой.

Этот тест называет поломку по имени вместо «19 эталонов разошлись».
"""
import pytest

import subject as S


@pytest.mark.parametrize("loader, expected", [
    (S.load_semibold, "Nunito-SemiBold.ttf"),
    (S.load_black, "NunitoSans-Black.ttf"),
    (S.load_bold, "Nunito-VariableFont_wght.ttf"),
])
def test_font_loads_from_repo_root(loader, expected):
    font = loader(51)
    path = getattr(font, "path", None)
    assert path is not None, (
        f"{expected}: загрузился шрифт без пути (load_default) — BASE сломан"
    )
    assert path.endswith(expected), (
        f"загрузился {path} вместо {expected} — BASE указывает не на корень репозитория"
    )
