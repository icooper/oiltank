import numpy as np
import pytest

from oiltank.readers.profile import Peak, find_peak, peak_confidence


def _bump(centre: float, n: int = 100, width: float = 4.0) -> np.ndarray:
    x = np.arange(n, dtype=np.float32)
    return np.exp(-0.5 * ((x - centre) / width) ** 2).astype(np.float32)


@pytest.mark.parametrize("centre", [30.0, 30.25, 30.5, 61.8])
def test_subpixel_peak(centre: float) -> None:
    peak = find_peak(_bump(centre), exclusion=10)
    assert peak is not None
    assert peak.row == pytest.approx(centre, abs=0.1)
    assert peak.score == pytest.approx(1.0, abs=0.02)


def test_runner_up_is_outside_exclusion_zone() -> None:
    scores = _bump(30) + 0.6 * _bump(70)
    peak = find_peak(scores.astype(np.float32), exclusion=10)
    assert peak is not None
    assert peak.row == pytest.approx(30, abs=0.1)
    assert peak.runner_up == pytest.approx(0.6, abs=0.02)
    assert peak.prominence == pytest.approx(0.4, abs=0.03)


def test_nans_ignored_and_all_nan_is_none() -> None:
    scores = _bump(50)
    scores[:20] = np.nan
    scores[50] = np.nan  # a hole at the true peak: falls back to the best neighbour
    peak = find_peak(scores, exclusion=5)
    assert peak is not None
    assert abs(peak.row - 50) <= 1
    assert find_peak(np.full(10, np.nan, np.float32), exclusion=2) is None


def test_confidence_ramps() -> None:
    kw = {"score_floor": 0.3, "score_good": 0.5, "prominence_good": 0.2}
    assert peak_confidence(Peak(row=0, score=0.6, runner_up=0.1), **kw) == 1.0
    assert peak_confidence(Peak(row=0, score=0.25, runner_up=0.0), **kw) == 0.0
    assert peak_confidence(Peak(row=0, score=0.4, runner_up=0.0), **kw) == pytest.approx(0.5)
    assert peak_confidence(Peak(row=0, score=0.6, runner_up=0.5), **kw) == pytest.approx(0.5)
    assert peak_confidence(Peak(row=0, score=0.6, runner_up=float("-inf")), **kw) == 1.0
