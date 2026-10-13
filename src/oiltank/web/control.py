"""What the web UI can see of the service, and ask of it.

:class:`ServiceControl` is for the UI served by ``oiltank run``: it reads the service's snapshot
and puts events on its queue. :class:`FileControl` is for ``oiltank web`` on its own, which can
only see the heartbeat file of a service running elsewhere.
"""

import json
import queue
import time
from datetime import datetime
from pathlib import Path
from typing import Protocol

from oiltank.events import Event, EventKind
from oiltank.model import LevelState
from oiltank.service import HEALTHY_AGE_S, Service
from oiltank.web.schemas import ServiceInfo


class Control(Protocol):
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
    def __init__(self, data_dir: Path) -> None:
        self._heartbeat = data_dir / "heartbeat"

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
