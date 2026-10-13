"""The MQTT connection to Home Assistant's broker (paho-mqtt 2.x).

paho runs its network loop in a background thread and calls back from it. The callbacks here do
only cheap MQTT work (subscribe, announce availability and discovery) and put an :class:`Event` on
a queue. Reading the gauge, which is slow CV work, happens in whoever consumes that queue.
"""

import json
import logging
import queue
import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion, MQTTErrorCode
from paho.mqtt.reasoncodes import ReasonCode

from oiltank.config import Settings
from oiltank.model import LevelState
from oiltank.publish.discovery import (
    OFFLINE,
    ONLINE,
    PRESS,
    Topics,
    discovery_payload,
    state_payload,
)

log = logging.getLogger(__name__)

QOS = 1
PUBLISH_TIMEOUT_S = 10.0


class MqttError(Exception):
    """Couldn't connect to, or publish through, the broker."""


class EventKind(StrEnum):
    CONNECTED = "connected"
    HA_ONLINE = "ha_online"
    """Home Assistant (re)started: republish discovery and state."""
    READ_NOW = "read_now"
    """The Read now button was pressed."""
    STOP = "stop"
    """Not from MQTT: the process was asked to stop (SIGTERM), so the loop should wake and end."""


@dataclass(frozen=True, slots=True)
class Event:
    kind: EventKind


