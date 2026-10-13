"""Render annotated crops of the gauge: what the reader saw and what it concluded.

:func:`render` is the compact image published to Home Assistant. :func:`render_diagnostic` is a
larger view for checking a calibration: it outlines the disc where the calibration says 0% (E) and
100% (F) are, alongside the detected disc, the area the reader searches and the alignment anchor.
"""

from dataclasses import dataclass

import cv2
import numpy as np

from oiltank.calibration import Calibration
from oiltank.model import GrayImage, Measurement, Point

_GREEN = (80, 220, 80)
_RED = (60, 60, 230)
_AMBER = (40, 190, 250)
_GREY = (200, 200, 200)
_CYAN = (230, 210, 60)
_MAGENTA = (210, 90, 210)
_WHITE = (255, 255, 255)
_FONT = cv2.FONT_HERSHEY_SIMPLEX

MAX_JPEG_BYTES = 50_000
SCALE_MARKS = (0.0, 0.25, 0.5, 0.75, 1.0)
"""Gauge readings marked on the images, placed through the calibration."""
_HEADER_PX = 64
_OUTLINE_OFFSETS = [(dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy]


@dataclass(frozen=True, slots=True)
class _View:
    """Maps frame coordinates into a scaled crop. Calibration geometry is in reference-frame
    coordinates, so it is moved by the camera shift (``dx``, ``dy``) unless ``aligned=False``.
    """

    x0: int
    y0: int
    scale: float
    dx: float = 0.0
    dy: float = 0.0

    def xy(self, x: float, y: float, *, aligned: bool = True) -> tuple[float, float]:
        ox, oy = (self.dx, self.dy) if aligned else (0.0, 0.0)
        return (x + ox - self.x0) * self.scale, (y + oy - self.y0) * self.scale

    def px(self, x: float, y: float, *, aligned: bool = True) -> tuple[int, int]:
        fx, fy = self.xy(x, y, aligned=aligned)
        return round(fx), round(fy)


def _crop_box(calibration: Calibration, frame_size: tuple[int, int]) -> tuple[int, int, int, int]:
    """A box around the gauge, padded by one strip length on each side, clipped to the frame."""
    ax = calibration.axis
    pad = ax.length * (0.5 + ax.margin)
    xs = (ax.top[0], ax.bottom[0])
    ys = (ax.top[1], ax.bottom[1])
    w, h = frame_size
    x0 = int(max(0, min(xs) - ax.half_width - pad))
    x1 = int(min(w, max(xs) + ax.half_width + pad))
    y0 = int(max(0, min(ys) - pad))
    y1 = int(min(h, max(ys) + pad))
    return x0, y0, x1, y1


def _camera_shift(measurement: Measurement | None) -> tuple[float, float]:
    """Median shift of the aligned frames, to move calibration geometry onto this frame."""
    aligned = [r.alignment for r in measurement.frames if r.alignment.ok] if measurement else []
    if not aligned:
        return 0.0, 0.0
    return float(np.median([a.dx for a in aligned])), float(np.median([a.dy for a in aligned]))


def _status_colour(measurement: Measurement) -> tuple[int, int, int]:
    if not measurement.ok:
        return _RED
    return _GREEN if measurement.confidence >= 0.5 else _AMBER


def _header(
    width: int, measurement: Measurement | None, capacity: float | None, unit: str
) -> np.ndarray:
    header = np.zeros((_HEADER_PX, width, 3), np.uint8)
    if measurement is None:
        cv2.putText(header, "calibration", (8, 28), _FONT, 0.8, _GREY, 2, cv2.LINE_AA)
        return header
    if measurement.ok and measurement.fraction is not None:
        text = f"{measurement.fraction:.0%}"
        if capacity is not None:
            text += f"  {measurement.fraction * capacity:.0f} {unit}"
        detail = f"conf {measurement.confidence:.2f}  t={measurement.t:.3f}"
    else:
        text = str(measurement.problem or "no reading")
        detail = f"conf {measurement.confidence:.2f}"
    stamp = measurement.ts.strftime("%Y-%m-%d %H:%M")
    colour = _status_colour(measurement)
    cv2.putText(header, text, (8, 28), _FONT, 0.8, colour, 2, cv2.LINE_AA)
    cv2.putText(header, f"{detail}   {stamp}", (8, 52), _FONT, 0.45, _GREY, 1, cv2.LINE_AA)
    return header


def _crop(gray: GrayImage, box: tuple[int, int, int, int], scale: float) -> np.ndarray:
    x0, y0, x1, y1 = box
    crop = cv2.cvtColor(gray[y0:y1, x0:x1], cv2.COLOR_GRAY2BGR)
    return np.asarray(cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC))


