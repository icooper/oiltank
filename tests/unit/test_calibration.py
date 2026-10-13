from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from oiltank.calibration import (
    Calibration,
    CalibrationError,
    CalibrationPoint,
    ResolutionMismatch,
)


def _data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "reference_image": "ref.png",
        "frame_size": [400, 300],
        "axis": {"top": [200, 50], "bottom": [200, 150], "half_width": 20},
        "disc": {"width": 30, "height": 15},
        "anchor": {"rect": [150, 200, 100, 50]},
    }
    data.update(overrides)
    return data


def test_default_points_are_identity_and_clamped() -> None:
    cal = Calibration.model_validate(_data())
    assert cal.fraction(0.0) == 0.0
    assert cal.fraction(0.5) == pytest.approx(0.5)
    assert cal.fraction(1.0) == 1.0
    assert cal.fraction(-0.2) == 0.0
    assert cal.fraction(1.3) == 1.0


def test_extra_points_make_mapping_piecewise_linear() -> None:
    points = [
        {"t": 0.0, "fraction": 0.0},
        {"t": 0.5, "fraction": 0.25},
        {"t": 1.0, "fraction": 1.0},
    ]
    cal = Calibration.model_validate(_data(points=points))
    assert cal.fraction(0.25) == pytest.approx(0.125)
    assert cal.fraction(0.75) == pytest.approx(0.625)


def test_end_segments_extend_past_outermost_points() -> None:
    # One checked reading near full plus the default E point: F is extrapolated, not assumed.
    points = [{"t": 0.0, "fraction": 0.0}, {"t": 0.8, "fraction": 0.96}]
    cal = Calibration.model_validate(_data(points=points))
    assert cal.fraction(0.4) == pytest.approx(0.48)
    assert cal.fraction(0.82) == pytest.approx(0.984)
    assert cal.fraction(0.9) == 1.0  # clamped
    assert cal.t_for(1.0) == pytest.approx(0.8 / 0.96)
    assert cal.t_for(0.0) == pytest.approx(0.0)


def test_t_for_inverts_fraction() -> None:
    points = [
        {"t": 0.1, "fraction": 0.0},
        {"t": 0.5, "fraction": 0.25},
        {"t": 0.9, "fraction": 1.0},
    ]
    cal = Calibration.model_validate(_data(points=points))
    for fraction in (0.0, 0.1, 0.25, 0.6, 1.0):
        assert cal.fraction(cal.t_for(fraction)) == pytest.approx(fraction)
    assert cal.t_for(0.25) == pytest.approx(0.5)


def test_t_for_may_fall_outside_axis() -> None:
    points = [{"t": 0.2, "fraction": 0.1}, {"t": 0.6, "fraction": 0.5}]
    cal = Calibration.model_validate(_data(points=points))
    assert cal.t_for(0.0) == pytest.approx(0.1)
    assert cal.t_for(1.0) == pytest.approx(1.1)


def test_axis_point_interpolates_between_endpoints() -> None:
    axis = Calibration.model_validate(_data()).axis  # top (200, 50), bottom (200, 150)
    assert axis.point(1.0) == pytest.approx((200, 50))
    assert axis.point(0.0) == pytest.approx((200, 150))
    assert axis.point(0.25) == pytest.approx((200, 125))


def test_axis_t_of_projects_onto_axis() -> None:
    axis = Calibration.model_validate(_data()).axis  # top (200, 50), bottom (200, 150)
    assert axis.t_of((200, 100)) == pytest.approx(0.5)
    assert axis.t_of((215, 100)) == pytest.approx(0.5)  # off-axis: projected
    assert axis.t_of(axis.point(0.3)) == pytest.approx(0.3)


