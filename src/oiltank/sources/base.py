"""The frame-source interface."""

from typing import Protocol

from oiltank.model import Frame


class CameraError(Exception):
    """No usable frame could be fetched (camera or go2rtc unreachable, or bad responses)."""


class CameraConfigError(CameraError):
    """The camera answered, but in a way retrying won't fix: wrong URL, credentials or content."""


class FrameSource(Protocol):
    def grab(self, n: int, spacing_s: float) -> list[Frame]:
        """Up to ``n`` distinct frames, ``spacing_s`` apart; at least one, else CameraError."""
        ...
