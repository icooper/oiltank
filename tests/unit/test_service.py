"""The service, with a scripted camera and a publisher that records what it's asked to publish."""

import json
import queue
import shutil
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
import pytest

from oiltank.archive import Archive, ArchivePolicy
from oiltank.calibration import Calibration, CalibrationPoint
from oiltank.config import Settings
from oiltank.history import History
from oiltank.model import Frame, LevelState, Problem
from oiltank.publish.discovery import Topics
from oiltank.publish.mqtt import Event, EventKind, MqttError
from oiltank.service import Service
from oiltank.sources.base import CameraError
from tests.conftest import SAMPLES
from tests.cv import synth


@dataclass
class ScriptedCamera:
    """Hands out the sample frames (distinct each time), or raises when told to."""

    frames: list[Frame]
    fail: bool = False
    calls: int = 0

    def grab(self, n: int, spacing_s: float) -> list[Frame]:
        self.calls += 1
        if self.fail:
            raise CameraError("go2rtc:1984 didn't answer within 15 s")
        start = (self.calls - 1) * n
        return [self.frames[(start + k) % len(self.frames)] for k in range(n)]


@dataclass
class RecordingPublisher:
    topics: Topics
    connected: bool = True
    broken: bool = False
    states: list[LevelState] = field(default_factory=list)
    images: list[bytes] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    def start(self, timeout_s: float = 15.0, *, keep_trying: bool = False) -> bool:
        self.calls.append("start")
        return True

    def stop(self, *, offline: bool = True) -> None:
        self.calls.append("stop")

    def publish_discovery(self) -> None:
        self.calls.append("discovery")

    def publish_availability(self, online: bool) -> None:
        self.calls.append(f"availability:{online}")

    def publish_state(self, state: LevelState) -> None:
        if self.broken:
            raise MqttError("publishing to oiltank/oil_tank/state failed: not connected")
        self.states.append(state)

    def publish_image(self, jpeg: bytes) -> None:
        self.images.append(jpeg)


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


@dataclass
class Rig:
    service: Service
    publisher: RecordingPublisher
    events: queue.Queue[Event]
    history: History


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


def test_a_cycle_reads_stores_and_publishes(rig: Rig) -> None:
    state = rig.service.cycle()
    assert state.fraction == pytest.approx(0.98, abs=0.01)
    assert state.volume == pytest.approx(245, abs=3)
    assert state.problem is None
    assert rig.publisher.states == [state]
    assert len(rig.publisher.images) == 1
    assert len(rig.publisher.images[0]) <= 50_000
    assert rig.history.count() == 1


def test_camera_outage_becomes_a_problem(rig: Rig, camera: ScriptedCamera) -> None:
    rig.service.cycle()
    camera.fail = True
    states = [rig.service.cycle() for _ in range(3)]
    assert [s.problem for s in states] == [None, None, Problem.CAMERA_UNREACHABLE]
    assert states[-1].fraction == pytest.approx(0.98, abs=0.01)  # the last good level stays
    assert len(rig.publisher.images) == 1  # no new image without frames
    camera.fail = False
    assert rig.service.cycle().problem is None


def test_frozen_camera_is_noticed(
    rig: Rig, camera: ScriptedCamera, sample_frames: list[Frame]
) -> None:
    camera.frames = sample_frames[:2]  # the same two frames every time
    rig.service.cycle()
    for _ in range(3):
        state = rig.service.cycle()
    assert state.problem is Problem.CAMERA_UNREACHABLE


def test_wrong_resolution(rig: Rig, camera: ScriptedCamera, sample_frames: list[Frame]) -> None:
    small = [
        synth.with_gray(f, np.asarray(cv2.resize(f.gray, (640, 360)), dtype=np.uint8))
        for f in sample_frames
    ]
    for f, k in zip(small, range(len(small)), strict=True):
        object.__setattr__(f, "data", bytes([k]))  # distinct, so not mistaken for frozen
    camera.frames = small
    for _ in range(3):
        state = rig.service.cycle()
    assert state.problem is Problem.WRONG_RESOLUTION


