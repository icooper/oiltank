"""What wakes the service loop: events that other threads put on its queue.

The MQTT link's callbacks, the signal handlers and the web UI never do the service's work
themselves. They put an :class:`Event` on the queue, and the loop handles it between readings.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class EventKind(StrEnum):
    CONNECTED = "connected"
    HA_ONLINE = "ha_online"
    """Home Assistant (re)started: republish discovery and state."""
    READ_NOW = "read_now"
    """The Read now button was pressed."""
    STOP = "stop"
    """Not from MQTT: the process was asked to stop (SIGTERM), so the loop should wake and end."""
    CALL = "call"
    """Run ``Event.task`` on the service thread (see :meth:`oiltank.service.Service.call`)."""


@dataclass(frozen=True, slots=True)
class Event:
    kind: EventKind
    task: Callable[[], None] | None = None
    """For CALL: what to run."""
