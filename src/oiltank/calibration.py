"""Per-installation calibration: where the gauge is, and how its position maps to a level.

A calibration is a YAML file plus a reference frame (the image the anchor patch is cut from). The
reference path in the YAML is resolved relative to the YAML file's directory.
"""

import itertools
from pathlib import Path
from typing import Annotated, Self

import numpy as np
import yaml
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from oiltank.model import GrayImage, Point
from oiltank.sources.files import load_frame

CALIBRATION_VERSION = 1

type Rect = tuple[int, int, int, int]
"""``(x, y, width, height)`` in frame pixels."""


class CalibrationError(ValueError):
    """The calibration is invalid or doesn't match the frames being read."""


class ResolutionMismatch(CalibrationError):  # noqa: N818 - reads better than ...Error here
    """A frame's size differs from the size the calibration was made at."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Axis(_Model):
    """The line the indicator disc's centre travels along, in frame pixels.

    ``top`` is the disc centre at the top of its travel (t = 1), ``bottom`` at the bottom (t = 0).
    """

    top: Point
    bottom: Point
    half_width: Annotated[float, Field(gt=0, description="Half the strip width across the axis.")]
    margin: Annotated[
        float,
        Field(ge=0, le=1, description="Strip extension past each end, as a fraction of length."),
    ] = 0.25

    @property
    def length(self) -> float:
        return float(np.hypot(self.bottom[0] - self.top[0], self.bottom[1] - self.top[1]))

    def point(self, t: float) -> Point:
        """Frame position of the disc centre at gauge position ``t`` (reference-frame coords)."""
        (tx, ty), (bx, by) = self.top, self.bottom
        return bx + (tx - bx) * t, by + (ty - by) * t

    def t_of(self, p: Point) -> float:
        """Gauge position ``t`` of a frame position, projected onto the axis."""
        (tx, ty), (bx, by) = self.top, self.bottom
        return ((p[0] - bx) * (tx - bx) + (p[1] - by) * (ty - by)) / self.length**2

    @model_validator(mode="after")
    def _non_degenerate(self) -> Self:
        if self.length < 5:
            raise ValueError("axis top and bottom must be at least 5 px apart")
        return self


class Disc(_Model):
    """Apparent size of the indicator disc, in frame pixels."""

    width: Annotated[float, Field(gt=0)]
    height: Annotated[float, Field(gt=0)]


class Anchor(_Model):
    """A patch of static scene (not the gauge interior) used to detect and undo camera shifts."""

    rect: Rect
    search: Annotated[int, Field(gt=0, description="Search radius in px.")] = 48
    min_score: Annotated[float, Field(gt=0, le=1)] = 0.6
    recalibrate_shift: Annotated[
        float, Field(gt=0, description="Shift (px) above which recalibration is suggested.")
    ] = 20.0


class CalibrationPoint(_Model):
    """A known gauge reading (0 = E, 1 = F) at a disc position.

    The position is either ``t`` along the axis, or ``at``: the disc centre in reference-frame
    pixels, as ``oiltank read`` prints it. An ``at`` point stays on the disc if the axis endpoints
    are edited later; a ``t`` point moves with them.
    """

    t: float | None = None
    at: Point | None = None
    fraction: Annotated[float, Field(ge=0, le=1)]
    note: str = ""

    @model_validator(mode="after")
    def _one_position(self) -> Self:
        if (self.t is None) == (self.at is None):
            raise ValueError("a calibration point needs exactly one of t or at")
        return self

    def t_on(self, axis: Axis) -> float:
        """This point's gauge position on ``axis``."""
        if self.at is not None:
            return axis.t_of(self.at)
        assert self.t is not None  # guaranteed by _one_position
        return self.t


def _piecewise_linear(x: float, xs: NDArray[np.float64], ys: NDArray[np.float64]) -> float:
    """Interpolate through ``(xs, ys)`` (both increasing), continuing the end segments linearly."""
    if x < xs[0]:
        return float(ys[0] + (x - xs[0]) * (ys[1] - ys[0]) / (xs[1] - xs[0]))
    if x > xs[-1]:
        return float(ys[-1] + (x - xs[-1]) * (ys[-1] - ys[-2]) / (xs[-1] - xs[-2]))
    return float(np.interp(x, xs, ys))


