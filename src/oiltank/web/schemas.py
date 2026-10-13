"""What the web API sends back."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class ServiceInfo(BaseModel):
    here: bool
    """This process runs the service (``oiltank run``), rather than ``oiltank web`` alone."""
    alive: bool
    """The service's heartbeat is recent."""
    reading: bool | None = None
    """A reading is in progress."""
    next_reading: datetime | None = None
    mqtt_connected: bool | None = None
    last_reading: datetime | None = None


class ArchiveInfo(BaseModel):
    enabled: bool
    size: int | None
    """Bytes used, once the service has archived a reading."""
    max_size: int | None


class Status(BaseModel):
    read_only: bool
    unit: str
    capacity: float
    state: dict[str, Any] | None
    """The state Home Assistant gets (:func:`oiltank.publish.discovery.state_payload`)."""
    service: ServiceInfo
    archive: ArchiveInfo
