"""Core data types passed between pipeline stages."""

from dataclasses import dataclass
from datetime import datetime

import numpy as np
from numpy.typing import NDArray

type GrayImage = NDArray[np.uint8]


@dataclass(frozen=True, slots=True)
class Frame:
    """A single decoded camera frame, in grayscale (the camera is always in IR mode)."""

    gray: GrayImage
    ts: datetime
    source: str

    @property
    def size(self) -> tuple[int, int]:
        """Frame size as ``(width, height)``."""
        h, w = self.gray.shape
        return w, h
