"""The History view's series: recalibrated levels, the replayed filter, and refills."""

from datetime import UTC, datetime, timedelta

import pytest

from oiltank.calibration import Calibration, CalibrationPoint
from oiltank.filter import FilterParams
from oiltank.history import StoredReading
from oiltank.model import Problem
from oiltank.web.timeline import build_series, scale

T0 = datetime(2026, 10, 13, tzinfo=UTC)


def reading(
    hours: float,
    t: float | None,
    fraction: float | None = 0.5,
    confidence: float = 0.95,
    problem: Problem | None = None,
) -> StoredReading:
    return StoredReading(
        T0 + timedelta(hours=hours), fraction, confidence, t, problem, True, 5, None
    )


def test_levels_come_from_t_and_the_current_calibration(calibration: Calibration) -> None:
    owner_t = calibration.t_for(0.98)
    out = build_series([reading(0, owner_t, fraction=0.1)], T0, calibration, FilterParams(), 250)
    assert out["readings"]["level"] == [pytest.approx(98, abs=0.01)]
    rescaled = calibration.updated(
        points=[CalibrationPoint(at=calibration.points[0].at, fraction=0.9)]
    )
    again = build_series([reading(0, owner_t)], T0, rescaled, FilterParams(), 250)
    assert again["readings"]["level"] == [pytest.approx(90, abs=0.01)]


def test_columns_smoothing_and_rejections(calibration: Calibration) -> None:
    t = calibration.t_for(0.6)
    misread = calibration.t_for(0.2)
    rows = [
        reading(0, t),
        reading(1, t),
        reading(2, misread),  # one bad read doesn't move the smoothed level
        reading(3, None, fraction=None, confidence=0.0, problem=Problem.CAMERA_UNREACHABLE),
        reading(4, t, confidence=0.3),  # below the confidence gate
    ]
    cols = build_series(rows, T0, calibration, FilterParams(), 250)["readings"]
    assert cols["ts"] == [(T0 + timedelta(hours=h)).timestamp() for h in range(5)]
    assert cols["level"][:3] == pytest.approx([60, 60, 20], abs=0.01)
    assert cols["level"][3] is None
    assert cols["smoothed"] == pytest.approx([60, 60, 60, 60, 60], abs=0.01)
    assert cols["accepted"] == [True, True, True, False, False]
    assert cols["problem"] == [None, None, None, "camera_unreachable", None]


def test_warm_up_readings_are_replayed_but_not_returned(calibration: Calibration) -> None:
    low, high = calibration.t_for(0.3), calibration.t_for(0.9)
    rows = [reading(h, low) for h in range(-30, 0)] + [reading(0, high), reading(1, high)]
    out = build_series(rows, T0, calibration, FilterParams(), 250)
    assert len(out["readings"]["ts"]) == 2
    [fill] = out["fills"]  # the jump, confirmed by the next reading
    assert fill["ts"] == T0.timestamp()
    assert fill["before"] == pytest.approx(30, abs=0.01)
    assert fill["after"] == pytest.approx(90, abs=0.01)
    assert fill["volume"] == pytest.approx(150, abs=0.1)


def test_readings_without_a_position_keep_their_stored_level(calibration: Calibration) -> None:
    out = build_series([reading(0, None, fraction=0.42)], T0, calibration, FilterParams(), 250)
    assert out["readings"]["level"] == [42.0]


def test_scale(calibration: Calibration) -> None:
    s = scale(calibration)
    margin = calibration.axis.margin
    assert (s["t_top"], s["t_bottom"], s["rows"]) == (1 + margin, -margin, 256)
    ticks = {tick["label"]: tick["t"] for tick in s["ticks"]}
    assert set(ticks) == {"F", "E", "98%"}
    assert ticks["E"] == pytest.approx(0.0)
    assert ticks["98%"] == pytest.approx(calibration.t_for(0.98), abs=1e-4)
    assert ticks["F"] > ticks["98%"] > ticks["E"]
