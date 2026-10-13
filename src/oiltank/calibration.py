"""Per-installation calibration: where the gauge is, and how its position maps to a level.

A calibration is a YAML file plus a reference frame (the image the anchor patch is cut from). The
reference path in the YAML is resolved relative to the YAML file's directory.
"""

import hashlib
import itertools
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Self

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


def reference_name(ts: datetime, suffix: str = ".jpg") -> Path:
    """The file name for a new reference frame taken at ``ts``: ``reference-<UTC time>.jpg``."""
    return Path(f"reference-{ts.astimezone(UTC):%Y%m%d-%H%M%S}{suffix}")


def save_reference(calibration_dir: Path, name: Path, data: bytes) -> None:
    """Write a reference frame's bytes, exactly as received, next to the calibration (unless a
    file of that name is already there: reference frames are never overwritten).

    Raises OSError if it can't be written.
    """
    target = calibration_dir / name
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    tmp.write_bytes(data)
    tmp.replace(target)


def calibration_etag(path: Path) -> str:
    """A tag for the calibration file's exact contents: it changes whenever the file does.

    Raises CalibrationError if the file can't be read.
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as e:
        raise CalibrationError(f"cannot read calibration {path}: {e}") from e


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    def updated(self, **changes: Any) -> Self:
        """A validated copy with ``changes`` applied (``model_copy`` doesn't validate)."""
        return self.model_validate({**dict(self), **changes})


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


def _piecewise_linear(
    x: NDArray[np.float64], xs: NDArray[np.float64], ys: NDArray[np.float64]
) -> NDArray[np.float64]:
    """Interpolate through ``(xs, ys)`` (both increasing), continuing the end segments linearly.

    NaN stays NaN.
    """
    below = ys[0] + (x - xs[0]) * (ys[1] - ys[0]) / (xs[1] - xs[0])
    above = ys[-1] + (x - xs[-1]) * (ys[-1] - ys[-2]) / (xs[-1] - xs[-2])
    inside = np.interp(x, xs, ys)
    return np.asarray(np.where(x < xs[0], below, np.where(x > xs[-1], above, inside)))


IMPLICIT_E = (0.0, 0.0)
"""``(t, fraction)``: until there are two points, the bottom of travel may stand in for E..."""
IMPLICIT_F = (1.0, 1.0)
"""...and the top of travel for F."""

_DEFAULT_HEADER = """\
# oiltank calibration. Safe to hand-edit; it is validated on load. `oiltank calibrate` keeps this
# header comment when it rewrites the file, but not comments further down.
"""


class _Flow(dict[str, Any]):
    """A mapping written in YAML flow style, ``{a: 1, b: 2}``."""


class _Dumper(yaml.SafeDumper):
    """Block style overall, but lists of scalars and points on one line; list items indented."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        super().increase_indent(flow, False)


def _represent_list(dumper: yaml.SafeDumper, data: list[Any]) -> yaml.Node:
    flow = all(not isinstance(v, list | dict) for v in data)
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=flow)


def _represent_flow(dumper: yaml.SafeDumper, data: _Flow) -> yaml.Node:
    return dumper.represent_mapping("tag:yaml.org,2002:map", data, flow_style=True)


_Dumper.add_representer(list, _represent_list)
_Dumper.add_representer(_Flow, _represent_flow)


def _rounded(value: Any) -> Any:
    """Round floats (recursively) so rewritten files stay readable."""
    if isinstance(value, float):
        return round(value, 3)
    if isinstance(value, list):
        return [_rounded(v) for v in value]
    if isinstance(value, dict):
        return {k: _rounded(v) for k, v in value.items()}
    return value


def _leading_comments(text: str) -> str:
    lines = []
    for line in text.splitlines(keepends=True):
        if not line.startswith("#"):
            break
        lines.append(line)
    return "".join(lines)


def _point_yaml(point: CalibrationPoint) -> _Flow:
    data = _rounded(point.model_dump(mode="json"))
    return _Flow({k: v for k, v in data.items() if v not in (None, "")})


class Calibration(_Model):
    version: int = CALIBRATION_VERSION
    reference_image: Path
    frame_size: tuple[int, int]
    axis: Axis
    disc: Disc
    anchor: Anchor
    points: list[CalibrationPoint] = Field(default_factory=list)

    _base_dir: Path = PrivateAttr(default_factory=Path.cwd)
    _anchor_patch: GrayImage | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.version != CALIBRATION_VERSION:
            raise ValueError(f"unsupported calibration version {self.version}")
        for (ta, fa), (tb, fb) in itertools.pairwise(self.scale_points()):
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

    # ---- construction and persistence ----------------------------------------------------------

    @classmethod
    def build(cls, base_dir: Path, **fields: Any) -> Self:
        """Validate a calibration whose relative reference path resolves against ``base_dir``."""
        cal = cls.model_validate(fields)
        cal._base_dir = base_dir.resolve()
        return cal

    def updated(self, **changes: Any) -> Self:
        """A validated copy with ``changes`` applied; the reference directory carries over."""
        return self.build(self._base_dir, **{**dict(self), **changes})

    @classmethod
    def load(cls, path: Path) -> Self:
        try:
            data = yaml.safe_load(path.read_text())
        except OSError as e:
            raise CalibrationError(f"cannot read calibration {path}: {e}") from e
        except yaml.YAMLError as e:
            raise CalibrationError(f"calibration {path} is not valid YAML: {e}") from e
        if not isinstance(data, dict):
            raise CalibrationError(f"calibration {path} should be a YAML mapping")
        return cls.build(path.parent, **data)

    def save(self, path: Path) -> None:
        """Write the calibration as YAML, atomically.

        An existing file's header comment is kept and its previous contents are copied to
        ``<name>.bak``.
        """
        data = _rounded(self.model_dump(mode="json", exclude={"points"}))
        points = [_point_yaml(p) for p in sorted(self.points, key=lambda p: p.t_on(self.axis))]
        header = _DEFAULT_HEADER
        if path.exists():
            previous = path.read_text()
            header = _leading_comments(previous) or header
            path.with_name(path.name + ".bak").write_text(previous)

        def dump(obj: object) -> str:
            return yaml.dump(obj, Dumper=_Dumper, sort_keys=False, width=110, allow_unicode=True)

        match len(points):
            case 0:
                scale_note = (
                    "# No points yet, so the scale runs from E at the bottom of travel (t = 0)"
                    " to F at the top (t = 1).\n"
                )
            case 1 if points[0]["fraction"] >= 0.5:
                scale_note = (
                    "# One point, so the scale also takes E as the bottom of travel (t = 0).\n"
                )
            case 1:
                scale_note = (
                    "# One point, so the scale also takes F as the top of travel (t = 1).\n"
                )
            case _:
                scale_note = ""
        text = header + dump(data) + scale_note + dump({"points": points})
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(text)
        tmp.replace(path)

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

    def use_reference(self, gray: GrayImage) -> None:
        """Take the anchor patch from ``gray`` rather than reading ``reference_image`` (for a
        reference frame that is still only in memory, e.g. fetched from the camera)."""
        self.check_frame_size((gray.shape[1], gray.shape[0]))
        x, y, w, h = self.anchor.rect
        self._anchor_patch = np.ascontiguousarray(gray[y : y + h, x : x + w])

    def check_frame_size(self, size: tuple[int, int]) -> None:
        if tuple(size) != tuple(self.frame_size):
            w, h = size
            cw, ch = self.frame_size
            raise ResolutionMismatch(
                f"frame is {w}x{h} but the calibration was made at {cw}x{ch}; "
                "check the camera URL (main stream?) or recalibrate"
            )

    def scale_points(self) -> list[tuple[float, float]]:
        """The ``(t, fraction)`` pairs the scale is built from, in order of ``t``.

        Defaults fill in until there are two points. With no points, the bottom of travel is E and
        the top is F. With one, it is paired with whichever of those is further from it in reading,
        so one known reading near full plus E at the bottom of travel sets the scale.
        """
        pts = sorted((p.t_on(self.axis), p.fraction) for p in self.points)
        match pts:
            case []:
                return [IMPLICIT_E, IMPLICIT_F]
            case [(t, f)]:
                return [IMPLICIT_E, (t, f)] if f >= 0.5 else [(t, f), IMPLICIT_F]
            case _:
                return pts

    def _curve(self) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
        pts = self.scale_points()
        ts = np.array([t for t, _ in pts], dtype=np.float64)
        fs = np.array([f for _, f in pts], dtype=np.float64)
        return ts, fs

    def fraction(self, t: float) -> float:
        """Map gauge position ``t`` to a gauge reading in [0, 1].

        Piecewise linear through :meth:`scale_points`. Past the outermost points the end segments
        are extended, so points needn't span the whole gauge. The result is clamped to [0, 1].
        """
        return float(self.fractions(np.float64(t)))

    def fractions(self, t: NDArray[np.float64] | np.float64) -> NDArray[np.float64]:
        """:meth:`fraction` for many positions at once (NaN, for no reading, stays NaN)."""
        ts, fs = self._curve()
        x = np.asarray(t, dtype=np.float64)
        return np.asarray(np.clip(_piecewise_linear(x, ts, fs), 0.0, 1.0))

    def t_for(self, fraction: float) -> float:
        """The gauge position ``t`` that reads as ``fraction``: the inverse of :meth:`fraction`.

        Not clamped; a result outside [0, 1] means that reading lies beyond the axis endpoints.
        """
        ts, fs = self._curve()
        return float(_piecewise_linear(np.asarray(fraction, dtype=np.float64), fs, ts))
