"""Load frames from image files on disk (for development, tests, and calibration)."""

from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np

from oiltank.model import Frame


class FrameDecodeError(ValueError):
    """The bytes could not be decoded as an image."""


def decode_gray(data: bytes, *, source: str, ts: datetime | None = None) -> Frame:
    """Decode encoded image bytes (JPEG/PNG) into a grayscale :class:`Frame`."""
    decoded = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
    if decoded is None:
        raise FrameDecodeError(f"could not decode image from {source}")
    gray = np.asarray(decoded, dtype=np.uint8)
    return Frame(gray=gray, ts=ts or datetime.now(UTC), source=source)


def load_frame(path: Path) -> Frame:
    """Load an image file as a grayscale frame, timestamped with the file's mtime."""
    ts = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    return decode_gray(path.read_bytes(), source=str(path), ts=ts)
