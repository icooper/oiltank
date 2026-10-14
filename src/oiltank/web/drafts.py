"""What a draft calibration's geometry makes of some frames: what the reader finds with it, the
anchor's self-test and the calibration's warnings; and the disc-size scan. CV only: no I/O.

The anchor patch comes from the reference frame in memory, so a draft never needs saving first.
"""

import base64
import math
from collections.abc import Sequence
from typing import Any

import cv2
import numpy as np

from oiltank.calibrate import check_anchor, scan_disc_sizes, warnings
from oiltank.calibration import IMPLICIT_E, IMPLICIT_F, Calibration
from oiltank.geometry import Aligner
from oiltank.model import Frame, FrameResult, GrayImage
from oiltank.pipeline import measure
from oiltank.readers import ReaderConfig, build_reader


def _num(value: float | None, digits: int = 4) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(float(value), digits)


def _xy(point: tuple[float, float] | None) -> list[float] | None:
    return None if point is None else [round(point[0], 2), round(point[1], 2)]


def _label(fraction: float) -> str:
    return "E" if fraction == 0.0 else "F" if fraction == 1.0 else f"{100 * fraction:.0f}%"


def _end(calibration: Calibration, fraction: float) -> dict[str, Any]:
    """Where F (``fraction`` 1) or E (0) is, and what puts it there (``source``): a point at that
    reading; the end of travel, which stands in until there are two points; or the scale through
    the two points nearest that end, extended (``through`` names them)."""
    t = calibration.t_for(fraction)
    mark: dict[str, Any] = {
        "label": _label(fraction),
        "at": _xy(calibration.axis.point(t)),
        "t": _num(t),
    }
    scale = calibration.scale_points()
    if any(p.fraction == fraction for p in calibration.points):
        mark["source"] = "point"
    elif (IMPLICIT_F if fraction == 1.0 else IMPLICIT_E) in scale:
        mark["source"] = "travel"
    else:
        nearest = scale[-2:] if fraction == 1.0 else scale[:2]
        mark |= {"source": "scale", "through": [_label(f) for _, f in nearest]}
    return mark


def marks(calibration: Calibration) -> list[dict[str, Any]]:
    """Where the gauge's marks are, in reference-frame pixels and along the axis (``t``): F and E
    (see :func:`_end`), then the points."""
    axis = calibration.axis
    out = [_end(calibration, 1.0), _end(calibration, 0.0)]
    for p in calibration.points:
        if 0.0 < p.fraction < 1.0:
            t = p.t_on(axis)
            out.append({"label": _label(p.fraction), "at": _xy(axis.point(t)), "t": _num(t)})
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
        "strip": None if not m.frames else strip_view(m.frames[0], calibration),
    }


def strip_view(result: FrameResult, calibration: Calibration) -> dict[str, Any] | None:
    """The strip the reader got from a frame (as a PNG data URL), the reader's score for every
    row, the row it picked, and the rows of the gauge's marks: what the inspector shows."""
    strip = result.strip
    if strip is None:
        return None
    ok, png = cv2.imencode(".png", strip.pixels)
    if not ok:
        return None
    first, last = float(strip.t_of_row[0]), float(strip.t_of_row[-1])

    def row(t: float) -> float:
        return round((t - first) / (last - first) * (strip.rows - 1), 2)

    axis = calibration.axis
    ticks = [
        {"label": "F", "row": row(calibration.t_for(1.0))},
        {"label": "E", "row": row(calibration.t_for(0.0))},
    ]
    for p in calibration.points:
        if 0.0 < p.fraction < 1.0:
            ticks.append({"label": f"{100 * p.fraction:.0f}%", "row": row(p.t_on(axis))})
    reading = result.reading
    return {
        "png": "data:image/png;base64," + base64.b64encode(png.tobytes()).decode(),
        "rows": strip.rows,
        "t_first": round(first, 5),
        "t_last": round(last, 5),
        "disc_row": None if reading is None else _num(reading.row, 2),
        "scores": None if reading is None else [_num(float(v), 4) for v in reading.scores],
        "ticks": ticks,
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
