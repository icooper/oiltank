"""Core data types passed between pipeline stages."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

import numpy as np
from numpy.typing import NDArray

type GrayImage = NDArray[np.uint8]
type FloatArray = NDArray[np.float32]
type Point = tuple[float, float]


@dataclass(frozen=True, slots=True)
class Frame:
    """A single decoded camera frame, in grayscale (the camera is always in IR mode)."""

    gray: GrayImage
    ts: datetime
    source: str
    data: bytes | None = field(default=None, repr=False)
    """The encoded image as received (JPEG from the camera), when there is one."""

    @property
    def size(self) -> tuple[int, int]:
        """Frame size as ``(width, height)``."""
        h, w = self.gray.shape
        return w, h


@dataclass(frozen=True, slots=True)
class Alignment:
    """Offset of a frame relative to the calibration reference, found via the anchor patch."""

    dx: float
    dy: float
    score: float
    ok: bool


@dataclass(frozen=True, slots=True)
class GaugeStrip:
    """The gauge, warped upright: row 0 is above the top of travel, the last row below the bottom.

    ``to_frame`` is the 2x3 affine matrix mapping strip ``(col, row)`` to frame ``(x, y)``.
    """

    pixels: GrayImage
    t_of_row: FloatArray
    to_frame: NDArray[np.float64]
    px_per_row: float
    px_per_col: float

    @property
    def rows(self) -> int:
        return int(self.pixels.shape[0])

    @property
    def cols(self) -> int:
        return int(self.pixels.shape[1])

    def frame_point(self, col: float, row: float) -> Point:
        """Map a (sub-pixel) strip coordinate to frame pixel coordinates."""
        x, y = self.to_frame @ np.array([col, row, 1.0])
        return float(x), float(y)

    def t_at(self, row: float) -> float:
        """Gauge position ``t`` at a (sub-pixel) strip row, by linear interpolation."""
        rows = np.arange(self.rows, dtype=np.float64)
        return float(np.interp(row, rows, self.t_of_row.astype(np.float64)))


@dataclass(frozen=True, slots=True)
class Reading:
    """What a reader found in one strip.

    ``t`` is the indicator's position along the gauge axis: 1.0 at the top of travel, 0.0 at the
    bottom (values slightly outside [0, 1] are possible). Readers never convert ``t`` to a level.
    """

    t: float | None
    confidence: float
    row: float | None
    col: float | None
    scores: FloatArray
    debug: Mapping[str, float] = field(default_factory=dict)


class Problem(StrEnum):
    """Why a measurement could not be trusted."""

    CAMERA_MOVED = "camera_moved"
    LOW_CONFIDENCE = "low_confidence"
    CAMERA_UNREACHABLE = "camera_unreachable"


class Notice(StrEnum):
    """Conditions worth surfacing that don't invalidate a measurement."""

    LOW_RESOLUTION = "low_resolution"
    RECALIBRATE = "recalibrate_suggested"


@dataclass(frozen=True, slots=True)
class FrameResult:
    """The pipeline's result for one frame of a burst."""

    frame: Frame
    alignment: Alignment
    strip: GaugeStrip | None
    reading: Reading | None


@dataclass(frozen=True, slots=True)
class Measurement:
    """The combined result of one burst of frames."""

    ts: datetime
    t: float | None
    fraction: float | None
    confidence: float
    indicator: Point | None
    """Where the disc centre is in the frame that located it (frame px)."""
    position: Point | None
    """The same point with that frame's camera shift removed: reference-frame px, the coordinates
    the calibration uses."""
    problem: Problem | None
    notices: tuple[Notice, ...]
    frames: tuple[FrameResult, ...]
    reader: str

    @property
    def ok(self) -> bool:
        return self.problem is None and self.fraction is not None
