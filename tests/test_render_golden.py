"""Эталонные тесты рендера.

Доказывают, что распил bot.py не изменил ни одного пикселя. Эталоны сняты
на монолите до начала переноса.

НИКОГДА не запускай GOLDEN_UPDATE=1 во время распила. Эталон, переснятый
ради зелёного прогона, уничтожает единственное доказательство корректности.
"""
import os

import numpy as np
import pytest
from PIL import Image

from cases import CASES

HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN_DIR = os.path.join(HERE, "golden")
OUTPUT_DIR = os.path.join(HERE, "output")
UPDATE = os.environ.get("GOLDEN_UPDATE") == "1"


@pytest.mark.parametrize("name", sorted(CASES))
def test_render_matches_golden(name):
    img = CASES[name]()
    golden_path = os.path.join(GOLDEN_DIR, f"{name}.png")

    if UPDATE:
        os.makedirs(GOLDEN_DIR, exist_ok=True)
        img.save(golden_path, optimize=True)
        pytest.skip(f"эталон записан: {name}")

    assert os.path.exists(golden_path), (
        f"нет эталона {name}.png — снять эталоны: GOLDEN_UPDATE=1 ./tests/run.sh"
    )

    with Image.open(golden_path) as gi:
        golden = np.array(gi.convert("RGB"))
    actual = np.array(img.convert("RGB"))

    if actual.shape != golden.shape:
        pytest.fail(
            f"{name}: размер холста {actual.shape[1]}x{actual.shape[0]} "
            f"вместо {golden.shape[1]}x{golden.shape[0]}"
        )

    if not np.array_equal(actual, golden):
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        img.save(os.path.join(OUTPUT_DIR, f"{name}.png"))
        diff = actual.astype(np.int16) - golden.astype(np.int16)
        changed = int(np.count_nonzero(diff.any(axis=2)))
        total = actual.shape[0] * actual.shape[1]
        pytest.fail(
            f"{name}: разошлось {changed} из {total} пикселей "
            f"({100.0 * changed / total:.2f}%), макс. отклонение канала "
            f"{int(np.abs(diff).max())}. Результат: tests/output/{name}.png"
        )
