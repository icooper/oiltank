"""Fixtures for integration tests: a Mosquitto broker in Docker, and a topic watcher."""

import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator

import paho.mqtt.client as mqtt
import pytest
from paho.mqtt.enums import CallbackAPIVersion

from oiltank.config import Settings

IMAGE = "eclipse-mosquitto:2.0.20"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


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
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
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
        self.client.connect("127.0.0.1", port)
        self.client.subscribe("#", qos=1)
        self.client.loop_start()

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
