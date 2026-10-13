"""``oiltank run`` as a real process: fake go2rtc on localhost, Mosquitto in Docker."""

import json
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from oiltank.config import Settings
from tests.conftest import SAMPLES
from tests.fakes import serve_go2rtc
from tests.integration.conftest import Watcher

pytestmark = pytest.mark.integration
OILTANK = str(Path(sys.executable).parent / "oiltank")


@pytest.fixture
def env(
    settings: Settings, tmp_path: Path, sample_frame_paths: list[Path]
) -> Iterator[dict[str, str]]:
    data = tmp_path / "data"
    data.mkdir()
    shutil.copyfile(SAMPLES / "calibration.yaml", data / "calibration.yaml")
    shutil.copyfile(SAMPLES / "frame0.jpg", data / "frame0.jpg")
    with serve_go2rtc([p.read_bytes() for p in sample_frame_paths]) as go2rtc:
        yield {
            "PATH": os.environ.get("PATH", ""),
            "OILTANK_MQTT_HOST": "127.0.0.1",
            "OILTANK_MQTT_PORT": str(settings.mqtt.port),
            "OILTANK_DEVICE_ID": settings.device.id,
            "OILTANK_CAMERA_URL": go2rtc.url,
            "OILTANK_CAMERA_BURST_FRAMES": "2",
            "OILTANK_CAMERA_BURST_SPACING_S": "0",
            "OILTANK_SERVICE_INTERVAL_MIN": "0.05",  # every 3 s
            "OILTANK_DATA_DIR": str(data),
            "OILTANK_TANK_CAPACITY": "250",
        }


def _states(watcher: Watcher, topic: str) -> int:
    return sum(1 for t in watcher.log if t == topic)


def test_service_end_to_end(env: dict[str, str], settings: Settings, watcher: Watcher) -> None:
    base = f"oiltank/{settings.device.id}"
    proc = subprocess.Popen(
        [OILTANK, "run"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    try:
        watcher.wait_for(f"homeassistant/device/{settings.device.id}/config", timeout=20)
        assert watcher.wait_for(f"{base}/availability", b"online", timeout=20)
        state = json.loads(watcher.wait_for(f"{base}/state", timeout=30).payload)
        assert state["level"] == pytest.approx(98, abs=1)
        assert state["volume"] == pytest.approx(245, abs=3)
        assert watcher.wait_for(f"{base}/image").payload[:2] == b"\xff\xd8"

        watcher.wait_until(lambda: _states(watcher, f"{base}/state") >= 2, timeout=20)  # scheduled
        before = _states(watcher, f"{base}/state")
        watcher.client.publish(f"{base}/cmd/read", "PRESS", qos=1)
        watcher.wait_until(lambda: _states(watcher, f"{base}/state") > before, timeout=10)

        health = subprocess.run([OILTANK, "healthcheck"], env=env, capture_output=True, text=True)
        assert health.returncode == 0, health.stdout
        assert "MQTT connected" in health.stdout

        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=30) == 0
        assert watcher.wait_for(f"{base}/availability", b"offline", timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
        output = proc.communicate()[0]
    assert "Read now pressed" in output
    assert "stopping" in output

    history = subprocess.run([OILTANK, "history"], env=env, capture_output=True, text=True)
    assert history.returncode == 0
    assert "now: level 9" in history.stdout
