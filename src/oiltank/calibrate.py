"""The logic behind ``oiltank calibrate``: starting values, checks and edits for a calibration.

Nothing here reads or writes files. The CLI loads frames, calls these functions, and saves the
result.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import cv2
import numpy as np
from numpy.typing import NDArray

from oiltank.calibration import Anchor, Axis, Calibration, CalibrationPoint, Disc
from oiltank.geometry import Aligner, rectify
from oiltank.model import Alignment, Frame, GrayImage
from oiltank.pipeline import BurstParams
from oiltank.readers.classical import ClassicalReader

# Starting proportions, relative to the axis length, taken from the sample gauge. They are only a
# first guess: init scans for the disc size, and the diagnostic image shows the result.
DISC_WIDTH_PER_AXIS = 0.42
DISC_HEIGHT_PER_AXIS = 0.27
HALF_WIDTH_PER_AXIS = 0.33

SAME_POINT = 0.005
"""Points whose readings differ by less than this (half a percent) are the same point."""


# ---- starting values ---------------------------------------------------------------------------


def default_disc(axis: Axis) -> Disc:
    """A first guess at the disc size, in proportion to the travel. Refine with a scan."""
    return Disc(
        width=round(DISC_WIDTH_PER_AXIS * axis.length, 1),
        height=round(DISC_HEIGHT_PER_AXIS * axis.length, 1),
    )


def default_half_width(axis: Axis, disc: Disc) -> float:
    """Strip half-width: a third of the travel, and always comfortably wider than the disc."""
    return round(max(HALF_WIDTH_PER_AXIS * axis.length, 0.75 * disc.width), 1)


def travel_envelope(axis: Axis, disc: Disc) -> tuple[float, float, float, float]:
    """The box ``(x0, y0, x1, y1)`` the disc can occupy anywhere in its travel."""
    xs, ys = (axis.top[0], axis.bottom[0]), (axis.top[1], axis.bottom[1])
    hw, hh = disc.width / 2, disc.height / 2
    return min(xs) - hw, min(ys) - hh, max(xs) + hw, max(ys) + hh


def default_anchor(axis: Axis, disc: Disc, frame_size: tuple[int, int]) -> Anchor:
    """A patch just below the disc's lowest position: the gauge fitting and the tank top.

    Raises :class:`ValueError` if there is no room for one in the frame.
    """
    length = axis.length
    x0, _, x1, y1 = travel_envelope(axis, disc)
    fw, fh = frame_size
    w = min(round(1.9 * length), fw)
    top = round(y1 + 0.03 * length)
    h = min(round(1.1 * length), fh - top)
    if h < max(16, 0.25 * length):
        raise ValueError("no room below the gauge for an alignment anchor; give one explicitly")
    x = min(max(0, round((x0 + x1) / 2 - w / 2)), fw - w)
    return Anchor(rect=(x, top, w, h), search=max(8, round(0.3 * length)))


# ---- anchor check ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AnchorCheck:
    contrast: float
    """Standard deviation of the anchor patch, in grey levels."""
    max_error_px: float
    """Worst error recovering known camera shifts (inf if a shift wasn't found at all)."""
    overlap: float
    """Fraction of the anchor patch the disc can pass through."""
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems


# Self-test shifts, as fractions of the anchor's search radius.
_SELF_TEST = ((0.15, -0.1), (-0.35, 0.25), (0.6, 0.45))
MIN_CONTRAST = 8.0
MAX_SELF_TEST_ERROR_PX = 0.5
MAX_OVERLAP = 0.02


def _shifted(gray: GrayImage, dx: float, dy: float) -> GrayImage:
    m = np.array([[1, 0, dx], [0, 1, dy]], dtype=np.float64)
    h, w = gray.shape
    return np.asarray(
        cv2.warpAffine(gray, m, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE),
        dtype=np.uint8,
    )


def check_anchor(reference: GrayImage, calibration: Calibration) -> AnchorCheck:
    """Check the anchor works: it has texture, recovers known shifts, and avoids the disc."""
    anchor = calibration.anchor
    x, y, w, h = anchor.rect
    patch = np.ascontiguousarray(reference[y : y + h, x : x + w])
    contrast = float(patch.std())
    aligner = Aligner(anchor, patch)
    errors = []
    for fx, fy in _SELF_TEST:
        dx, dy = fx * anchor.search, fy * anchor.search
        found = aligner.locate(_shifted(reference, dx, dy))
        errors.append(float(np.hypot(found.dx - dx, found.dy - dy)) if found.ok else np.inf)
    max_error = max(errors)

    ex0, ey0, ex1, ey1 = travel_envelope(calibration.axis, calibration.disc)
    ix = max(0.0, min(x + w, ex1) - max(x, ex0))
    iy = max(0.0, min(y + h, ey1) - max(y, ey0))
    overlap = ix * iy / (w * h)

    problems = []
    if contrast < MIN_CONTRAST:
        problems.append(
            f"the anchor patch is nearly featureless (contrast {contrast:.1f}); "
            "choose a textured, static area"
        )
    if max_error > MAX_SELF_TEST_ERROR_PX:
        problems.append(
            f"the anchor failed its self-test (error up to {max_error:.2f} px recovering known "
            "shifts); choose a larger or more textured area"
        )
    if overlap > MAX_OVERLAP:
        problems.append(
            f"the anchor overlaps the disc's travel ({overlap:.0%} of it); the moving disc will "
            "disturb alignment"
        )
    return AnchorCheck(contrast, max_error, overlap, tuple(problems))


# ---- disc size scan ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DiscScan:
    """Where the reader puts the disc for each candidate disc size.

    A wrong size can lock onto other features in the dome, with high confidence. So the scan
    follows the region of neighbouring sizes that agree with the starting size about where the
    disc is, and recommends the size deepest inside it. Other positions that several sizes agree
    on are reported as ``alternatives``.
    """

    widths: tuple[float, ...]
    heights: tuple[float, ...]
    t: NDArray[np.float64]
    """Detected position per ``[width, height]``; NaN where nothing was found."""
    confidence: NDArray[np.float64]
    stable: NDArray[np.bool_]
    """Sizes that agree with the starting size about the position (and connect to it)."""
    best: Disc | None
    """The size deepest inside the stable region, or None if nothing agrees."""
    consensus_t: float | None
    """Median position over the stable region."""
    alternatives: tuple[tuple[float, int], ...]
    """``(t, number of sizes)`` for other positions, clearly apart from ``consensus_t``, that
    several sizes agree on."""

    def index(self, disc: Disc) -> tuple[int, int]:
        """``[width, height]`` indexes of the scanned size nearest ``disc``."""
        i = int(np.argmin(np.abs(np.log(np.array(self.widths) / disc.width))))
        j = int(np.argmin(np.abs(np.log(np.array(self.heights) / disc.height))))
        return i, j

    def holds(self, disc: Disc) -> tuple[tuple[float, float], tuple[float, float]] | None:
        """The widths (at ``disc``'s height) and heights (at its width) over which the reading
        stays put, or None if ``disc`` (the nearest scanned size) isn't in the stable region.
        """
        i, j = self.index(disc)
        if not self.stable[i, j]:
            return None
        (wl, wh), (hl, hh) = _run(self.stable[:, j], i), _run(self.stable[i, :], j)
        return (self.widths[wl], self.widths[wh]), (self.heights[hl], self.heights[hh])

    def at_edge(self, disc: Disc) -> bool:
        """Whether a size one step away from ``disc`` already reads elsewhere (inside the grid)."""
        i, j = self.index(disc)
        if not self.stable[i, j]:
            return True
        last = len(self.widths) - 1, len(self.heights) - 1
        neighbours = [(i - 1, j), (i + 1, j), (i, j - 1), (i, j + 1)]
        return any(
            0 <= a <= last[0] and 0 <= b <= last[1] and not self.stable[a, b] for a, b in neighbours
        )


def _run(line: NDArray[np.bool_], k: int) -> tuple[int, int]:
    """The ends of the run of True values in ``line`` that contains index ``k``."""
    lo = hi = k
    while lo > 0 and line[lo - 1]:
        lo -= 1
    while hi < len(line) - 1 and line[hi + 1]:
        hi += 1
    return lo, hi


def _mode(values: NDArray[np.float64], tol: float) -> float:
    """The value with the most others within ``tol`` of it."""
    counts = [int((np.abs(values - v) <= tol).sum()) for v in values]
    return float(values[int(np.argmax(counts))])


def scan_disc_sizes(
    frames: Sequence[Frame],
    calibration: Calibration,
    aligner: Aligner,
    *,
    centre: Disc | None = None,
    steps: int = 9,
    span: float = 1.6,
    tolerance_px: float = 2.5,
    min_confidence: float = 0.8,
) -> DiscScan:
    """Read ``frames`` with disc sizes from ``centre / span`` to ``centre * span`` in each axis.

    ``centre`` (by default the calibrated size) is the starting size. ``steps`` should be odd so
    it is one of the sizes tried. Raises :class:`ValueError` if no frame can be aligned.
    """
    centre = centre or calibration.disc
    factors = np.geomspace(1 / span, span, steps)
    widths = tuple(round(float(centre.width * f), 1) for f in factors)
    heights = tuple(round(float(centre.height * f), 1) for f in factors)

    strips = []
    for frame in frames:
        calibration.check_frame_size(frame.size)
        alignment = aligner.locate(frame.gray)
        if alignment.ok:
            strips.append(rectify(frame.gray, calibration.axis, alignment))
    if not strips:
        raise ValueError("none of the frames could be aligned to the anchor")
    majority = len(strips) // 2 + 1
    floor = BurstParams().min_frame_confidence

    t = np.full((steps, steps), np.nan)
    confidence = np.zeros((steps, steps))
    for i, w in enumerate(widths):
        for j, h in enumerate(heights):
            reader = ClassicalReader(Disc(width=w, height=h))
            readings = [reader.read(s) for s in strips]
            good = [r for r in readings if r.t is not None and r.confidence >= floor]
            if len(good) >= majority:
                t[i, j] = float(np.median([r.t for r in good if r.t is not None]))
                confidence[i, j] = float(np.median([r.confidence for r in good]))

    valid = ~np.isnan(t) & (confidence >= min_confidence)
    tt = np.nan_to_num(t, nan=np.inf)
    tol = tolerance_px / calibration.axis.length

    def region(position: float, seed: tuple[int, int] | None) -> NDArray[np.bool_]:
        """Sizes that agree on ``position``: the region containing ``seed``, else the largest."""
        agree = valid & (np.abs(tt - position) <= tol)
        n, labels = cv2.connectedComponents(agree.astype(np.uint8), connectivity=4)
        if n < 2:
            return np.zeros_like(valid)
        if seed is not None and labels[seed]:
            return np.asarray(labels == labels[seed])
        sizes = [int((labels == k).sum()) for k in range(1, n)]
        return np.asarray(labels == 1 + int(np.argmax(sizes)))

    c = (steps // 2, steps // 2)
    if valid[c]:
        stable = region(float(t[c]), c)
    elif valid.any():
        stable = region(_mode(t[valid], tol), None)
    else:
        stable = np.zeros_like(valid)

    best = consensus = None
    if stable.any():
        # The size deepest inside the region, furthest from where the reading breaks down.
        padded = np.pad(stable.astype(np.uint8), 1)
        depth = np.asarray(cv2.distanceTransform(padded, cv2.DIST_L2, 3))[1:-1, 1:-1]
        ii, jj = np.nonzero(depth == depth[stable].max())
        ci, cj = np.nonzero(stable)
        k = int(np.argmin((ii - ci.mean()) ** 2 + (jj - cj.mean()) ** 2))
        best = Disc(width=widths[ii[k]], height=heights[jj[k]])
        consensus = float(np.median(t[stable]))

    # Other positions that several sizes agree on, clearly apart from this one.
    alternatives: list[tuple[float, int]] = []
    rest = valid & ~stable
    if consensus is not None:
        rest &= np.abs(tt - consensus) > 2 * tol
    while rest.any() and len(alternatives) < 3:
        mode = _mode(t[rest], tol)
        members = rest & (np.abs(tt - mode) <= tol)
        if members.sum() < 3:
            break
        alternatives.append((float(np.median(t[members])), int(members.sum())))
        rest &= ~members
    return DiscScan(widths, heights, t, confidence, stable, best, consensus, tuple(alternatives))


# ---- rebase ------------------------------------------------------------------------------------


REBASE_MIN_SCORE = 0.9
"""Weakest anchor match ``rebase`` accepts. Over a wide search a similar-looking patch elsewhere
can reach 0.6-0.8 (a mirrored view of the samples scores 0.77); the same view, bumped, at night,
blurred or noisy, scores 0.96 or more."""


def find_shift(
    calibration: Calibration, gray: GrayImage, search: int, min_score: float = REBASE_MIN_SCORE
) -> Alignment:
    """Where the anchor is in ``gray``, searching ``search`` px around its calibrated place.

    The result is only ``ok`` if the match scores at least ``min_score`` (or the anchor's own,
    if higher).
    """
    anchor = calibration.anchor.updated(
        search=search, min_score=max(min_score, calibration.anchor.min_score)
    )
    return Aligner(anchor, calibration.anchor_patch).locate(gray)


def shifted(calibration: Calibration, dx: float, dy: float, reference: Path) -> Calibration:
    """The calibration moved by ``(dx, dy)`` frame px, with a new reference frame.

    The axis, anchor and ``at`` points move; ``t`` points are relative to the axis and follow it.
    Raises :class:`pydantic.ValidationError` if the anchor would leave the frame.
    """
    ax, an = calibration.axis, calibration.anchor
    x, y, w, h = an.rect
    return calibration.updated(
        reference_image=reference,
        axis=ax.updated(
            top=(ax.top[0] + dx, ax.top[1] + dy), bottom=(ax.bottom[0] + dx, ax.bottom[1] + dy)
        ),
        anchor=an.updated(rect=(round(x + dx), round(y + dy), w, h)),
        points=[
            p.updated(at=(p.at[0] + dx, p.at[1] + dy)) if p.at is not None else p
            for p in calibration.points
        ],
    )


# ---- points ------------------------------------------------------------------------------------


def dated_note(text: str, today: date) -> str:
    """A point's note, starting with the day it was recorded."""
    day = today.isoformat()
    return f"{day}: {text}" if text else day


def with_point(
    calibration: Calibration, point: CalibrationPoint
) -> tuple[Calibration, list[CalibrationPoint]]:
    """Add ``point``, replacing any point with the same reading. Returns the replaced points.

    Raises :class:`pydantic.ValidationError` if the points would no longer increase together.
    """
    replaced = [p for p in calibration.points if abs(p.fraction - point.fraction) < SAME_POINT]
    kept = [p for p in calibration.points if p not in replaced]
    return calibration.updated(points=[*kept, point]), replaced


def without_point(
    calibration: Calibration, fraction: float
) -> tuple[Calibration, list[CalibrationPoint]]:
    """Remove the points with reading ``fraction``. Returns the removed points."""
    removed = [p for p in calibration.points if abs(p.fraction - fraction) < SAME_POINT]
    kept = [p for p in calibration.points if p not in removed]
    return calibration.updated(points=kept), removed


# ---- warnings ----------------------------------------------------------------------------------


def warnings(calibration: Calibration) -> list[str]:
    """Things about a calibration that will make readings fail or mislead."""
    ax = calibration.axis
    out = []
    lo, hi = -ax.margin, 1.0 + ax.margin
    ends = {name: calibration.t_for(f) for f, name in ((0.0, "E (0%)"), (1.0, "F (100%)"))}
    for name, t in ends.items():
        if not lo <= t <= hi:
            out.append(
                f"{name} is outside the area the reader searches (t = {t:.3f}); the disc can't be "
                "found there. Extend the axis, or check the calibration points"
            )
    span = abs(ends["F (100%)"] - ends["E (0%)"]) * ax.length
    if span < BurstParams().min_travel_px:
        out.append(
            f"E to F spans only {span:.0f} px, so each px is {100 / span:.1f}% of the gauge; "
            "use a higher-resolution stream"
        )
    if 2 * ax.half_width < 1.2 * calibration.disc.width:
        out.append(
            f"the strip ({2 * ax.half_width:.0f} px wide) is barely wider than the disc "
            f"({calibration.disc.width:.0f} px); increase the axis half-width"
        )
    return out
