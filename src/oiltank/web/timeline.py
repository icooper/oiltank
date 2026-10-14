"""The History view's data: readings over a time range, with their levels worked out by the
current calibration, the filtered level the service would have published, and refills. Pure: no
I/O.

Levels are recomputed from each reading's ``t`` (carried onto the current axis), and the filter is
replayed from before the range, so the series is what the service would show now, after any
recalibration.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import numpy as np

from oiltank.calibration import Calibration
from oiltank.filter import Fill, FilterParams, LevelFilter, Observation
from oiltank.geometry import STRIP_ROWS
from oiltank.history import StoredReading


def _pct(fraction: float | None) -> float | None:
    return None if fraction is None else round(100 * fraction, 2)


def _seconds(ts: datetime) -> float:
    return round(ts.timestamp(), 3)


def build_series(
    readings: Sequence[StoredReading],
    start: datetime,
    calibration: Calibration,
    params: FilterParams,
    capacity: float,
    reserve: float = 0.0,
    last_fill: Fill | None = None,
) -> dict[str, Any]:
    """The readings from ``start`` on, as columns (times in Unix seconds, levels in percent).

    ``readings`` should begin before ``start`` (by the filter's usage window) so the filter has
    warmed up, and have ``t`` carried onto the current axis.
    """
    t = np.array(
        [np.nan if r.t is None or r.fraction is None else r.t for r in readings], dtype=np.float64
    )
    fractions = calibration.fractions(t) if len(t) else t
    lf = LevelFilter(params, capacity, reserve, last_fill)
    cols: dict[str, list[Any]] = {
        k: [] for k in ("ts", "level", "t", "confidence", "problem", "accepted", "smoothed")
    }
    fills = []
    for r, f in zip(readings, fractions, strict=True):
        if r.fraction is None:
            fraction = None
        elif np.isnan(f):  # a reading with no position (older data): as stored
            fraction = r.fraction
        else:
            fraction = float(f)
        step = lf.advance(Observation(r.ts, fraction, r.confidence, r.t, r.problem))
        if r.ts < start:
            continue
        cols["ts"].append(_seconds(r.ts))
        cols["level"].append(_pct(fraction))
        cols["t"].append(None if r.t is None else round(r.t, 5))
        cols["confidence"].append(round(r.confidence, 3))
        cols["problem"].append(None if r.problem is None else str(r.problem))
        cols["accepted"].append(step.accepted)
        cols["smoothed"].append(_pct(lf.level))
        if step.fill is not None:
            fill = step.fill
            fills.append(
                {
                    "ts": _seconds(fill.ts),
                    "before": _pct(fill.before),
                    "after": _pct(fill.after),
                    "volume": round(fill.volume(capacity), 1),
                }
            )
    return {"readings": cols, "fills": fills}


def scale(calibration: Calibration) -> dict[str, Any]:
    """Where the gauge's marks are, as ``t`` on the current axis, and the ``t`` range of the
    rows of a strip (top row first), which the gauge-over-time image shares."""
    margin = calibration.axis.margin
    ticks = [
        {"label": "F", "t": round(calibration.t_for(1.0), 5)},
        {"label": "E", "t": round(calibration.t_for(0.0), 5)},
    ]
    for p in calibration.points:
        if 0.0 < p.fraction < 1.0:
            ticks.append(
                {"label": f"{100 * p.fraction:.0f}%", "t": round(p.t_on(calibration.axis), 5)}
            )
    return {"t_top": 1.0 + margin, "t_bottom": -margin, "rows": STRIP_ROWS, "ticks": ticks}
