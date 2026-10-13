"""The calibration tools (``oiltank.calibrate``) on the real sample frames."""

import numpy as np
import pytest
from pydantic import ValidationError

from oiltank.calibrate import (
    DiscScan,
    check_anchor,
    default_anchor,
    default_disc,
    default_half_width,
    find_shift,
    scan_disc_sizes,
    shifted,
    travel_envelope,
    warnings,
    with_point,
    without_point,
)
from oiltank.calibration import Axis, Calibration, CalibrationPoint, Disc
from oiltank.geometry import Aligner
from oiltank.model import Frame
from oiltank.pipeline import measure
from oiltank.readers import GaugeReader
from oiltank.readers.classical import ClassicalReader
from tests.cv import synth

DISC_Y = 1047.7  # where the disc is in the samples, day and night


def _y(calibration: Calibration, t: float | None) -> float:
    assert t is not None
    return calibration.axis.point(t)[1]


# ---- starting values ---------------------------------------------------------------------------


def test_default_anchor_sits_below_the_disc_travel(calibration: Calibration) -> None:
    disc = default_disc(calibration.axis)
    anchor = default_anchor(calibration.axis, disc, calibration.frame_size)
    x, y, w, h = anchor.rect
    _, _, _, envelope_bottom = travel_envelope(calibration.axis, disc)
    assert y > envelope_bottom
    assert x >= 0 and x + w <= 3840 and y + h <= 2160
    assert anchor.search == round(0.3 * calibration.axis.length)


def test_default_anchor_needs_room() -> None:
    axis = Axis(top=(200, 150), bottom=(200, 290), half_width=40)
    with pytest.raises(ValueError, match="no room"):
        default_anchor(axis, default_disc(axis), (400, 300))


def test_default_half_width_clears_the_disc() -> None:
    axis = Axis(top=(200, 50), bottom=(200, 150), half_width=1)
    assert default_half_width(axis, Disc(width=40, height=20)) == 33.0
    assert default_half_width(axis, Disc(width=80, height=20)) == 60.0


# ---- anchor check ------------------------------------------------------------------------------


def test_sample_anchor_passes(sample_frames: list[Frame], calibration: Calibration) -> None:
    check = check_anchor(sample_frames[0].gray, calibration)
    assert check.ok, check.problems
    assert check.max_error_px < 0.2


def test_anchor_on_the_gauge_is_flagged(
    sample_frames: list[Frame], calibration: Calibration
) -> None:
    on_gauge = calibration.updated(anchor=calibration.anchor.updated(rect=(2100, 1000, 140, 120)))
    check = check_anchor(sample_frames[0].gray, on_gauge)
    assert any("overlaps" in p for p in check.problems)


def test_featureless_anchor_is_flagged(calibration: Calibration) -> None:
    flat = np.full((2160, 3840), 90, np.uint8)
    check = check_anchor(flat, calibration)
    assert any("featureless" in p for p in check.problems)
    assert any("self-test" in p for p in check.problems)


# ---- disc size scan ----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def day_scan(day_frames: list[Frame], aligner: Aligner, calibration: Calibration) -> DiscScan:
    return scan_disc_sizes(day_frames, calibration, aligner)


def test_scan_finds_the_disc(day_scan: DiscScan, calibration: Calibration) -> None:
    assert day_scan.best is not None
    assert _y(calibration, day_scan.consensus_t) == pytest.approx(DISC_Y, abs=2.0)
    assert day_scan.holds(day_scan.best) is not None
    assert not day_scan.at_edge(day_scan.best)
    # The sample's own size reads consistently too.
    assert day_scan.holds(calibration.disc) is not None


def test_scan_reports_the_feature_too_small_a_disc_locks_onto(
    day_scan: DiscScan, calibration: Calibration
) -> None:
    # Undersized templates find something about 12 px above the disc (see the M2 notes).
    ys = [_y(calibration, t) for t, _ in day_scan.alternatives]
    assert any(abs(y - 1035) < 4 for y in ys), ys


def test_scan_is_repeatable(
    day_scan: DiscScan, day_frames: list[Frame], aligner: Aligner, calibration: Calibration
) -> None:
    assert day_scan.best is not None
    again = scan_disc_sizes(day_frames, calibration, aligner, centre=day_scan.best)
    assert again.best is not None
    assert again.holds(day_scan.best) is not None
    assert _y(calibration, again.consensus_t) == pytest.approx(
        _y(calibration, day_scan.consensus_t), abs=1.0
    )


def test_scan_from_the_generic_guess_and_at_night(
    night_frames: list[Frame], aligner: Aligner, calibration: Calibration
) -> None:
    scan = scan_disc_sizes(
        night_frames, calibration, aligner, centre=default_disc(calibration.axis)
    )
    assert scan.best is not None
    assert _y(calibration, scan.consensus_t) == pytest.approx(DISC_Y, abs=2.0)
    reader = ClassicalReader(scan.best)
    m = measure(night_frames, aligner, reader, calibration)
    assert m.position is not None
    assert m.position[1] == pytest.approx(DISC_Y, abs=2.0)