def _text(
    img: np.ndarray,
    text: str,
    org: tuple[int, int],
    colour: tuple[int, int, int],
    size: float = 0.45,
    thickness: int = 1,
) -> None:
    """Text with a dark outline, so it stays readable over the bright wall.

    The outline is the text drawn offset in each direction. A thicker stroke would be simpler,
    but OpenCV 5's fonts change their letter spacing with thickness.
    """
    x, y = org
    for ox, oy in _OUTLINE_OFFSETS:
        cv2.putText(img, text, (x + ox, y + oy), _FONT, size, (0, 0, 0), thickness, cv2.LINE_AA)
    cv2.putText(img, text, org, _FONT, size, colour, thickness, cv2.LINE_AA)


def _scale_ticks(img: np.ndarray, view: _View, calibration: Calibration) -> None:
    """Short ticks across the axis at each of :data:`SCALE_MARKS`, labelled with the reading.

    Labels sit left of the tube, clear of the indicator line and the disc outlines.
    """
    ax = calibration.axis
    tick = ax.half_width * 0.4
    gap = max(ax.half_width, calibration.disc.width / 2) + 4
    for fraction in SCALE_MARKS:
        x, y = ax.point(calibration.t_for(fraction))
        cv2.line(img, view.px(x - tick, y), view.px(x + tick, y), _GREY, 1, cv2.LINE_AA)
        label = f"{fraction:.0%}"
        (tw, th), _ = cv2.getTextSize(label, _FONT, 0.45, 1)
        lx, ly = view.px(x - gap, y)
        _text(img, label, (lx - tw, ly + th // 2), _GREY)


def _signed(v: float) -> str:
    return f"{v:+.1f}".replace("-0.0", "+0.0")


def render(
    gray: GrayImage,
    measurement: Measurement,
    calibration: Calibration,
    *,
    capacity: float | None = None,
    unit: str = "gal",
    scale: float = 2.0,
) -> np.ndarray:
    """Draw the axis, scale marks and detected indicator on a crop of ``gray`` (BGR out)."""
    box = _crop_box(calibration, (gray.shape[1], gray.shape[0]))
    img = _crop(gray, box, scale)
    view = _View(box[0], box[1], scale, *_camera_shift(measurement))

    ax = calibration.axis
    cv2.line(img, view.px(*ax.top), view.px(*ax.bottom), _GREY, 1, cv2.LINE_AA)
    _scale_ticks(img, view, calibration)

    if measurement.indicator is not None:
        # Already in this frame's coordinates (the reader worked on the aligned strip).
        x, y = measurement.indicator
        half = ax.half_width
        left = view.px(x - half, y, aligned=False)
        right = view.px(x + half, y, aligned=False)
        cv2.line(img, left, right, _status_colour(measurement), 2, cv2.LINE_AA)

    return np.vstack([_header(img.shape[1], measurement, capacity, unit), img])


def _disc_box(
    view: _View, calibration: Calibration, centre: Point, *, aligned: bool = True
) -> tuple[tuple[float, float], tuple[float, float], float]:
    """A ``cv2.ellipse`` rotated box for the disc outline centred on ``centre``."""
    ax = calibration.axis
    (tx, ty), (bx, by) = ax.top, ax.bottom
    angle = float(np.degrees(np.arctan2(tx - bx, by - ty)))  # 0 when the axis is vertical
    size = (calibration.disc.width * view.scale, calibration.disc.height * view.scale)
    return view.xy(*centre, aligned=aligned), size, angle


def render_diagnostic(
    gray: GrayImage,
    calibration: Calibration,
    measurement: Measurement | None = None,
    *,
    height: int = 900,
    capacity: float | None = None,
    unit: str = "gal",
) -> np.ndarray:
    """A large view for checking a calibration (BGR out, about ``height`` px of image).

    Shows, in frame terms:

    - cyan disc outlines where the calibration puts 0% (E) and 100% (F), with scale marks between;
    - white dots at the calibration points (readings checked by eye, and defaults);
    - the detected disc (green/amber/red by confidence) when ``measurement`` is given;
    - the grey box the reader searches, and the magenta anchor patch used for alignment.

    If the 0% and 100% outlines don't sit where the disc would rest when the tank is empty and
    full, the calibration needs another checked point.
    """
    ax = calibration.axis
    fw, fh = gray.shape[1], gray.shape[0]
    gx0, gy0, gx1, gy1 = _crop_box(calibration, (fw, fh))
    rx, ry, rw, rh = calibration.anchor.rect
    pad = 16
    box = (
        max(0, min(gx0, rx - pad)),
        max(0, min(gy0, ry - pad)),
        min(fw, max(gx1, rx + rw + pad)),
        min(fh, max(gy1, ry + rh + pad)),
    )
    scale = height / (box[3] - box[1])
    img = _crop(gray, box, scale)
    dx, dy = _camera_shift(measurement)
    view = _View(box[0], box[1], scale, dx, dy)
    line = cv2.LINE_AA

    # Reader search area: the rectified strip's footprint in the frame.
    lo, hi = -ax.margin, 1.0 + ax.margin
    (tx, ty), (bx, by) = ax.top, ax.bottom
    ux, uy = (bx - tx) / ax.length, (by - ty) / ax.length
    nx, ny = -uy * ax.half_width, ux * ax.half_width
    corners = [
        view.px(px + sx * nx, py + sx * ny)
        for (px, py), sx in (
            (ax.point(hi), -1),
            (ax.point(hi), 1),
            (ax.point(lo), 1),
            (ax.point(lo), -1),
        )
    ]
    cv2.polylines(img, [np.array(corners, np.int32)], True, _GREY, 1, line)

    # Anchor patch, where the aligner found it in this frame.
    cv2.rectangle(img, view.px(rx, ry), view.px(rx + rw, ry + rh), _MAGENTA, 2, line)
    anchor_label = "anchor"
    if measurement is not None:
        scores = [r.alignment.score for r in measurement.frames]
        anchor_label += f"  score {np.median(scores):.2f}  shift {_signed(dx)},{_signed(dy)} px"
    lx, ly = view.px(rx, ry + rh)
    _text(img, anchor_label, (lx + 6, ly - 8), _MAGENTA)

    cv2.line(img, view.px(*ax.top), view.px(*ax.bottom), _GREY, 1, line)
    _scale_ticks(img, view, calibration)

    # The calibrated ends of the scale, drawn as the disc would appear there.
    for fraction, name in ((1.0, "F"), (0.0, "E")):
        t = calibration.t_for(fraction)
        centre = ax.point(t)
        cv2.ellipse(img, _disc_box(view, calibration, centre), _CYAN, 2, line)
        ex, ey = view.px(centre[0] + calibration.disc.width / 2, centre[1])
        label = f"{fraction:.0%} {name}  t={t:.3f}"
        _text(img, label, (ex + 8, ey + 5), _CYAN)

    for point in calibration.points:
        cv2.circle(img, view.px(*ax.point(point.t_on(ax))), 4, _WHITE, -1, line)

    if measurement is not None and measurement.indicator is not None:
        colour = _status_colour(measurement)
        cv2.ellipse(
            img, _disc_box(view, calibration, measurement.indicator, aligned=False), colour, 1, line
        )
        cx, cy = view.px(*measurement.indicator, aligned=False)
        cv2.drawMarker(img, (cx, cy), colour, cv2.MARKER_CROSS, 12, 1, line)

    legend = (
        "cyan: disc at calibrated 0% / 100%   white: calibration points",
        "green: detected disc   grey: reader search area   magenta: anchor",
    )
    footer = np.zeros((14 + 20 * len(legend), img.shape[1], 3), np.uint8)
    for i, text in enumerate(legend):
        cv2.putText(footer, text, (8, 22 + 20 * i), _FONT, 0.45, _GREY, 1, line)
    return np.vstack([_header(img.shape[1], measurement, capacity, unit), img, footer])


def encode_jpeg(image: np.ndarray, max_bytes: int = MAX_JPEG_BYTES) -> bytes:
    """JPEG-encode, lowering quality (then size) until the result fits in ``max_bytes``."""
    img = image
    while True:
        for quality in (85, 75, 65, 55, 45):
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
            if not ok:
                raise ValueError("JPEG encoding failed")
            if len(buf) <= max_bytes:
                return bytes(buf)
        if min(img.shape[:2]) < 64:
            return bytes(buf)
        img = cv2.resize(img, None, fx=0.75, fy=0.75, interpolation=cv2.INTER_AREA)
