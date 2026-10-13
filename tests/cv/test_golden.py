"""The six real 4K frames: the disc's centre is at y ≈ 1048 with the tank at about 98%."""

import pytest

from oiltank.calibration import Calibration
from oiltank.geometry import Aligner
from oiltank.model import Frame
from oiltank.pipeline import measure
from oiltank.readers import GaugeReader

EXPECTED_Y = 1048.0
TOLERANCE_PX = 4.0


@pytest.mark.parametrize("index", range(6))
def test_each_frame(
    index: int,
    sample_frames: list[Frame],
    aligner: Aligner,
    reader: GaugeReader,
    calibration: Calibration,
) -> None:
    m = measure([sample_frames[index]], aligner, reader, calibration)
    assert m.ok, m.problem
    assert m.indicator is not None
    assert m.indicator[1] == pytest.approx(EXPECTED_Y, abs=TOLERANCE_PX)
    assert m.confidence >= 0.8
    assert m.notices == ()


def test_frames_agree(
    sample_frames: list[Frame], aligner: Aligner, reader: GaugeReader, calibration: Calibration
) -> None:
    ys = []
    for frame in sample_frames:
        m = measure([frame], aligner, reader, calibration)
        assert m.indicator is not None
        ys.append(m.indicator[1])
    assert max(ys) - min(ys) <= 2.0


def test_burst(
    sample_frames: list[Frame], aligner: Aligner, reader: GaugeReader, calibration: Calibration
) -> None:
    m = measure(sample_frames, aligner, reader, calibration)
    assert m.ok
    assert m.indicator is not None
    assert m.indicator[1] == pytest.approx(EXPECTED_Y, abs=2.0)
    assert m.confidence >= 0.9
    assert m.t == pytest.approx(calibration.axis.t_of((2168.0, EXPECTED_Y)), abs=2 / 125)
    # The owner put the tank at ~98% (a week after a fill); the sample calibration has that point.
    assert m.fraction == pytest.approx(0.98, abs=0.01)
    assert all(r.alignment.ok and abs(r.alignment.dx) < 0.5 for r in m.frames)
