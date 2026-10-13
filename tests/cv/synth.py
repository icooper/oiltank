"""Synthetic variants of the sample frames, for testing the reader beyond the one real level.

All coordinates are for the native-4K sample frames (see ``samples/calibration.yaml``).
"""

from pathlib import Path

import cv2
import numpy as np

from oiltank.calibration import Calibration
from oiltank.model import Frame, GrayImage

DISC_CENTRE = (2168, 1047)
"""Where the disc actually is in the sample frames (frame px)."""
DISC_HALF = (35, 22)
"""Half-extent of the disc's elliptical cut-out (a little larger than the disc)."""

_TUBE_X = (2120, 2212)
_DISC_ROWS = (1018, 1078)
_FILL_OFFSET = 62  # copy tube interior from this many px below to hide the disc


def with_gray(frame: Frame, gray: GrayImage) -> Frame:
    return Frame(gray=gray, ts=frame.ts, source=frame.source + "+synthetic")


def _u8(img: np.ndarray) -> GrayImage:
    return np.asarray(np.clip(img, 0, 255), dtype=np.uint8)


def remove_disc(gray: GrayImage) -> GrayImage:
    """Hide the disc by filling its rows of the tube interior with the tube interior below it."""
    out = gray.astype(np.float32)
    x0, x1 = _TUBE_X
    y0, y1 = _DISC_ROWS
    src = out[y0 + _FILL_OFFSET : y1 + _FILL_OFFSET, x0:x1].copy()
    w = np.ones((y1 - y0, x1 - x0), np.float32)
    ramp_y, ramp_x = np.linspace(0, 1, 6, dtype=np.float32), np.linspace(0, 1, 4, dtype=np.float32)
    w[:6] *= ramp_y[:, None]
    w[-6:] *= ramp_y[::-1, None]
    w[:, :4] *= ramp_x[None]
    w[:, -4:] *= ramp_x[None, ::-1]
    out[y0:y1, x0:x1] = w * src + (1 - w) * out[y0:y1, x0:x1]
    return _u8(out)


def move_disc(gray: GrayImage, new_cy: int) -> GrayImage:
    """Cut the disc out (feathered ellipse), hide it, and paste it centred on row ``new_cy``."""
    cx, cy = DISC_CENTRE
    a, b = DISC_HALF
    pad = 6
    yy, xx = np.mgrid[0 : 2 * (b + pad) + 1, 0 : 2 * (a + pad) + 1].astype(np.float32)
    inside = (((xx - a - pad) / a) ** 2 + ((yy - b - pad) / b) ** 2 <= 1).astype(np.float32)
    alpha = cv2.GaussianBlur(inside, (0, 0), 2.0)
    ys = slice(cy - b - pad, cy + b + pad + 1)
    xs = slice(cx - a - pad, cx + a + pad + 1)
    patch = gray[ys, xs].astype(np.float32)

    out = remove_disc(gray).astype(np.float32)
    ys_new = slice(new_cy - b - pad, new_cy + b + pad + 1)
    out[ys_new, xs] = alpha * patch + (1 - alpha) * out[ys_new, xs]
    return _u8(out)


def shift(gray: GrayImage, dx: float, dy: float) -> GrayImage:
    """Translate the whole frame, as if the camera had been nudged."""
    m = np.array([[1, 0, dx], [0, 1, dy]], dtype=np.float64)
    h, w = gray.shape
    return np.asarray(
        cv2.warpAffine(gray, m, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE),
        dtype=np.uint8,
    )


def add_noise(gray: GrayImage, sigma: float, seed: int = 0) -> GrayImage:
    rng = np.random.default_rng(seed)
    return _u8(gray.astype(np.float32) + rng.normal(0, sigma, gray.shape))


def jpeg(gray: GrayImage, quality: int) -> GrayImage:
    _, buf = cv2.imencode(".jpg", gray, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return np.asarray(cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE), dtype=np.uint8)


def blur(gray: GrayImage, sigma: float) -> GrayImage:
    return np.asarray(cv2.GaussianBlur(gray, (0, 0), sigma), dtype=np.uint8)


def gain(gray: GrayImage, factor: float) -> GrayImage:
    return _u8(gray.astype(np.float32) * factor)


def gamma(gray: GrayImage, g: float) -> GrayImage:
    return _u8(255.0 * (gray.astype(np.float32) / 255.0) ** g)


def downscaled(
    gray: GrayImage, cal: Calibration, size: tuple[int, int], tmp: Path
) -> tuple[GrayImage, Calibration]:
    """A downscaled frame plus a calibration scaled to match (reference image written to tmp)."""
    w, h = size
    k = w / cal.frame_size[0]
    small = np.asarray(cv2.resize(gray, size, interpolation=cv2.INTER_AREA), dtype=np.uint8)
    ref = tmp / f"reference_{w}x{h}.png"
    cv2.imwrite(str(ref), small)
    x, y, aw, ah = cal.anchor.rect
    scaled = cal.model_copy(
        update={
            "reference_image": ref,
            "frame_size": (w, h),
            "axis": cal.axis.model_copy(
                update={
                    "top": (cal.axis.top[0] * k, cal.axis.top[1] * k),
                    "bottom": (cal.axis.bottom[0] * k, cal.axis.bottom[1] * k),
                    "half_width": cal.axis.half_width * k,
                }
            ),
            "disc": cal.disc.model_copy(
                update={"width": cal.disc.width * k, "height": cal.disc.height * k}
            ),
            "anchor": cal.anchor.model_copy(
                update={
                    "rect": (round(x * k), round(y * k), round(aw * k), round(ah * k)),
                    "search": max(4, round(cal.anchor.search * k)),
                }
            ),
        }
    )
    return small, Calibration.model_validate(scaled.model_dump())
