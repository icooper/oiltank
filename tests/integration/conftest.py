"""Fixtures for integration tests: a Mosquitto broker in Docker, and a topic watcher."""

import shutil
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator

import paho.mqtt.client as mqtt
import pytest
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

from oiltank.config import Settings

IMAGE = "eclipse-mosquitto:2.0.20"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _mqtt_ready(port: int, timeout: float = 2.0) -> bool:
    """A whole MQTT handshake works. A TCP connection isn't enough: Docker's port proxy accepts
    those before Mosquitto is listening, and then drops them."""
    connected = threading.Event()

    def on_connect(
        client: mqtt.Client,
        userdata: object,
        flags: mqtt.ConnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        if not reason_code.is_failure:
            connected.set()

    client = mqtt.Client(CallbackAPIVersion.VERSION2, client_id=f"probe-{uuid.uuid4().hex[:6]}")
    client.on_connect = on_connect
    try:
        client.connect("127.0.0.1", port, keepalive=5)
    except OSError:
        return False
    client.loop_start()
    try:
        return connected.wait(timeout)
    finally:
        client.disconnect()
        client.loop_stop()


@pytest.fixture(scope="module")
def broker(tmp_path_factory: pytest.TempPathFactory) -> Iterator[int]:
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    conf = tmp_path_factory.mktemp("mosquitto") / "mosquitto.conf"
    conf.write_text("listener 1883\nallow_anonymous true\n")
    port = free_port()
    name = f"oiltank-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-p",
            f"127.0.0.1:{port}:1883",
            "-v",
            f"{conf}:/mosquitto/config/mosquitto.conf:ro",
            IMAGE,
        ],
        check=True,
        capture_output=True,
    )
    try:
        deadline = time.monotonic() + 20
        while not _mqtt_ready(port):
            if time.monotonic() > deadline:
                raise RuntimeError(f"Mosquitto ({IMAGE}) didn't start")
            time.sleep(0.2)
        yield port
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture
def settings(broker: int, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("OILTANK_MQTT_HOST", "127.0.0.1")
    monkeypatch.setenv("OILTANK_MQTT_PORT", str(broker))
    monkeypatch.setenv("OILTANK_DEVICE_ID", f"tank_{uuid.uuid4().hex[:6]}")  # isolate tests
    return Settings()


class Watcher:
    """Subscribes to everything and records the latest message per topic."""

    def __init__(self, port: int) -> None:
        self.messages: dict[str, mqtt.MQTTMessage] = {}
        self.log: list[str] = []  # every topic received, in order
        self.client = mqtt.Client(
            CallbackAPIVersion.VERSION2, client_id=f"w-{uuid.uuid4().hex[:6]}"
        )
        self.client.on_message = self._on_message
        self.client.on_connect = self._on_connect
        self.client.on_subscribe = self._on_subscribe
        self._subscribed = threading.Event()
        self.client.connect("127.0.0.1", port)
        self.client.loop_start()
        if not self._subscribed.wait(10):
            raise AssertionError("the watcher couldn't subscribe")

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: object,
        flags: mqtt.ConnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        if not reason_code.is_failure:
            client.subscribe("#", qos=1)  # on every connect: a reconnect starts unsubscribed

    def _on_subscribe(
        self,
        client: mqtt.Client,
        userdata: object,
        mid: int,
        reason_codes: list[ReasonCode],
        properties: Properties | None,
    ) -> None:
        self._subscribed.set()

    def _on_message(self, client: mqtt.Client, userdata: object, msg: mqtt.MQTTMessage) -> None:
        self.messages[msg.topic] = msg
        self.log.append(msg.topic)

    def wait_until(self, condition: Callable[[], bool], timeout: float = 10) -> None:
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                raise AssertionError("timed out waiting")
            time.sleep(0.05)

    def wait_for(
        self, topic: str, payload: bytes | None = None, timeout: float = 5
    ) -> mqtt.MQTTMessage:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            m = self.messages.get(topic)
            if m is not None and (payload is None or m.payload == payload):
                return m
            time.sleep(0.05)
        raise AssertionError(f"nothing on {topic} (have {sorted(self.messages)})")

    def close(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


@pytest.fixture
def watcher(broker: int) -> Iterator[Watcher]:
    w = Watcher(broker)
    yield w
    w.close()