def test_scan_needs_an_aligned_frame(aligner: Aligner, calibration: Calibration) -> None:
    from datetime import UTC, datetime

    blank = Frame(gray=np.zeros((2160, 3840), np.uint8), ts=datetime.now(UTC), source="blank")
    with pytest.raises(ValueError, match="aligned"):
        scan_disc_sizes([blank], calibration, aligner)


def test_holds_and_at_edge() -> None:
    stable = np.zeros((5, 5), bool)
    stable[1:4, 1:5] = True
    scan = DiscScan(
        widths=(10, 20, 30, 40, 50),
        heights=(1, 2, 3, 4, 5),
        t=np.zeros((5, 5)),
        confidence=np.ones((5, 5)),
        stable=stable,
        best=Disc(width=30, height=3),
        consensus_t=0.0,
        alternatives=(),
    )
    assert scan.holds(Disc(width=30, height=3)) == ((20, 40), (2, 5))
    assert not scan.at_edge(Disc(width=30, height=3))
    assert scan.at_edge(Disc(width=20, height=3))  # width 10 at this height reads elsewhere
    assert not scan.at_edge(Disc(width=30, height=5))  # the grid edge isn't a breakdown
    assert scan.holds(Disc(width=10, height=1)) is None


# ---- rebase ------------------------------------------------------------------------------------


def test_find_shift_and_shifted(
    sample_frames: list[Frame], reader: GaugeReader, calibration: Calibration
) -> None:
    frame = sample_frames[0]
    bumped = synth.with_gray(frame, synth.shift(frame.gray, 61.5, -40.25))
    shift = find_shift(calibration, bumped.gray, search=200)
    assert shift.ok
    assert (shift.dx, shift.dy) == pytest.approx((61.5, -40.25), abs=0.15)

    with_at = calibration.updated(points=[CalibrationPoint(at=(2168.0, 1047.7), fraction=0.98)])
    moved = shifted(with_at, shift.dx, shift.dy, with_at.reference_image)
    assert moved.axis.top == pytest.approx(
        (with_at.axis.top[0] + shift.dx, with_at.axis.top[1] + shift.dy)
    )
    x, y, w, h = moved.anchor.rect
    assert (x, y) == (round(1950 + shift.dx), round(1185 + shift.dy))
    assert moved.points[0].at == pytest.approx((2168.0 + shift.dx, 1047.7 + shift.dy))
    # Reading the bumped frame against the moved calibration needs no further alignment.
    patch_aligner = Aligner(moved.anchor, bumped.gray[y : y + h, x : x + w].copy())
    m = measure([bumped], patch_aligner, reader, moved)
    assert m.fraction == pytest.approx(0.98, abs=0.01)
    assert abs(m.frames[0].alignment.dx) < 1 and abs(m.frames[0].alignment.dy) < 1


def test_shifted_rejects_an_anchor_leaving_the_frame(calibration: Calibration) -> None:
    with pytest.raises(ValidationError, match="anchor rect"):
        shifted(calibration, 0, 900, calibration.reference_image)


# ---- points and warnings -----------------------------------------------------------------------


def test_with_point_replaces_the_same_reading(calibration: Calibration) -> None:
    first = CalibrationPoint(at=(2168, 1100), fraction=0.5)
    cal, replaced = with_point(calibration.updated(points=[]), first)
    assert replaced == []
    second = CalibrationPoint(at=(2168, 1102), fraction=0.502)
    cal, replaced = with_point(cal, second)
    assert replaced == [first]
    assert cal.points == [second]
    cal, removed = without_point(cal, 0.5)
    assert removed == [second]
    assert cal.points == []


def test_with_point_rejects_a_point_out_of_order(calibration: Calibration) -> None:
    cal = calibration.updated(points=[CalibrationPoint(at=(2168, 1047.7), fraction=0.98)])
    with pytest.raises(ValidationError, match="increase"):
        with_point(cal, CalibrationPoint(at=(2168, 1030), fraction=0.5))


def test_warnings(calibration: Calibration) -> None:
    assert warnings(calibration) == []
    steep = [CalibrationPoint(t=0.1, fraction=0.1), CalibrationPoint(t=0.3, fraction=0.2)]
    beyond = calibration.updated(points=steep)  # F extrapolates to t = 1.9
    assert any("F (100%) is outside" in w for w in warnings(beyond))
    narrow = calibration.updated(axis=calibration.axis.updated(half_width=30))
    assert any("barely wider" in w for w in warnings(narrow))
    tiny = calibration.updated(
        axis=calibration.axis.updated(top=(2166, 1100), bottom=(2166, 1130), half_width=40),
        points=[],
    )
    assert any("spans only" in w for w in warnings(tiny))