def test_at_points_stay_on_the_disc_when_the_axis_moves() -> None:
    points = [{"t": 0.0, "fraction": 0.0}, {"at": [201, 70], "fraction": 0.9}]
    a = Calibration.model_validate(_data(points=points))
    moved_axis = {"top": [200, 40], "bottom": [200, 170], "half_width": 20}
    b = Calibration.model_validate(_data(points=points, axis=moved_axis))
    for cal in (a, b):
        assert cal.fraction(cal.axis.t_of((200, 70))) == pytest.approx(0.9)
    assert a.points[1].t_on(a.axis) == pytest.approx(0.8)
    assert b.points[1].t_on(b.axis) == pytest.approx(100 / 130)


@pytest.mark.parametrize(
    "point",
    [{"fraction": 0.5}, {"t": 0.5, "at": [200, 100], "fraction": 0.5}],
)
def test_point_needs_exactly_one_position(point: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="exactly one of t or at"):
        Calibration.model_validate(_data(points=[{"t": 0.0, "fraction": 0.0}, point]))


def test_at_points_are_checked_for_monotonicity() -> None:
    # at y=140 is t=0.1, below the t=0.5 point, yet its fraction is higher.
    points = [{"t": 0.5, "fraction": 0.4}, {"at": [200, 140], "fraction": 0.6}]
    with pytest.raises(ValidationError, match="increase"):
        Calibration.model_validate(_data(points=points))


def test_points_may_be_given_in_any_order() -> None:
    points = [{"t": 1.0, "fraction": 1.0}, {"t": 0.0, "fraction": 0.0}]
    assert Calibration.model_validate(_data(points=points)).fraction(0.4) == pytest.approx(0.4)


@pytest.mark.parametrize(
    "points",
    [
        [{"t": 0.0, "fraction": 0.5}, {"t": 1.0, "fraction": 0.4}],
        [{"t": 0.3, "fraction": 0.1}, {"t": 0.3, "fraction": 0.2}],
        [{"t": -0.1, "fraction": 0.9}],  # pairs with E at t = 0, but lies below it
        [{"t": 1.2, "fraction": 0.2}],  # pairs with F at t = 1, but lies above it
    ],
)
def test_invalid_points_rejected(points: list[dict[str, float]]) -> None:
    with pytest.raises(ValidationError, match="increase"):
        Calibration.model_validate(_data(points=points))


def test_one_high_point_pairs_with_e_at_the_bottom() -> None:
    cal = Calibration.model_validate(_data(points=[{"t": 0.8, "fraction": 0.96}]))
    assert cal.scale_points() == [(0.0, 0.0), (0.8, 0.96)]
    assert cal.fraction(0.4) == pytest.approx(0.48)
    assert cal.t_for(1.0) == pytest.approx(0.8 / 0.96)


def test_one_low_point_pairs_with_f_at_the_top() -> None:
    cal = Calibration.model_validate(_data(points=[{"t": 0.3, "fraction": 0.2}]))
    assert cal.scale_points() == [(0.3, 0.2), (1.0, 1.0)]
    assert cal.fraction(0.65) == pytest.approx(0.6)
    assert cal.t_for(0.0) == pytest.approx(0.3 - 0.2 * 0.7 / 0.8)


def test_two_points_need_no_defaults() -> None:
    points = [{"t": 0.2, "fraction": 0.25}, {"t": 0.6, "fraction": 0.75}]
    cal = Calibration.model_validate(_data(points=points))
    assert cal.scale_points() == [(0.2, 0.25), (0.6, 0.75)]


def test_anchor_outside_frame_rejected() -> None:
    with pytest.raises(ValidationError, match="anchor rect"):
        Calibration.model_validate(_data(anchor={"rect": [350, 250, 100, 100]}))


def test_degenerate_axis_rejected() -> None:
    with pytest.raises(ValidationError, match="axis"):
        Calibration.model_validate(
            _data(axis={"top": [200, 50], "bottom": [201, 51], "half_width": 20})
        )


def test_unknown_keys_rejected() -> None:
    with pytest.raises(ValidationError):
        Calibration.model_validate(_data(colour="red"))


