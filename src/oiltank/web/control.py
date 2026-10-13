"""What the web UI can see of the service, and ask of it.

:class:`ServiceControl` is for the UI served by ``oiltank run``: it reads the service's snapshot
and puts events on its queue. :class:`FileControl` is for ``oiltank web`` on its own, which can
only see the heartbeat file of a service running elsewhere.
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

from oiltank.calibration import Calibration, CalibrationError
from oiltank.epochs import AxisEpoch, successor
from oiltank.events import Event, EventKind
from oiltank.history import History
from oiltank.model import LevelState
from oiltank.service import HEALTHY_AGE_S, Service
from oiltank.web.schemas import ServiceInfo

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Current:
    """The calibration readings are shown with, and the axis epoch stored ``t`` is carried onto."""

    calibration: Calibration
    epoch: AxisEpoch


class Control(Protocol):
    def current(self) -> Current | None:
        """None if there's no usable calibration."""
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

    def current(self) -> Current:
        snap = self._service.snapshot
        return Current(snap.calibration, snap.epoch)

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
        self._calibration_path = calibration_path
        self._loaded: tuple[int, Calibration] | None = None

    def current(self) -> Current | None:
        """The calibration file as it is now, and where a service would carry ``t`` to."""
        try:
            mtime = self._calibration_path.stat().st_mtime_ns
            if self._loaded is None or self._loaded[0] != mtime:
                self._loaded = (mtime, Calibration.load(self._calibration_path))
        except (OSError, CalibrationError, ValidationError) as e:
            log.debug("no usable calibration at %s: %s", self._calibration_path, e)
            return None
        calibration = self._loaded[1]
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