def _default_points() -> list[CalibrationPoint]:
    return [
        CalibrationPoint(t=0.0, fraction=0.0, note="default: bottom of travel = E"),
        CalibrationPoint(t=1.0, fraction=1.0, note="default: top of travel = F"),
    ]


class Calibration(_Model):
    version: int = CALIBRATION_VERSION
    reference_image: Path
    frame_size: tuple[int, int]
    axis: Axis
    disc: Disc
    anchor: Anchor
    points: list[CalibrationPoint] = Field(default_factory=_default_points)

    _base_dir: Path = PrivateAttr(default_factory=Path.cwd)
    _anchor_patch: GrayImage | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.version != CALIBRATION_VERSION:
            raise ValueError(f"unsupported calibration version {self.version}")
        if len(self.points) < 2:
            raise ValueError("at least two calibration points are required")
        pts = sorted((p.t_on(self.axis), p.fraction) for p in self.points)
        for (ta, fa), (tb, fb) in itertools.pairwise(pts):
            if not (tb > ta and fb > fa):
                raise ValueError(
                    "calibration points must increase in both t and fraction "
                    f"(t={ta:.4g}->{fa}, t={tb:.4g}->{fb})"
                )
        w, h = self.frame_size
        x, y, aw, ah = self.anchor.rect
        if aw <= 0 or ah <= 0 or x < 0 or y < 0 or x + aw > w or y + ah > h:
            raise ValueError(f"anchor rect {self.anchor.rect} is outside the {w}x{h} frame")
        return self

    # ---- persistence ---------------------------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> Self:
        try:
            data = yaml.safe_load(path.read_text())
        except OSError as e:
            raise CalibrationError(f"cannot read calibration {path}: {e}") from e
        cal = cls.model_validate(data)
        cal._base_dir = path.resolve().parent
        return cal

    def save(self, path: Path) -> None:
        data = self.model_dump(mode="json")
        data["points"] = [{k: v for k, v in p.items() if v is not None} for p in data["points"]]
        header = "# oiltank calibration. Safe to hand-edit; validated on load.\n"
        path.write_text(header + yaml.safe_dump(data, sort_keys=False))

    @property
    def reference_path(self) -> Path:
        return (
            self.reference_image
            if self.reference_image.is_absolute()
            else (self._base_dir / self.reference_image)
        )

    # ---- derived values ------------------------------------------------------------------------

    @property
    def anchor_patch(self) -> GrayImage:
        """The anchor template, cut from the reference frame (loaded on first use)."""
        if self._anchor_patch is None:
            ref = load_frame(self.reference_path)
            self.check_frame_size(ref.size)
            x, y, w, h = self.anchor.rect
            self._anchor_patch = np.ascontiguousarray(ref.gray[y : y + h, x : x + w])
        return self._anchor_patch

    def check_frame_size(self, size: tuple[int, int]) -> None:
        if tuple(size) != tuple(self.frame_size):
            w, h = size
            cw, ch = self.frame_size
            raise ResolutionMismatch(
                f"frame is {w}x{h} but the calibration was made at {cw}x{ch}; "
                "check the camera URL (main stream?) or recalibrate"
            )

    def _curve(self) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
        pts = sorted((p.t_on(self.axis), p.fraction) for p in self.points)
        ts = np.array([t for t, _ in pts], dtype=np.float64)
        fs = np.array([f for _, f in pts], dtype=np.float64)
        return ts, fs

    def fraction(self, t: float) -> float:
        """Map gauge position ``t`` to a gauge reading in [0, 1].

        Piecewise linear through the points. Past the outermost points the end segments are
        extended, so points needn't span the whole gauge: a single checked reading near full plus
        the default E point still puts F in the right place. The result is clamped to [0, 1].
        """
        ts, fs = self._curve()
        return float(np.clip(_piecewise_linear(t, ts, fs), 0.0, 1.0))

    def t_for(self, fraction: float) -> float:
        """The gauge position ``t`` that reads as ``fraction``: the inverse of :meth:`fraction`.

        Not clamped; a result outside [0, 1] means that reading lies beyond the axis endpoints.
        """
        ts, fs = self._curve()
        return _piecewise_linear(fraction, fs, ts)
