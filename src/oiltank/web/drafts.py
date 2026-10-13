"""What a draft calibration's geometry makes of some frames: what the reader finds with it, the
anchor's self-test and the calibration's warnings; and the disc-size scan. CV only: no I/O.

The anchor patch comes from the reference frame in memory, so a draft never needs saving first.
"""

import math
from collections.abc import Sequence
from typing import Any

import numpy as np

from oiltank.calibrate import check_anchor, scan_disc_sizes, warnings
from oiltank.calibration import Calibration
from oiltank.geometry import Aligner
from oiltank.model import Frame, GrayImage
from oiltank.pipeline import measure
from oiltank.readers import ReaderConfig, build_reader


def _num(value: float | None, digits: int = 4) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(float(value), digits)


def _xy(point: tuple[float, float] | None) -> list[float] | None:
    return None if point is None else [round(point[0], 2), round(point[1], 2)]


def marks(calibration: Calibration) -> list[dict[str, Any]]:
    """Where the gauge's marks are, in reference-frame pixels: F, E and the points."""
    axis = calibration.axis
    out = [
        {"label": "F", "at": _xy(axis.point(calibration.t_for(1.0)))},
        {"label": "E", "at": _xy(axis.point(calibration.t_for(0.0)))},
    ]
    for p in calibration.points:
        if 0.0 < p.fraction < 1.0:
            out.append({"label": f"{100 * p.fraction:.0f}%", "at": _xy(axis.point(p.t_on(axis)))})
    return out


def evaluate(
    calibration: Calibration, frames: Sequence[Frame], reference: GrayImage
) -> dict[str, Any]:
    """Read ``frames`` with ``calibration`` (its anchor cut from ``reference``).

    Raises ResolutionMismatch if a frame isn't the calibration's size.
    """
    calibration.use_reference(reference)
    anchor = check_anchor(reference, calibration)
    aligner = Aligner(calibration.anchor, calibration.anchor_patch)
    m = measure(frames, aligner, build_reader(ReaderConfig(), calibration), calibration)
    return {
        "warnings": warnings(calibration),
        "anchor": {
            "ok": anchor.ok,
            "contrast": _num(anchor.contrast, 1),
            "max_error_px": _num(anchor.max_error_px, 2),
            "overlap": _num(anchor.overlap, 3),
            "problems": list(anchor.problems),
        },
        "reading": {
            "ok": m.ok,
            "level": None if m.fraction is None else round(100 * m.fraction, 2),
            "t": _num(m.t, 5),
            "confidence": _num(m.confidence, 3),
            "problem": None if m.problem is None else str(m.problem),
            "position": _xy(m.position),
            "notices": [str(n) for n in m.notices],
            "frames": [
                {
                    "dx": _num(r.alignment.dx, 2),
                    "dy": _num(r.alignment.dy, 2),
                    "score": _num(r.alignment.score, 3),
                    "aligned": r.alignment.ok,
                    "t": None if r.reading is None else _num(r.reading.t, 5),
                    "confidence": None if r.reading is None else _num(r.reading.confidence, 3),
                }
                for r in m.frames
            ],
        },
        "marks": marks(calibration),
    }


def scan(calibration: Calibration, frames: Sequence[Frame], reference: GrayImage) -> dict[str, Any]:
    """Where the reader puts the disc for disc sizes around the draft's (see
    :func:`oiltank.calibrate.scan_disc_sizes`). Raises ValueError if no frame aligns."""
    calibration.use_reference(reference)
    aligner = Aligner(calibration.anchor, calibration.anchor_patch)
    result = scan_disc_sizes(frames, calibration, aligner, centre=calibration.disc)

    def grid(values: np.ndarray, digits: int) -> list[list[float | None]]:
        return [[_num(float(v), digits) for v in row] for row in values]

    i, j = result.index(calibration.disc)
    return {
        "widths": list(result.widths),
        "heights": list(result.heights),
        "t": grid(result.t, 4),
        "confidence": grid(result.confidence, 3),
        "stable": [[bool(v) for v in row] for row in result.stable],
        "best": None
        if result.best is None
        else {"width": result.best.width, "height": result.best.height},
        "consensus_t": _num(result.consensus_t, 4),
        "alternatives": [{"t": round(t, 4), "sizes": n} for t, n in result.alternatives],
        "current": [i, j],
        "at_edge": result.at_edge(calibration.disc),
    }