def test_frame_size_check() -> None:
    cal = Calibration.model_validate(_data())
    cal.check_frame_size((400, 300))
    with pytest.raises(ResolutionMismatch, match="640x360"):
        cal.check_frame_size((640, 360))


def test_save_load_roundtrip_resolves_reference_relative_to_file(tmp_path: Path) -> None:
    points = [
        {"t": 0.0, "fraction": 0.0},
        {"at": [200.5, 60.0], "fraction": 0.95},
        {"t": 0.95, "fraction": 1.0, "note": "full"},
    ]
    cal = Calibration.model_validate(_data(points=points))
    path = tmp_path / "sub" / "calibration.yaml"
    path.parent.mkdir()
    cal.save(path)
    assert "null" not in path.read_text()  # unused position keys are left out
    loaded = Calibration.load(path)
    assert loaded.model_dump() == cal.model_dump()
    assert loaded.points[1] == CalibrationPoint(at=(200.5, 60.0), fraction=0.95)
    assert loaded.points[2] == CalibrationPoint(t=0.95, fraction=1.0, note="full")
    assert loaded.reference_path == path.parent / "ref.png"


def test_sample_calibration_loads(calibration: Calibration) -> None:
    assert calibration.frame_size == (3840, 2160)
    assert calibration.anchor_patch.shape == (165, 290)


def test_sample_calibration_scale(calibration: Calibration) -> None:
    # The owner put the tank at ~98% with the disc centred at about (2168.7, 1047.3).
    disc_t = calibration.axis.t_of((2168.7, 1047.3))
    assert calibration.fraction(disc_t) == pytest.approx(0.98, abs=0.005)
    # E is the bottom of travel; F is a little above the disc and below the dome cap (y ≈ 1005).
    assert calibration.t_for(0.0) == pytest.approx(0.0)
    f_y = calibration.axis.point(calibration.t_for(1.0))[1]
    assert 1005 < f_y < 1047.3


def test_save_format_header_and_backup(tmp_path: Path) -> None:
    path = tmp_path / "calibration.yaml"
    cal = Calibration.build(tmp_path, **_data())
    cal.save(path)
    first = path.read_text()
    assert first.startswith("# oiltank calibration.")
    assert "frame_size: [400, 300]" in first  # short lists on one line
    assert "# No points yet" in first
    assert not (tmp_path / "calibration.yaml.bak").exists()

    # A hand-written header survives a rewrite, and the previous version is kept.
    path.write_text("# my gauge\n# second line\n" + first.split("\n", 2)[2])
    edited = path.read_text()
    point = CalibrationPoint(at=(200.04, 70.0), fraction=0.9, note="checked")
    cal.updated(points=[point]).save(path)
    text = path.read_text()
    assert text.startswith("# my gauge\n# second line\nversion: 1\n")
    assert "  - {at: [200.04, 70.0], fraction: 0.9, note: checked}" in text
    assert "# One point, so the scale also takes E" in text
    assert (tmp_path / "calibration.yaml.bak").read_text() == edited
    assert not list(tmp_path.glob(".*.tmp"))
    assert Calibration.load(path).points == [point]


def test_updated_validates_and_keeps_reference_dir(tmp_path: Path) -> None:
    cal = Calibration.build(tmp_path, **_data())
    moved = cal.updated(disc={"width": 40, "height": 20})
    assert moved.disc.width == 40
    assert moved.reference_path == tmp_path / "ref.png"
    with pytest.raises(ValidationError, match="axis"):
        cal.updated(axis=cal.axis.updated(bottom=(200, 52)))


def test_load_rejects_non_mapping(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text("- just\n- a list\n")
    with pytest.raises(CalibrationError, match="mapping"):
        Calibration.load(path)
    path.write_text("version: [1\n")
    with pytest.raises(CalibrationError, match="not valid YAML"):
        Calibration.load(path)
