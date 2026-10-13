"""Frames for the geometry editor: the reference frame, archived full frames, and frames grabbed
from the camera just now. Each is decoded once and kept for a while (a decoded 4K frame is about
8 MB, so only a few).
"""

import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from oiltank.archive import ArchiveReader
from oiltank.model import Frame
from oiltank.sources.base import FrameSource
from oiltank.sources.files import decode_gray
from oiltank.web.control import CalibrationFile

GRAB_TTL_S = 30 * 60.0
KEEP = 6
"""Decoded frames kept (reference and archived ones; grabs are kept until they expire)."""


class FrameRef(BaseModel):
    """Which frame: ``{"kind": "reference"}``, ``{"kind": "archive", "day": ..., "key": ...}`` or
    ``{"kind": "grab", "grab": ..., "index": ...}``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["reference", "archive", "grab"]
    day: str | None = None
    key: str | None = None
    grab: str | None = None
    index: int | None = None


class NoSuchFrame(LookupError):  # noqa: N818 - reads better as a lookup failure
    pass


class FrameStore:
    def __init__(
        self,
        archive: ArchiveReader,
        calibration: CalibrationFile,
        camera: Callable[[], AbstractContextManager[FrameSource]] | None,
    ) -> None:
        """``camera`` opens the camera for a grab (None if there isn't one configured)."""
        self._archive = archive
        self._calibration = calibration
        self._camera = camera
        self._decoded: OrderedDict[str, Frame] = OrderedDict()
        self._grabs: dict[str, tuple[float, list[Frame]]] = {}
        self._lock = threading.Lock()

    @property
    def can_grab(self) -> bool:
        return self._camera is not None

    def get(self, ref: FrameRef) -> Frame:
        """The frame ``ref`` names; raises NoSuchFrame if there isn't one."""
        if ref.kind == "grab":
            return self._grabbed(ref)
        key, load = self._source(ref)
        with self._lock:
            if key in self._decoded:
                self._decoded.move_to_end(key)
                return self._decoded[key]
        frame = load()
        with self._lock:
            self._decoded[key] = frame
            while len(self._decoded) > KEEP:
                self._decoded.popitem(last=False)
        return frame

    def reference_bytes(self) -> tuple[str, bytes]:
        """The reference frame as stored: ``(its name, its bytes)``."""
        loaded = self._calibration.load()
        if loaded is None:
            raise NoSuchFrame("there's no usable calibration")
        cal = loaded[0]
        try:
            return cal.reference_image.as_posix(), cal.reference_path.read_bytes()
        except OSError as e:
            raise NoSuchFrame(f"can't read the reference frame: {e}") from e

    def grab(self, n: int) -> tuple[str, list[Frame]]:
        """``n`` frames from the camera now (CameraError if it can't), kept under a new id."""
        if self._camera is None:
            raise NoSuchFrame("no camera is configured (OILTANK_CAMERA_URL)")
        with self._camera() as source:
            frames = source.grab(n, 0.5)
        grab_id = secrets.token_hex(6)
        now = time.monotonic()
        with self._lock:
            self._grabs = {k: v for k, v in self._grabs.items() if now - v[0] < GRAB_TTL_S}
            self._grabs[grab_id] = (now, frames)
        return grab_id, frames

    def _grabbed(self, ref: FrameRef) -> Frame:
        with self._lock:
            entry = self._grabs.get(ref.grab or "")
        if entry is None or time.monotonic() - entry[0] >= GRAB_TTL_S:
            raise NoSuchFrame("that grab has expired; grab again")
        frames = entry[1]
        if ref.index is None or not 0 <= ref.index < len(frames):
            raise NoSuchFrame("no such frame in that grab")
        return frames[ref.index]

    def _source(self, ref: FrameRef) -> tuple[str, Callable[[], Frame]]:
        if ref.kind == "reference":
            name, data = self.reference_bytes()
            return f"reference:{name}:{len(data)}", lambda: decode_gray(
                data, source=name, ts=datetime.now(UTC)
            )
        record = self._archive.record(ref.day or "", ref.key or "")
        if record is None or record.frame_path is None:
            raise NoSuchFrame("that archive record has no full frame")
        path = record.frame_path
        ts = record.meta.ts
        return f"archive:{ref.day}/{ref.key}", lambda: decode_gray(
            path.read_bytes(), source=str(path), ts=ts
        )

    def all(self, refs: Sequence[FrameRef]) -> list[Frame]:
        return [self.get(ref) for ref in refs]
