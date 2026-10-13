"""Synthetic variants of the sample frames: a moving disc, image degradation, camera shifts."""

from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from oiltank.calibration import Calibration, ResolutionMismatch
from oiltank.geometry import Aligner
from oiltank.model import Frame, GrayImage, Measurement, Notice, Problem
from oiltank.pipeline import measure
from oiltank.readers import GaugeReader, ReaderConfig, build_reader
from tests.cv import synth

ACTUAL_Y = 1047.5


def _measure(
    frame: Frame,
    gray: GrayImage,
    aligner: Aligner,
    reader: GaugeReader,
    calibration: Calibration,
) -> Measurement:
    return measure([synth.with_gray(frame, gray)], aligner, reader, calibration)


def _t_for_y(calibration: Calibration, y: float) -> float:
    top, bottom = calibration.axis.top[1], calibration.axis.bottom[1]
    return 1.0 - (y - top) / (bottom - top)


@pytest.mark.parametrize("new_cy", [1027, 1040, 1060, 1080, 1100, 1120, 1140, 1148])
def test_disc_moved_along_travel(
    new_cy: int,
    sample_frames: list[Frame],
    aligner: Aligner,
    reader: GaugeReader,
    calibration: Calibration,
) -> None:
    frame = sample_frames[0]
    m = _measure(frame, synth.move_disc(frame.gray, new_cy), aligner, reader, calibration)
    assert m.ok, m.problem
    assert m.t == pytest.approx(_t_for_y(calibration, new_cy), abs=0.02)
    assert m.confidence >= 0.8


def test_disc_removed_is_low_confidence(
    sample_frames: list[Frame], aligner: Aligner, reader: GaugeReader, calibration: Calibration
) -> None:
    frame = sample_frames[0]
    m = _measure(frame, synth.remove_disc(frame.gray), aligner, reader, calibration)
    assert not m.ok
    assert m.problem is Problem.LOW_CONFIDENCE
    assert m.fraction is None


_DEGRADATIONS: dict[str, Callable[[GrayImage], GrayImage]] = {
    "noise5": lambda g: synth.add_noise(g, 5),
    "noise10": lambda g: synth.add_noise(g, 10),
    "noise20": lambda g: synth.add_noise(g, 20),
    "jpeg50": lambda g: synth.jpeg(g, 50),
    "jpeg30": lambda g: synth.jpeg(g, 30),
    "blur1.5": lambda g: synth.blur(g, 1.5),
    "blur3": lambda g: synth.blur(g, 3.0),
    "gain0.75": lambda g: synth.gain(g, 0.75),
    "gain1.25": lambda g: synth.gain(g, 1.25),
    "gamma0.75": lambda g: synth.gamma(g, 0.75),
    "gamma1.33": lambda g: synth.gamma(g, 1.33),
}


@pytest.mark.parametrize("name", list(_DEGRADATIONS))
def test_degraded_image(
    name: str,
    sample_frames: list[Frame],
    aligner: Aligner,
    reader: GaugeReader,
    calibration: Calibration,
) -> None:
    frame = sample_frames[0]
    m = _measure(frame, _DEGRADATIONS[name](frame.gray), aligner, reader, calibration)
    assert m.ok, m.problem
    assert m.indicator is not None
    assert m.indicator[1] == pytest.approx(ACTUAL_Y, abs=2.5)


@pytest.mark.parametrize(("dx", "dy"), [(30, 0), (-30, 20), (0, -30), (12.5, -7.25)])
def test_camera_nudged(
    dx: float,
    dy: float,
    sample_frames: list[Frame],
    aligner: Aligner,
    reader: GaugeReader,
    calibration: Calibration,
) -> None:
    frame = sample_frames[0]
    m = _measure(frame, synth.shift(frame.gray, dx, dy), aligner, reader, calibration)
    assert m.ok, m.problem
    assert m.t == pytest.approx(_t_for_y(calibration, ACTUAL_Y), abs=0.01)
    assert m.indicator is not None
    assert m.indicator[1] == pytest.approx(ACTUAL_Y + dy, abs=2.0)
    shifted_far = np.hypot(dx, dy) > calibration.anchor.recalibrate_shift
    assert (Notice.RECALIBRATE in m.notices) == shifted_far


def test_camera_moved_far(
    sample_frames: list[Frame], aligner: Aligner, reader: GaugeReader, calibration: Calibration
) -> None:
    frame = sample_frames[0]
    moved = np.roll(frame.gray, 400, axis=1)
    m = _measure(frame, moved, aligner, reader, calibration)
    assert m.problem is Problem.CAMERA_MOVED


def test_majority_of_burst_must_agree_on_alignment(
    sample_frames: list[Frame], aligner: Aligner, reader: GaugeReader, calibration: Calibration
) -> None:
    frames = list(sample_frames[:3])
    frames[0] = synth.with_gray(frames[0], np.roll(frames[0].gray, 400, axis=1))
    m = measure(frames, aligner, reader, calibration)
    assert m.ok  # one bad frame out of three is outvoted
    assert sum(not r.alignment.ok for r in m.frames) == 1


@pytest.mark.parametrize(("size", "low_res"), [((1280, 720), False), ((640, 360), True)])
def test_lower_resolutions(
    size: tuple[int, int],
    low_res: bool,
    tmp_path: Path,
    sample_frames: list[Frame],
    calibration: Calibration,
) -> None:
    frame = sample_frames[0]
    small, cal = synth.downscaled(frame.gray, calibration, size, tmp_path)
    m = measure(
        [synth.with_gray(frame, small)],
        Aligner(cal.anchor, cal.anchor_patch),
        build_reader(ReaderConfig(), cal),
        cal,
    )
    assert m.ok, m.problem
    assert m.indicator is not None
    k = size[0] / calibration.frame_size[0]
    assert m.indicator[1] / k == pytest.approx(ACTUAL_Y, abs=4.0)
    assert (Notice.LOW_RESOLUTION in m.notices) == low_res


def test_wrong_resolution_raises(
    tmp_path: Path,
    sample_frames: list[Frame],
    aligner: Aligner,
    reader: GaugeReader,
    calibration: Calibration,
) -> None:
    frame = sample_frames[0]
    small, _ = synth.downscaled(frame.gray, calibration, (640, 360), tmp_path)
    with pytest.raises(ResolutionMismatch):
        _measure(frame, small, aligner, reader, calibration)
