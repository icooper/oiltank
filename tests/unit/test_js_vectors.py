"""Values the web UI's JavaScript must agree with the server on, worked out here in Python and
checked by ``tests/js/geometry.test.js``. Rewrite with ``UPDATE_GOLDEN=1 uv run pytest
tests/unit/test_js_vectors.py`` and review the diff."""

import json
import math
import os
from pathlib import Path
from typing import Any

import numpy as np

from oiltank.annotate import _disc_box, _View
from oiltank.calibration import Calibration
from oiltank.geometry import strip_for
from oiltank.web.kymograph import Grid
from oiltank.web.timeline import scale

GOLDEN = Path(__file__).resolve().parents[1] / "data" / "js-vectors.json"


def vectors(calibration: Calibration) -> dict[str, Any]:
    s = scale(calibration)
    grid = Grid(s["t_top"], s["t_bottom"], s["rows"])
    centres = grid.t()
    strip = strip_for(np.zeros((256, 128), np.uint8), calibration.axis)
    angles = []
    for top in ([2166.0, 1020.0], [2186.0, 1020.0], [2140.0, 1030.0]):
        cal = calibration.updated(axis=calibration.axis.updated(top=tuple(top)))
        _, _, degrees = _disc_box(_View(0, 0, 1.0, 0, 0), cal, (0.0, 0.0), aligned=False)
        angles.append(
            {"top": top, "bottom": list(cal.axis.bottom), "radians": math.radians(degrees)}
        )
    return {
        "grid": {"t_top": grid.t_top, "t_bottom": grid.t_bottom, "rows": grid.rows},
        "grid_rows": [{"row": k, "t": round(float(centres[k]), 9)} for k in (0, 37, 128, 255)],
        "strip": {
            "first": round(float(strip.t_of_row[0]), 9),
            "last": round(float(strip.t_of_row[-1]), 9),
            "rows": [
                {"row": k, "t": round(float(strip.t_of_row[k]), 9)} for k in (0, 90, 200, 255)
            ],
        },
        "disc_angles": angles,
    }


def test_js_vectors(calibration: Calibration) -> None:
    current = vectors(calibration)
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.write_text(json.dumps(current, indent=1) + "\n")
    assert json.loads(GOLDEN.read_text()) == current
