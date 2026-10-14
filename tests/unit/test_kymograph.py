"""The gauge-over-time image: columns resampled onto the current axis, and normalised."""

import numpy as np
import pytest

from oiltank.web.kymograph import Grid, band, normalise, profile, resample, tile

GRID = Grid(t_top=1.25, t_bottom=-0.25)


def strip_with_band(row: int, rows: int = 256, cols: int = 128) -> np.ndarray:
    pixels = np.full((rows, cols), 60, np.uint8)
    pixels[row - 3 : row + 4, 40:88] = 200  # the disc, across the middle
    return pixels


def peak_row(column: np.ndarray) -> float:
    """The middle of the brightest rows (the band is a plateau several rows tall)."""
    rows = np.flatnonzero(column >= np.nanmax(column) - 1e-6)
    return float(rows.mean())


def test_a_strip_on_the_current_axis_lands_where_its_rows_say() -> None:
    # A strip rectified exactly like the grid: row r is grid row r.
    t = GRID.t()
    column = resample(profile(strip_with_band(100), 20), float(t[0]), float(t[-1]), GRID)
    assert peak_row(column) == pytest.approx(100, abs=1)


def test_a_carried_strip_moves_onto_the_current_axis() -> None:
    t = GRID.t()
    # The same strip, measured along an axis whose t is 0.9 * t + 0.05 on the current one.
    first, last = 0.9 * float(t[0]) + 0.05, 0.9 * float(t[-1]) + 0.05
    column = resample(profile(strip_with_band(100), 20), first, last, GRID)
    t_disc = 0.9 * float(t[100]) + 0.05
    expected = (GRID.t_top - t_disc) / (GRID.t_top - GRID.t_bottom) * GRID.rows - 0.5
    assert peak_row(column) == pytest.approx(expected, abs=1)
    # It covers less than the grid: the rest is NaN, so transparent.
    assert np.isnan(column[0]) and np.isnan(column[-1])


def test_normalise_evens_out_brightness() -> None:
    column = np.linspace(10, 30, 256)
    day, alpha = normalise(column * 3 + 100)
    night, _ = normalise(column)
    assert np.abs(day.astype(int) - night.astype(int)).max() <= 1
    assert alpha.min() == 255
    flat_gray, _ = normalise(np.full(256, 50.0))
    assert flat_gray.max() == 0  # a flat column isn't stretched into noise
    _, holes = normalise(np.where(np.arange(256) < 20, np.nan, 5.0))
    assert holes[:20].max() == 0 and holes[20:].min() == 255


def test_tile() -> None:
    t = GRID.t()
    column = resample(profile(strip_with_band(60), 20), float(t[0]), float(t[-1]), GRID)
    image = tile([column, None, column])
    assert image.shape == (256, 3, 4)
    assert image[:, 1, 3].max() == 0  # no strip: transparent
    assert image[60, 0, 0] > image[200, 0, 0]


def test_band_follows_the_disc_width() -> None:
    assert band(disc_width=64, half_width=50) == 20  # a quarter of the disc either side
    assert band(disc_width=4, half_width=50) == 2
    assert band(disc_width=1000, half_width=50) == 64