def test_readings_are_archived(settings: Settings, camera: ScriptedCamera) -> None:
    archive = Archive(settings.data_dir / "archive", ArchivePolicy())
    with History(settings.data_dir / "oiltank.db") as history:
        service = Service(
            settings,
            settings.calibration_path,
            camera,
            RecordingPublisher(Topics.from_settings(settings)),
            queue.Queue(),
            history,
            archive=archive,
        )
        service.cycle()
        service.cycle()
        camera.fail = True
        service.cycle()  # no frames: nothing to archive
    records = sorted((settings.data_dir / "archive").rglob("*.json"))
    assert len(records) == 2
    assert len(list((settings.data_dir / "archive").rglob("*-strip.png"))) == 2
    assert len(list((settings.data_dir / "archive").rglob("*-frame.jpg"))) == 1  # the first only


def test_publishing_failures_are_survived(rig: Rig) -> None:
    rig.publisher.broken = True
    rig.service.cycle()  # logged, not raised
    assert rig.publisher.states == []
    assert rig.history.count() == 1


def test_restart_replays_history(rig: Rig, settings: Settings, camera: ScriptedCamera) -> None:
    for _ in range(3):
        rig.service.cycle()
    publisher = RecordingPublisher(Topics.from_settings(settings))
    restarted = Service(
        settings, settings.calibration_path, camera, publisher, queue.Queue(), rig.history
    )
    restarted._publish()  # what it sends on connecting, before its first reading
    assert publisher.states[0].fraction == pytest.approx(0.98, abs=0.01)


def test_calibration_edits_are_picked_up(rig: Rig, settings: Settings) -> None:
    first = rig.service.cycle()
    cal = Calibration.load(settings.calibration_path)
    # Say the gauge read 90% where the disc is now: the level drops accordingly, history included.
    assert cal.points[0].at is not None
    cal.updated(points=[CalibrationPoint(at=cal.points[0].at, fraction=0.9)]).save(
        settings.calibration_path
    )
    second = rig.service.cycle()
    assert first.fraction is not None and second.fraction is not None
    assert second.fraction == pytest.approx(first.fraction * 0.9 / 0.98, abs=0.01)


def test_an_invalid_calibration_edit_keeps_the_old_one(rig: Rig, settings: Settings) -> None:
    rig.service.cycle()
    settings.calibration_path.write_text("version: 1\nnot: valid\n")
    assert rig.service.cycle().fraction == pytest.approx(0.98, abs=0.01)


def test_run_loop(rig: Rig, settings: Settings, camera: ScriptedCamera) -> None:
    stop = threading.Event()
    thread = threading.Thread(target=rig.service.run, args=(stop,), daemon=True)
    thread.start()
    try:
        _wait(lambda: camera.calls == 1)  # an immediate first reading
        heartbeat = json.loads((settings.data_dir / "heartbeat").read_text())
        assert heartbeat["mqtt_connected"] is True
        rig.events.put(Event(EventKind.READ_NOW))
        _wait(lambda: camera.calls == 2)
        rig.events.put(Event(EventKind.HA_ONLINE))
        _wait(lambda: "discovery" in rig.publisher.calls)
        assert "availability:True" in rig.publisher.calls
        published = len(rig.publisher.states)
        rig.events.put(Event(EventKind.CONNECTED))
        _wait(lambda: len(rig.publisher.states) > published)  # republished after a reconnect
    finally:
        stop.set()
        rig.events.put(Event(EventKind.STOP))
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert rig.publisher.calls[0] == "start"
    assert rig.publisher.calls[-1] == "stop"
    assert camera.calls == 2  # the 15-minute interval never came round


def _wait(condition: Callable[[], bool], timeout: float = 20.0) -> None:
    deadline = datetime.now(UTC) + timedelta(seconds=timeout)
    while not condition():
        if datetime.now(UTC) > deadline:
            raise AssertionError("timed out")
        threading.Event().wait(0.05)
