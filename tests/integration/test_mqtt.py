"""MqttLink against a real Mosquitto broker in Docker. Run with ``uv run pytest -m integration``."""

import json
import queue
import shutil
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import paho.mqtt.client as mqtt
import pytest
from paho.mqtt.enums import CallbackAPIVersion

from oiltank.config import Settings
from oiltank.model import LevelState
from oiltank.publish.mqtt import Event, EventKind, MqttError, MqttLink
from tests.conftest import SAMPLE_CALIBRATION

pytestmark = pytest.mark.integration

IMAGE = "eclipse-mosquitto:2.0.20"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def broker(tmp_path_factory: pytest.TempPathFactory) -> Iterator[int]:
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    conf = tmp_path_factory.mktemp("mosquitto") / "mosquitto.conf"
    conf.write_text("listener 1883\nallow_anonymous true\n")
    port = _free_port()
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
        self.client = mqtt.Client(
            CallbackAPIVersion.VERSION2, client_id=f"w-{uuid.uuid4().hex[:6]}"
        )
        self.client.on_message = lambda c, u, m: self.messages.__setitem__(m.topic, m)
        self.client.connect("127.0.0.1", port)
        self.client.subscribe("#", qos=1)
        self.client.loop_start()

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


def _state() -> LevelState:
    return LevelState(
        ts=datetime.now(UTC), fraction=0.5, volume=125, confidence=1, t=0.5, problem=None
    )


def test_connect_announces_and_publishes_retained(settings: Settings, watcher: Watcher) -> None:
    events: queue.Queue[Event] = queue.Queue()
    link = MqttLink(settings, events)
    link.start()
    assert events.get(timeout=5).kind is EventKind.CONNECTED
    link.publish_state(_state())
    link.publish_image(b"\xff\xd8fake jpeg")
    t = link.topics
    config = watcher.wait_for(t.discovery)
    assert json.loads(config.payload)["components"]["level"]["state_topic"] == t.state
    assert watcher.wait_for(t.availability, b"online")
    assert json.loads(watcher.wait_for(t.state).payload)["level"] == 50.0
    link.stop()
    assert watcher.wait_for(t.availability, b"offline")

    # A newcomer (e.g. Home Assistant starting) gets everything from the broker's retained store.
    late = Watcher(int(settings.mqtt.port))
    try:
        for topic in (t.discovery, t.state, t.image, t.availability):
            assert late.wait_for(topic).retain, topic
    finally:
        late.close()


def test_button_and_ha_restart_become_events(settings: Settings, watcher: Watcher) -> None:
    events: queue.Queue[Event] = queue.Queue()
    link = MqttLink(settings, events)
    link.start()
    assert events.get(timeout=5).kind is EventKind.CONNECTED
    watcher.client.publish(link.topics.command, "PRESS", qos=1)
    assert events.get(timeout=5).kind is EventKind.READ_NOW
    watcher.client.publish(link.topics.command, "something else", qos=1)
    watcher.client.publish(link.topics.ha_status, "online", qos=1)
    assert events.get(timeout=5).kind is EventKind.HA_ONLINE
    link.stop()


def test_last_will_on_abrupt_disconnect(settings: Settings, watcher: Watcher, broker: int) -> None:
    env = {
        "OILTANK_MQTT_HOST": "127.0.0.1",
        "OILTANK_MQTT_PORT": str(broker),
        "OILTANK_DEVICE_ID": settings.device.id,
        "PATH": "",
    }
    oiltank = Path(sys.executable).parent / "oiltank"
    proc = subprocess.Popen(
        [
            str(oiltank),
            "mqtt",
            "publish",
            "-c",
            str(SAMPLE_CALIBRATION),
            "--image",
            str(SAMPLE_CALIBRATION.parent / "frame0.jpg"),
            "--listen",
            "60",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        topics_base = f"oiltank/{settings.device.id}"
        watcher.wait_for(f"{topics_base}/image", timeout=30)
        assert watcher.wait_for(f"{topics_base}/availability").payload == b"online"
        proc.kill()  # no clean disconnect: the broker publishes the last will
        assert watcher.wait_for(f"{topics_base}/availability", b"offline", timeout=10)
    finally:
        proc.kill()
        proc.wait()


def test_clear_removes_everything(settings: Settings, watcher: Watcher) -> None:
    link = MqttLink(settings, queue.Queue())
    link.start()
    link.publish_state(_state())
    link.stop()
    clearer = MqttLink(settings, queue.Queue(), announce=False)
    clearer.start()
    clearer.clear()
    clearer.stop(offline=False)
    late = Watcher(int(settings.mqtt.port))
    try:
        time.sleep(1)
        assert not [t for t in late.messages if t.startswith(clearer.topics.base)]
        assert clearer.topics.discovery not in late.messages
    finally:
        late.close()


def test_unreachable_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OILTANK_MQTT_HOST", "127.0.0.1")
    monkeypatch.setenv("OILTANK_MQTT_PORT", str(_free_port()))
    link = MqttLink(Settings(), queue.Queue())
    with pytest.raises(MqttError, match="no answer"):
        link.start(timeout_s=2)
