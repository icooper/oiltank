"""Which gauge axis a stored reading's ``t`` was measured along, and how to carry ``t`` from one
axis to another. Pure: no I/O.

A reading's ``t`` is its position along the calibration's axis at the time, so editing the axis
endpoints changes what a stored ``t`` means. History therefore keeps an *epoch* for each axis it
has measured along. Each epoch maps its ``t`` onto one common scale (``scale * t + shift``), which
is the first epoch's ``t``.

- An **axis edit** (same reference frame, endpoints moved) projects the new axis onto the one
  before it, so a stored reading keeps its place on the gauge. An edit is taken to fit the
  disc's travel better, so the disc is assumed to run along the new axis. That only matters when
  an edit tilts the axis.
- A **rebase** (``oiltank calibrate rebase``: a new reference frame, with the axis translated
  along with the camera) leaves ``t`` alone, because the disc is at the same place on the gauge.
- Anything else can't be related to the axis before it. ``t`` carries over unchanged, as it did
  before epochs existed. Examples: a new reference frame whose axis didn't just translate
  (``calibrate init --force``), or an edit that turns the axis by more than ``MAX_TURN_DEG``.
"""

import math
from dataclasses import dataclass
from enum import StrEnum

from oiltank.calibration import Axis
from oiltank.model import Point

SAME_PX = 0.01
"""Endpoints this close count as unchanged (``Calibration.save`` rounds to 0.001 px)."""
MAX_TURN_DEG = 30.0
"""An edit that turns the axis by more than this is a different setup, not an edit."""


class Change(StrEnum):
    """How an epoch relates to the one in use before it."""

    FIRST = "first"
    AXIS_EDIT = "axis_edit"
    REBASE = "rebase"
    NEW_REFERENCE = "new_reference"


@dataclass(frozen=True, slots=True)
class AxisEpoch:
    reference: str
    """The calibration's ``reference_image``, as written in the file."""
    top: Point
    bottom: Point
    scale: float = 1.0
    shift: float = 0.0
    """The first epoch's ``t`` is ``scale * t + shift``."""
    change: Change = Change.FIRST
    id: int | None = None
    """Its row in history; None until it's stored."""

    def matches(self, reference: str, axis: Axis) -> bool:
        return (
            reference == self.reference
            and _near(axis.top, self.top)
            and _near(axis.bottom, self.bottom)
        )


def successor(prev: AxisEpoch | None, reference: str, axis: Axis) -> AxisEpoch:
    """The epoch for ``axis`` on ``reference``, following ``prev`` (the one in use until now).

    Returns ``prev`` itself if nothing changed. The result has no ``id``; storing it gives it one.
    """
    if prev is None:
        return AxisEpoch(reference, axis.top, axis.bottom)
    if prev.matches(reference, axis):
        return prev
    if reference == prev.reference:
        projected = _projection(prev, axis)
        if projected is not None:
            a, b = projected
            return AxisEpoch(
                reference,
                axis.top,
                axis.bottom,
                scale=prev.scale * a,
                shift=prev.scale * b + prev.shift,
                change=Change.AXIS_EDIT,
            )
        change = Change.NEW_REFERENCE  # turned too far to be the same gauge
    elif _near(_minus(axis.top, prev.top), _minus(axis.bottom, prev.bottom)):
        change = Change.REBASE
    else:
        change = Change.NEW_REFERENCE
    return AxisEpoch(
        reference, axis.top, axis.bottom, scale=prev.scale, shift=prev.shift, change=change
    )


def carry(src: AxisEpoch, dst: AxisEpoch) -> tuple[float, float]:
    """``(a, b)`` such that a ``t`` measured in ``src`` is ``a * t + b`` in ``dst``."""
    return src.scale / dst.scale, (src.shift - dst.shift) / dst.scale


def _projection(prev: AxisEpoch, axis: Axis) -> tuple[float, float] | None:
    """``(a, b)``: the disc at ``t`` on ``axis`` projects to ``a * t + b`` on ``prev``'s axis.

    None if ``axis`` turned by more than ``MAX_TURN_DEG``.
    """
    dpx, dpy = _minus(prev.top, prev.bottom)
    dnx, dny = _minus(axis.top, axis.bottom)
    along = dnx * dpx + dny * dpy
    if along <= math.hypot(dnx, dny) * math.hypot(dpx, dpy) * math.cos(math.radians(MAX_TURN_DEG)):
        return None
    norm = dpx * dpx + dpy * dpy
    ox, oy = _minus(axis.bottom, prev.bottom)
    return along / norm, (ox * dpx + oy * dpy) / norm


def _minus(p: Point, q: Point) -> Point:
    return p[0] - q[0], p[1] - q[1]


def _near(p: Point, q: Point) -> bool:
    return abs(p[0] - q[0]) <= SAME_PX and abs(p[1] - q[1]) <= SAME_PX
