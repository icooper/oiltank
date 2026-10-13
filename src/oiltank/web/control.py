"""What the web UI can see of the service, and ask of it.

:class:`ServiceControl` is for the UI served by ``oiltank run``: it reads the service's snapshot,
puts events on its queue, and saves calibrations through the service. :class:`FileControl` is for
``oiltank web`` on its own, which can only see the files of a service running elsewhere.

Either way, the calibration *file* is what the UI edits: :class:`CalibrationFile` loads it with
its tag, and a save is refused if the file has changed since (e.g. by ``oiltank calibrate``).
"""

import json
import logging
import queue
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from oiltank.calibration import Calibration, CalibrationError, calibration_etag, save_reference
from oiltank.epochs import AxisEpoch, successor
from oiltank.events import Event, EventKind
from oiltank.geometry import Aligner
from oiltank.history import History
from oiltank.model import LevelState
from oiltank.service import HEALTHY_AGE_S, CalibrationConflictError, Service
from oiltank.sources.files import FrameDecodeError
from oiltank.web.schemas import ServiceInfo

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Current:
    """The calibration readings are shown with, and the axis epoch stored ``t`` is carried onto."""

    calibration: Calibration
    epoch: AxisEpoch


class CalibrationFile:
    """The calibration file as it is now, with its tag (cached until the file changes)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._loaded: tuple[tuple[int, int], Calibration, str] | None = None

    def load(self) -> tuple[Calibration, str] | None:
        """``(calibration, tag)``, or None if there's no usable calibration."""
        try:
            stat = self.path.stat()
            key = (stat.st_mtime_ns, stat.st_size)
            if self._loaded is None or self._loaded[0] != key:
                tag = calibration_etag(self.path)
                self._loaded = (key, Calibration.load(self.path), tag)
        except (OSError, CalibrationError, ValidationError) as e:
            log.debug("no usable calibration at %s: %s", self.path, e)
            return None
        return self._loaded[1], self._loaded[2]


class Control(Protocol):
    def current(self) -> Current | None:
        """The calibration readings are shown with. None if there's no usable calibration."""
        ...

    def calibration_file(self) -> CalibrationFile: ...
    def save_calibration(
        self,
        calibration: Calibration,
        *,
        if_match: str,
        read_now: bool,
        reference: bytes | None = None,
    ) -> str:
        """Save ``calibration`` (with ``reference`` as its new reference frame) and use it.
        Returns the file's new tag. Raises CalibrationConflictError if the file isn't the one
        ``if_match`` names any more, and CalibrationError if it can't be used."""
        ...

    def service_info(self) -> ServiceInfo: ...
    def state(self) -> LevelState | None: ...
    def gauge_image(self) -> bytes | None: ...
    def archive_size(self) -> int | None: ...
    def read_now(self) -> bool:
        """Ask for a reading; False if there's no service here to ask."""
        ...


class ServiceControl:
    def __init__(self, service: Service, events: queue.Queue[Event]) -> None:
        self._service = service
        self._events = events
        self._file = CalibrationFile(service.calibration_path)

    def current(self) -> Current:
        snap = self._service.snapshot
        return Current(snap.calibration, snap.epoch)

    def calibration_file(self) -> CalibrationFile:
        return self._file

    def save_calibration(
        self,
        calibration: Calibration,
        *,
        if_match: str,
        read_now: bool,
        reference: bytes | None = None,
    ) -> str:
        """Through the service's own thread, which saves it, switches to it and republishes."""
        snap = self._service.call(
            lambda s: s.apply_calibration(calibration, if_match=if_match, reference=reference)
        )
        if read_now:
            self.read_now()
        return snap.calibration_etag

    def service_info(self) -> ServiceInfo:
        snap = self._service.snapshot
        return ServiceInfo(
            here=True,
            alive=True,
            reading=snap.reading,
            next_reading=snap.next_reading,
            mqtt_connected=snap.mqtt_connected,
            last_reading=None if snap.state is None else snap.state.ts,
        )

    def state(self) -> LevelState | None:
        return self._service.snapshot.state

    def gauge_image(self) -> bytes | None:
        return self._service.snapshot.image

    def archive_size(self) -> int | None:
        return self._service.snapshot.archive_bytes

    def read_now(self) -> bool:
        self._events.put(Event(EventKind.READ_NOW))
        return True


class FileControl:
    def __init__(self, data_dir: Path, calibration_path: Path) -> None:
        self._heartbeat = data_dir / "heartbeat"
        self._history = data_dir / "oiltank.db"
        self._file = CalibrationFile(calibration_path)

    def calibration_file(self) -> CalibrationFile:
        return self._file

    def save_calibration(
        self,
        calibration: Calibration,
        *,
        if_match: str,
        read_now: bool,
        reference: bytes | None = None,
    ) -> str:
        """Straight to the file; a service running elsewhere picks it up at its next reading."""
        path = self._file.path
        current = calibration_etag(path)
        if current != if_match:
            raise CalibrationConflictError(current)
        if reference is not None:
            try:
                save_reference(path.parent, calibration.reference_image, reference)
            except OSError as e:
                raise CalibrationError(f"cannot write the reference frame: {e}") from e
        try:
            Aligner(calibration.anchor, calibration.anchor_patch)  # the reference must be usable
        except (OSError, FrameDecodeError) as e:
            raise CalibrationError(f"cannot use the reference frame: {e}") from e
        calibration.save(path)
        return calibration_etag(path)

    def current(self) -> Current | None:
        """The calibration file as it is now, and where a service would carry ``t`` to."""
        loaded = self._file.load()
        if loaded is None:
            return None
        calibration = loaded[0]
        reference = calibration.reference_image.as_posix()
        try:
            with History.open_readonly(self._history) as h:
                epoch, _ = h.epoch_for(reference, calibration.axis)
        except OSError, RuntimeError, sqlite3.Error:
            epoch = successor(None, reference, calibration.axis)
        return Current(calibration, epoch)

    def service_info(self) -> ServiceInfo:
        try:
            age = time.time() - self._heartbeat.stat().st_mtime
            beat = json.loads(self._heartbeat.read_text())
        except OSError, ValueError:
            return ServiceInfo(here=False, alive=False)
        last = beat.get("last_reading")
        return ServiceInfo(
            here=False,
            alive=age < HEALTHY_AGE_S,
            mqtt_connected=bool(beat.get("mqtt_connected")),
            last_reading=None if last is None else datetime.fromisoformat(last),
        )

    def state(self) -> LevelState | None:
        return None

    def gauge_image(self) -> bytes | None:
        return None

    def archive_size(self) -> int | None:
        return None

    def read_now(self) -> bool:
        return False
