"""Frame alignment (to tolerate small camera shifts) and gauge rectification."""

import cv2
import numpy as np

from oiltank.calibration import Anchor, Axis
from oiltank.model import Alignment, GaugeStrip, GrayImage

STRIP_ROWS = 256
STRIP_COLS = 128


def _parabolic_offset(a: float, b: float, c: float) -> float:
    """Sub-sample offset of the extremum of a parabola through (-1, a), (0, b), (1, c)."""
    d = a - 2.0 * b + c
    if d == 0.0:
        return 0.0
    return float(np.clip(0.5 * (a - c) / d, -0.5, 0.5))


class Aligner:
    """Finds how far a frame has shifted from the calibration reference.

    Matches the anchor patch (static scene, e.g. the gauge fitting) within ``anchor.search`` px of
    its calibrated position using normalized cross-correlation, which is insensitive to IR
    brightness changes. A weak match, or a best match at the edge of the search window, means the
    camera has moved too far to trust.
    """

    def __init__(self, anchor: Anchor, patch: GrayImage) -> None:
        self._anchor = anchor
        self._patch = patch.astype(np.float32)

    def locate(self, gray: GrayImage) -> Alignment:
        x, y, w, h = self._anchor.rect
        s = self._anchor.search
        fh, fw = gray.shape
        x0, y0 = max(0, x - s), max(0, y - s)
        x1, y1 = min(fw, x + w + s), min(fh, y + h + s)
        window = gray[y0:y1, x0:x1].astype(np.float32)
        if window.shape[0] < h or window.shape[1] < w:
            return Alignment(dx=0.0, dy=0.0, score=0.0, ok=False)
        result = np.asarray(
            cv2.matchTemplate(window, self._patch, cv2.TM_CCOEFF_NORMED), dtype=np.float32
        )
        result = np.nan_to_num(result, nan=-1.0)
        py, px = np.unravel_index(int(np.argmax(result)), result.shape)
        score = float(result[py, px])
        rh, rw = result.shape
        on_edge = px in (0, rw - 1) or py in (0, rh - 1)
        fx = _parabolic_offset(*result[py, px - 1 : px + 2]) if 0 < px < rw - 1 else 0.0
        fy = _parabolic_offset(*result[py - 1 : py + 2, px]) if 0 < py < rh - 1 else 0.0
        dx = x0 + px + fx - x
        dy = y0 + py + fy - y
        ok = score >= self._anchor.min_score and not on_edge
        return Alignment(dx=float(dx), dy=float(dy), score=score, ok=ok)


def rectify(
    gray: GrayImage,
    axis: Axis,
    alignment: Alignment | None = None,
    *,
    rows: int = STRIP_ROWS,
    cols: int = STRIP_COLS,
) -> GaugeStrip:
    """Warp the region around the gauge axis into an upright ``rows x cols`` strip.

    The strip runs from ``axis.margin`` (as a fraction of axis length) above the top of travel to
    the same distance below the bottom, so the disc is never clipped at the ends. Columns span
    ``axis.half_width`` px either side of the axis. ``t`` is 1.0 at the top of travel and 0.0 at
    the bottom.
    """
    empty = np.zeros((rows, cols), dtype=np.uint8)
    strip = strip_for(empty, axis, alignment)
    pixels = cv2.warpAffine(
        gray,
        strip.to_frame,
        (cols, rows),
        flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
        borderMode=cv2.BORDER_REPLICATE,
    )
    return GaugeStrip(
        pixels=np.asarray(pixels, dtype=np.uint8),
        t_of_row=strip.t_of_row,
        to_frame=strip.to_frame,
        px_per_row=strip.px_per_row,
        px_per_col=strip.px_per_col,
    )


def strip_for(pixels: GrayImage, axis: Axis, alignment: Alignment | None = None) -> GaugeStrip:
    """``pixels`` (already rectified, e.g. an archived strip) with the geometry :func:`rectify`
    gives a strip of their size along ``axis``: which ``t`` each row is, and where it is in the
    frame."""
    rows, cols = pixels.shape
    offset = np.array([alignment.dx, alignment.dy] if alignment else [0.0, 0.0])
    top = np.array(axis.top, dtype=np.float64) + offset
    bottom = np.array(axis.bottom, dtype=np.float64) + offset
    length = axis.length
    u = (bottom - top) / length  # along the axis, downwards
    n = np.array([-u[1], u[0]])  # across the axis
    if n[0] < 0:  # keep strip columns increasing left-to-right in the frame
        n = -n

    extended = length * (1.0 + 2.0 * axis.margin)
    start = top - axis.margin * length * u
    px_per_row = extended / rows
    px_per_col = 2.0 * axis.half_width / cols

    # Strip (col, row) of a pixel centre -> frame (x, y).
    a = np.column_stack([px_per_col * n, px_per_row * u])
    b = start + 0.5 * px_per_row * u + (0.5 * px_per_col - axis.half_width) * n
    to_frame = np.hstack([a, b[:, None]])

    dist_from_top = (np.arange(rows, dtype=np.float64) + 0.5) * px_per_row - axis.margin * length
    t_of_row = (1.0 - dist_from_top / length).astype(np.float32)
    return GaugeStrip(
        pixels=pixels,
        t_of_row=t_of_row,
        to_frame=to_frame,
        px_per_row=float(px_per_row),
        px_per_col=float(px_per_col),
    )
