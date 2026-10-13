"""Turn a burst of frames into one measurement. Pure: no I/O, no clocks, no globals."""

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from oiltank.calibration import Calibration
from oiltank.geometry import Aligner, rectify
from oiltank.model import Frame, FrameResult, Measurement, Notice, Problem
from oiltank.readers.base import GaugeReader


@dataclass(frozen=True, slots=True)
class BurstParams:
    min_frame_confidence: float = 0.3
    """Frames below this confidence are left out of the burst median."""
    spread_ok: float = 0.01
    """Median deviation of ``t`` across frames (in t units) that costs no confidence."""
    spread_bad: float = 0.05
    """...and the deviation at which confidence reaches zero."""
    min_travel_px: float = 40.0
    """Below this many px of disc travel (about 2.5% of the gauge per px), flag low resolution."""


def measure(
    frames: Sequence[Frame],
    aligner: Aligner,
    reader: GaugeReader,
    calibration: Calibration,
    params: BurstParams | None = None,
) -> Measurement:
    """Align, rectify and read each frame, then combine the frames by median.

    Raises :class:`~oiltank.calibration.ResolutionMismatch` if a frame's size doesn't match the
    calibration, since that is a configuration error rather than a bad reading.
    """
    if not frames:
        raise ValueError("measure() needs at least one frame")
    p = params or BurstParams()
    results: list[FrameResult] = []
    for frame in frames:
        calibration.check_frame_size(frame.size)
        alignment = aligner.locate(frame.gray)
        if not alignment.ok:
            results.append(FrameResult(frame, alignment, strip=None, reading=None))
            continue
        strip = rectify(frame.gray, calibration.axis, alignment)
        results.append(FrameResult(frame, alignment, strip, reader.read(strip)))

    notices: list[Notice] = []
    if calibration.axis.length < p.min_travel_px:
        notices.append(Notice.LOW_RESOLUTION)

    aligned = [r for r in results if r.alignment.ok]
    if aligned:
        shift = float(np.median([np.hypot(r.alignment.dx, r.alignment.dy) for r in aligned]))
        if shift > calibration.anchor.recalibrate_shift:
            notices.append(Notice.RECALIBRATE)

    def failed(problem: Problem) -> Measurement:
        return Measurement(
            ts=frames[-1].ts,
            t=None,
            fraction=None,
            confidence=0.0,
            indicator=None,
            problem=problem,
            notices=tuple(notices),
            frames=tuple(results),
            reader=reader.name,
        )

    majority = len(results) // 2 + 1
    if len(aligned) < majority:
        return failed(Problem.CAMERA_MOVED)

    good = [
        (r.strip, r.reading, r.reading.t)
        for r in aligned
        if r.strip is not None
        and r.reading is not None
        and r.reading.t is not None
        and r.reading.confidence >= p.min_frame_confidence
    ]
    if len(good) < majority:
        return failed(Problem.LOW_CONFIDENCE)

    ts_ = np.array([t for _, _, t in good])
    t = float(np.median(ts_))
    spread = float(np.median(np.abs(ts_ - t)))
    agreement = float(
        np.clip(1.0 - max(0.0, spread - p.spread_ok) / (p.spread_bad - p.spread_ok), 0.0, 1.0)
    )
    confidence = float(np.median([reading.confidence for _, reading, _ in good])) * agreement

    # Indicator position in frame coordinates, from the frame whose t is closest to the median.
    strip, reading, _ = min(good, key=lambda g: abs(g[2] - t))
    indicator = None
    if reading.row is not None:
        col = reading.col if reading.col is not None else strip.cols / 2
        indicator = strip.frame_point(col, reading.row)

    return Measurement(
        ts=frames[-1].ts,
        t=t,
        fraction=calibration.fraction(t),
        confidence=confidence,
        indicator=indicator,
        problem=None,
        notices=tuple(notices),
        frames=tuple(results),
        reader=reader.name,
    )
