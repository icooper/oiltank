import cv2
import numpy as np
import pytest

from oiltank.calibration import Anchor, Axis
from oiltank.geometry import Aligner, rectify
from oiltank.model import Alignment, GaugeStrip


def _centroid(strip: GaugeStrip) -> tuple[float, float]:
    """(row, col) centre of mass of the bright pixels."""
    w = strip.pixels.astype(np.float64)
    w[w < w.max() / 2] = 0
    rows, cols = np.indices(w.shape)
    return float((rows * w).sum() / w.sum()), float((cols * w).sum() / w.sum())


def _dot_image(x: int, y: int) -> np.ndarray:
    img = np.zeros((300, 400), np.uint8)
    img[y - 1 : y + 2, x - 1 : x + 2] = 255
    return img


AXIS = Axis(top=(200, 100), bottom=(200, 200), half_width=20, margin=0.25)


def test_t_of_row_spans_axis() -> None:
    strip = rectify(np.zeros((300, 400), np.uint8), AXIS)
    assert strip.pixels.shape == (256, 128)
    # 25% margin each side: the strip covers t from 1.25 (top row) to -0.25 (bottom row).
    assert strip.t_of_row[0] == pytest.approx(1.25, abs=0.01)
    assert strip.t_of_row[-1] == pytest.approx(-0.25, abs=0.01)
    assert np.all(np.diff(strip.t_of_row) < 0)


@pytest.mark.parametrize(("y", "t"), [(100, 1.0), (150, 0.5), (200, 0.0)])
def test_dot_on_axis_lands_at_expected_t(y: int, t: float) -> None:
    strip = rectify(_dot_image(200, y), AXIS)
    row, col = _centroid(strip)
    assert strip.t_at(row) == pytest.approx(t, abs=0.005)
    assert col == pytest.approx((strip.cols - 1) / 2, abs=0.5)
    x, fy = strip.frame_point(col, row)
    assert x == pytest.approx(200, abs=0.25)
    assert fy == pytest.approx(y, abs=0.25)


def test_alignment_offset_shifts_sampling() -> None:
    strip = rectify(_dot_image(207, 155), AXIS, Alignment(dx=7, dy=5, score=1, ok=True))
    row, _ = _centroid(strip)
    assert strip.t_at(row) == pytest.approx(0.5, abs=0.005)


def test_tilted_axis() -> None:
    axis = Axis(top=(150, 100), bottom=(250, 200), half_width=20)
    strip = rectify(_dot_image(200, 150), axis)
    row, col = _centroid(strip)
    assert strip.t_at(row) == pytest.approx(0.5, abs=0.005)
    assert col == pytest.approx((strip.cols - 1) / 2, abs=0.5)


def _texture(seed: int = 0) -> np.ndarray:
    """Smoothed random texture: image-like, with a well-defined correlation peak."""
    rng = np.random.default_rng(seed)
    noise = rng.integers(0, 255, (300, 400), dtype=np.uint8)
    return np.asarray(cv2.GaussianBlur(noise, (0, 0), 1.5), dtype=np.uint8)


def _shift(img: np.ndarray, dx: int, dy: int) -> np.ndarray:
    return np.roll(np.roll(img, dy, axis=0), dx, axis=1)


@pytest.mark.parametrize(("dx", "dy"), [(0, 0), (5, -3), (-12, 9)])
def test_aligner_recovers_shift(dx: int, dy: int) -> None:
    ref = _texture()
    anchor = Anchor(rect=(150, 120, 60, 40), search=20)
    aligner = Aligner(anchor, ref[120:160, 150:210])
    a = aligner.locate(_shift(ref, dx, dy))
    assert a.ok
    assert a.dx == pytest.approx(dx, abs=0.2)
    assert a.dy == pytest.approx(dy, abs=0.2)


def test_aligner_rejects_shift_beyond_search() -> None:
    ref = _texture()
    aligner = Aligner(Anchor(rect=(150, 120, 60, 40), search=10), ref[120:160, 150:210])
    assert not aligner.locate(_shift(ref, 25, 0)).ok
    assert not aligner.locate(_texture(seed=1)).ok