class MqttLink:
    """Connects with a last will (``offline``), announces ``online`` and discovery on every
    (re)connect, and turns button presses and Home Assistant restarts into events.
    """

    def __init__(
        self,
        settings: Settings,
        events: queue.Queue[Event],
        *,
        client_id: str | None = None,
        announce: bool = True,
    ) -> None:
        """``announce=False`` skips availability and discovery on connect (e.g. for ``clear``)."""
        cfg = settings.mqtt
        if not cfg.host:
            raise MqttError("no MQTT broker: set OILTANK_MQTT_HOST (see .env.example)")
        self._host, self._port = cfg.host, cfg.port
        self._settings = settings
        self._events = events
        self.topics = Topics.from_settings(settings)
        self._announce = announce
        self._connected = threading.Event()
        self._failure: str | None = None

        self._client = mqtt.Client(
            CallbackAPIVersion.VERSION2,
            client_id=client_id or cfg.client_id,
            protocol=mqtt.MQTTv311,
        )
        if cfg.username:
            password = cfg.password.get_secret_value() if cfg.password else None
            self._client.username_pw_set(cfg.username, password)
        if cfg.tls:
            self._client.tls_set()
        self._client.will_set(self.topics.availability, OFFLINE, qos=QOS, retain=True)
        self._client.reconnect_delay_set(min_delay=1, max_delay=60)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._client.on_subscribe = self._on_subscribe
        self._subscription: int | None = None  # mid of the pending SUBSCRIBE

    # ---- lifecycle -----------------------------------------------------------------------------

    def start(self, timeout_s: float = 15.0, *, keep_trying: bool = False) -> bool:
        """Connect, and wait until the broker accepts us and our subscriptions (so a Read now
        press can't slip through before them). Reconnects happen in the background.

        Raises :class:`MqttError` if that takes longer than ``timeout_s``, unless ``keep_trying``:
        then it returns False and paho keeps trying in the background (for the service).
        """
        try:
            self._client.connect_async(self._host, self._port, keepalive=60)
        except (OSError, ValueError) as e:
            raise MqttError(f"cannot connect to {self._host}:{self._port}: {e}") from e
        self._client.loop_start()
        if self._connected.wait(timeout_s):
            return True
        problem = (
            self._failure or f"no answer from {self._host}:{self._port} within {timeout_s:g} s"
        )
        if keep_trying:
            log.warning("%s; still trying", problem)
            return False
        self._client.loop_stop()
        raise MqttError(problem)

    @property
    def connected(self) -> bool:
        return self._client.is_connected()

    def stop(self, *, offline: bool = True) -> None:
        """Say ``offline`` (unless told not to) and disconnect cleanly."""
        if offline and self._announce and self._connected.is_set():
            self._publish(self.topics.availability, OFFLINE, wait=True)
        self._client.disconnect()
        self._client.loop_stop()

    # ---- paho callbacks (network thread): cheap MQTT work and enqueueing only ------------------

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: mqtt.ConnectFlags,
        reason_code: ReasonCode,
        properties: Any,
    ) -> None:
        if reason_code.is_failure:
            self._failure = f"{self._host}:{self._port} refused the connection: {reason_code}"
            if str(reason_code) in ("Not authorized", "Bad user name or password"):
                self._failure += " (check OILTANK_MQTT_USERNAME and OILTANK_MQTT_PASSWORD)"
            log.error("%s", self._failure)
            return
        log.info("connected to %s:%d", self._host, self._port)
        if not self._announce:
            self._ready()
            return
        _, self._subscription = client.subscribe(
            [(self.topics.command, QOS), (self.topics.ha_status, QOS)]
        )
        self.publish_discovery()
        self._publish(self.topics.availability, ONLINE)

    def _on_subscribe(
        self,
        client: mqtt.Client,
        userdata: Any,
        mid: int,
        reason_codes: list[ReasonCode],
        properties: Any,
    ) -> None:
        if mid != self._subscription:
            return
        if any(rc.is_failure for rc in reason_codes):
            log.error("the broker refused our subscriptions: %s", reason_codes)
        self._ready()

    def _ready(self) -> None:
        """Connected, and (when announcing) subscribed: commands will reach us from now on."""
        self._connected.set()
        self._events.put(Event(EventKind.CONNECTED))

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: mqtt.DisconnectFlags,
        reason_code: ReasonCode,
        properties: Any,
    ) -> None:
        if reason_code.is_failure:
            log.warning("disconnected from %s (%s); reconnecting", self._host, reason_code)
            self._connected.clear()

    def _on_message(self, client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage) -> None:
        payload = msg.payload.decode(errors="replace").strip()
        if msg.topic == self.topics.command and payload == PRESS:
            log.info("Read now pressed")
            self._events.put(Event(EventKind.READ_NOW))
        elif msg.topic == self.topics.ha_status and payload == ONLINE:
            log.info("Home Assistant came online")
            self._events.put(Event(EventKind.HA_ONLINE))

    # ---- publishing ----------------------------------------------------------------------------

    def _publish(self, topic: str, payload: str | bytes, *, wait: bool = False) -> None:
        info = self._client.publish(topic, payload, qos=QOS, retain=True)
        if info.rc != MQTTErrorCode.MQTT_ERR_SUCCESS:
            raise MqttError(f"publishing to {topic} failed: {mqtt.error_string(info.rc)}")
        if wait:
            try:
                info.wait_for_publish(PUBLISH_TIMEOUT_S)
            except (RuntimeError, ValueError) as e:
                raise MqttError(f"publishing to {topic} failed: {e}") from e
            if not info.is_published():
                raise MqttError(f"publishing to {topic} timed out")

    def publish_discovery(self) -> None:
        payload = json.dumps(discovery_payload(self._settings), separators=(",", ":"))
        self._publish(self.topics.discovery, payload)

    def publish_availability(self, online: bool) -> None:
        self._publish(self.topics.availability, ONLINE if online else OFFLINE, wait=True)

    def publish_state(self, state: LevelState) -> None:
        self._publish(self.topics.state, json.dumps(state_payload(state)), wait=True)

    def publish_image(self, jpeg: bytes) -> None:
        self._publish(self.topics.image, jpeg, wait=True)

    def clear(self) -> None:
        """Remove the device from Home Assistant and every retained message under our topics."""
        for topic in (self.topics.discovery, self.topics.state, self.topics.image):
            self._publish(topic, b"", wait=True)
        self._publish(self.topics.availability, b"", wait=True)
