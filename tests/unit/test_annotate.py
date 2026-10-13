import cv2
import numpy as np
import pytest

from oiltank.annotate import encode_jpeg, render, render_diagnostic
from oiltank.calibration import Calibration
from oiltank.geometry import Aligner
from oiltank.model import Frame
from oiltank.pipeline import measure
from oiltank.readers import GaugeReader
from tests.cv import synth


def test_render_and_encode_fit_budget(
    sample_frames: list[Frame], aligner: Aligner, reader: GaugeReader, calibration: Calibration
) -> None:
    frame = sample_frames[0]
    m = measure([frame], aligner, reader, calibration)
    img = render(frame.gray, m, calibration, capacity=260)
    assert img.ndim == 3
    assert img.shape[2] == 3
    data = encode_jpeg(img)
    assert len(data) <= 50_000
    decoded = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None
    assert decoded.shape[:2] == img.shape[:2]


def test_render_failed_measurement(
    sample_frames: list[Frame], aligner: Aligner, reader: GaugeReader, calibration: Calibration
) -> None:
    frame = sample_frames[0]
    m = measure(
        [synth.with_gray(frame, synth.remove_disc(frame.gray))], aligner, reader, calibration
    )
    assert not m.ok
    assert render(frame.gray, m, calibration).size > 0


def test_encode_shrinks_to_tight_budget() -> None:
    rng = np.random.default_rng(0)
    noisy = rng.integers(0, 255, (600, 600, 3), dtype=np.uint8)
    data = encode_jpeg(noisy, max_bytes=20_000)
    assert len(data) <= 20_000


def _count(img: np.ndarray, bgr: tuple[int, int, int]) -> int:
    return int(np.all(img == np.array(bgr, np.uint8), axis=2).sum())


_CYAN = (230, 210, 60)


def test_diagnostic_outlines_both_ends_of_the_scale(
    sample_frames: list[Frame], aligner: Aligner, reader: GaugeReader, calibration: Calibration
) -> None:
    frame = sample_frames[0]
    m = measure([frame], aligner, reader, calibration)
    img = render_diagnostic(frame.gray, calibration, m, height=900)
    assert img.ndim == 3
    assert 900 + 64 <= img.shape[0] <= 900 + 64 + 80
    # Cyan appears in two bands of rows: the disc outlines at 0% and 100% (with their labels).
    rows = np.nonzero(np.all(img == np.array(_CYAN, np.uint8), axis=2).any(axis=1))[0]
    bands = np.split(rows, np.nonzero(np.diff(rows) > 1)[0] + 1)
    assert len(bands) == 2
    # Scale-free check: band spacing / band height = (E - F distance) / disc height.
    (f0, f1), (e0, e1) = (bands[0][[0, -1]], bands[1][[0, -1]])
    ys = [calibration.axis.point(calibration.t_for(f))[1] for f in (1.0, 0.0)]
    expected = (ys[1] - ys[0]) / calibration.disc.height
    measured = ((e0 + e1) / 2 - (f0 + f1) / 2) / ((f1 - f0 + e1 - e0) / 2)
    assert measured == pytest.approx(expected, rel=0.1)


def test_diagnostic_without_a_measurement(
    sample_frames: list[Frame], calibration: Calibration
) -> None:
    img = render_diagnostic(sample_frames[0].gray, calibration, height=600)
    assert _count(img, _CYAN) > 100


def test_diagnostic_for_a_failed_measurement(
    sample_frames: list[Frame], aligner: Aligner, reader: GaugeReader, calibration: Calibration
) -> None:
    frame = sample_frames[0]
    removed = synth.with_gray(frame, synth.remove_disc(frame.gray))
    m = measure([removed], aligner, reader, calibration)
    assert not m.ok
    assert _count(render_diagnostic(removed.gray, calibration, m), _CYAN) > 100
