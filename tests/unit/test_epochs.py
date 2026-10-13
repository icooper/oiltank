import pytest

from oiltank.calibration import Axis
from oiltank.epochs import AxisEpoch, Change, carry, successor
from oiltank.model import Point

SAMPLE = Axis(top=(2166.0, 1020.0), bottom=(2166.0, 1172.0), half_width=50.0)
"""The sample calibration's axis."""
DISC = (2168.0, 1047.5)
"""A disc position (reference px)."""


def moved(axis: Axis, dx: float, dy: float) -> Axis:
    return axis.updated(
        top=(axis.top[0] + dx, axis.top[1] + dy), bottom=(axis.bottom[0] + dx, axis.bottom[1] + dy)
    )


def carried(t: float, src: AxisEpoch, dst: AxisEpoch) -> float:
    a, b = carry(src, dst)
    return a * t + b


def test_the_first_epoch_is_the_common_scale() -> None:
    first = successor(None, "frame0.jpg", SAMPLE)
    assert first.change is Change.FIRST
    assert (first.scale, first.shift) == (1.0, 0.0)
    assert carried(0.42, first, first) == pytest.approx(0.42)


def test_nothing_changed_is_the_same_epoch() -> None:
    first = successor(None, "frame0.jpg", SAMPLE)
    rounded = SAMPLE.updated(top=(2166.0004, 1019.9996))  # as Calibration.save rounds
    assert successor(first, "frame0.jpg", rounded) is first


@pytest.mark.parametrize(
    "top, bottom",
    [
        ((2166.0, 990.0), (2166.0, 1172.0)),  # top raised
        ((2166.0, 1030.0), (2166.0, 1190.0)),  # both ends moved, unequally
        ((2160.0, 1015.0), (2170.0, 1180.0)),  # tilted a little
        ((2166.0, 1000.0), (2166.0, 1152.0)),  # translated within the same reference
    ],
)
def test_an_axis_edit_keeps_readings_where_they_were(top: Point, bottom: Point) -> None:
    first = successor(None, "frame0.jpg", SAMPLE)
    edited_axis = SAMPLE.updated(top=top, bottom=bottom)
    edited = successor(first, "frame0.jpg", edited_axis)
    assert edited.change is Change.AXIS_EDIT
    # An edit fits the axis to the disc's travel, so the disc runs along the edited axis.
    for t in (-0.1, 0.3, 0.8, 1.05):
        disc = edited_axis.point(t)
        assert carried(SAMPLE.t_of(disc), first, edited) == pytest.approx(t)
        assert carried(t, edited, first) == pytest.approx(SAMPLE.t_of(disc))


def test_an_edit_along_the_same_line_maps_off_axis_positions_exactly() -> None:
    first = successor(None, "frame0.jpg", SAMPLE)
    longer = SAMPLE.updated(top=(2166.0, 990.0), bottom=(2166.0, 1190.0))
    edited = successor(first, "frame0.jpg", longer)
    assert carried(SAMPLE.t_of(DISC), first, edited) == pytest.approx(longer.t_of(DISC))


def test_a_rebase_leaves_t_alone() -> None:
    first = successor(None, "frame0.jpg", SAMPLE)
    rebased = successor(first, "reference-20261101-120000.jpg", moved(SAMPLE, 12.5, -7.25))
    assert rebased.change is Change.REBASE
    assert carried(0.8, first, rebased) == pytest.approx(0.8)


def test_a_new_reference_with_a_different_axis_carries_t_unchanged() -> None:
    first = successor(None, "frame0.jpg", SAMPLE)
    other = successor(first, "reference-new.jpg", SAMPLE.updated(top=(2166.0, 900.0)))
    assert other.change is Change.NEW_REFERENCE
    assert carried(0.8, first, other) == pytest.approx(0.8)


def test_turning_the_axis_too_far_is_not_an_edit() -> None:
    first = successor(None, "frame0.jpg", SAMPLE)
    sideways = successor(first, "frame0.jpg", SAMPLE.updated(top=(2300.0, 1100.0)))
    assert sideways.change is Change.NEW_REFERENCE
    assert carried(0.8, first, sideways) == pytest.approx(0.8)


def test_edits_and_rebases_compose() -> None:
    a = successor(None, "frame0.jpg", SAMPLE)
    b_axis = SAMPLE.updated(top=(2166.0, 995.0))
    b = successor(a, "frame0.jpg", b_axis)
    shift = (8.0, 5.0)
    c_axis = moved(b_axis, *shift)
    c = successor(b, "reference-c.jpg", c_axis)
    d_axis = c_axis.updated(bottom=(c_axis.bottom[0], c_axis.bottom[1] + 20))
    d = successor(c, "reference-c.jpg", d_axis)
    assert [b.change, c.change, d.change] == [Change.AXIS_EDIT, Change.REBASE, Change.AXIS_EDIT]
    # The disc seen in epoch a is, after the camera moved, at DISC + shift in d's reference.
    in_d = d_axis.t_of((DISC[0] + shift[0], DISC[1] + shift[1]))
    assert carried(SAMPLE.t_of(DISC), a, d) == pytest.approx(in_d, abs=1e-3)
