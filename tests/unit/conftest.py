"""Fixtures shared by the unit tests: the service with a scripted camera and a publisher that
records what it's asked to publish."""

import queue
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from oiltank.config import Settings
from oiltank.events import Event
from oiltank.history import History
from oiltank.model import Frame
from oiltank.publish.discovery import Topics
from oiltank.service import Service
from tests.conftest import SAMPLES
from tests.fakes import RecordingPublisher, Rig, ScriptedCamera


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    shutil.copyfile(SAMPLES / "calibration.yaml", data / "calibration.yaml")
    shutil.copyfile(SAMPLES / "frame0.jpg", data / "frame0.jpg")
    return data


@pytest.fixture
def settings(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("OILTANK_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OILTANK_MQTT_HOST", "broker")
    monkeypatch.setenv("OILTANK_TANK_CAPACITY", "250")
    monkeypatch.setenv("OILTANK_CAMERA_BURST_FRAMES", "2")
    return Settings()


@pytest.fixture
def camera(sample_frames: list[Frame]) -> ScriptedCamera:
    return ScriptedCamera(frames=sample_frames)


@pytest.fixture
def rig(settings: Settings, camera: ScriptedCamera) -> Iterator[Rig]:
    events: queue.Queue[Event] = queue.Queue()
    publisher = RecordingPublisher(Topics.from_settings(settings))
    history = History(settings.data_dir / "oiltank.db")
    service = Service(
        settings,
        settings.calibration_path,
        camera,
        publisher,
        events,
        history,
        republish_delay=lambda: 0.0,
    )
    yield Rig(service, publisher, events, history)
    history.close()
