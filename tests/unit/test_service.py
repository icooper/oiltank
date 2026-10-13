"""The service, with a scripted camera and a publisher that records what it's asked to publish
(``rig`` and friends are in ``tests/unit/conftest.py``)."""

import json
import queue
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
import pytest

from oiltank.archive import Archive, ArchivePolicy
from oiltank.calibrate import shifted
from oiltank.calibration import Calibration, CalibrationError, CalibrationPoint, calibration_etag
from oiltank.config import Settings
from oiltank.epochs import Change
from oiltank.events import Event, EventKind
from oiltank.history import History
from oiltank.model import Frame, Problem
from oiltank.publish.discovery import Topics
from oiltank.service import (
    CalibrationConflictError,
    Service,
    ServiceBusyError,
    ServiceStoppedError,
)
from tests.cv import synth
from tests.fakes import RecordingPublisher, Rig, ScriptedCamera, running


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


EVER = datetime(2000, 1, 1, tzinfo=UTC)


def test_an_axis_edit_keeps_stored_readings_on_the_gauge(
    rig: Rig, settings: Settings, camera: ScriptedCamera
) -> None:
    for _ in range(3):
        rig.service.cycle()
    cal = Calibration.load(settings.calibration_path)
    (x, y), bottom = cal.axis.top, cal.axis.bottom
    # A longer axis: stored t values (measured along the old one) now mean somewhere else. The
    # owner's 98% point is an `at` position, so the disc still reads 98% on the new axis.
    cal.updated(axis=cal.axis.updated(top=(x, y - 30))).save(settings.calibration_path)
    assert rig.service.cycle().fraction == pytest.approx(0.98, abs=0.01)
    publisher = RecordingPublisher(Topics.from_settings(settings))
    restarted = Service(
        settings, settings.calibration_path, camera, publisher, queue.Queue(), rig.history
    )
    restarted._publish()  # replayed entirely from history
    assert publisher.states[0].fraction == pytest.approx(0.98, abs=0.01)
    current = rig.history.current_epoch()
    assert current is not None and current.change is Change.AXIS_EDIT
    assert current.bottom == bottom


def test_a_rebase_keeps_stored_readings_on_the_gauge(
    rig: Rig, settings: Settings, camera: ScriptedCamera, sample_frames: list[Frame]
) -> None:
    for _ in range(3):
        rig.service.cycle()
    dx, dy = 14, -9  # the camera was bumped
    bumped = []
    for f in sample_frames[:4]:
        gray = synth.shift(f.gray, dx, dy)
        ok, jpeg = cv2.imencode(".jpg", gray)
        assert ok
        bumped.append(Frame(gray=gray, ts=f.ts, source=f.source, data=jpeg.tobytes()))
    camera.frames = bumped
    reference = Path("reference-bumped.jpg")
    assert bumped[0].data is not None
    (settings.data_dir / reference).write_bytes(bumped[0].data)
    cal = Calibration.load(settings.calibration_path)
    shifted(cal, dx, dy, reference).save(settings.calibration_path)
    assert rig.service.cycle().fraction == pytest.approx(0.98, abs=0.01)
    current = rig.history.current_epoch()
    assert current is not None and current.change is Change.REBASE
    stored = rig.history.observations(EVER)
    assert [o.t for o in rig.history.observations(EVER, onto=current)] == [o.t for o in stored]


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


def test_calls_run_on_the_service_thread_between_readings(rig: Rig, camera: ScriptedCamera) -> None:
    camera.delay = 0.5
    with running(rig) as thread:
        _wait(lambda: rig.service.snapshot.reading)  # the first reading is under way
        seen = rig.service.call(
            lambda s: (threading.current_thread(), rig.history.count(), s.snapshot.reading)
        )
        assert rig.service.snapshot.next_reading is not None
    assert seen == (thread, 1, False)  # it waited for the reading to finish


def test_a_call_that_times_out_never_runs(rig: Rig, camera: ScriptedCamera) -> None:
    camera.delay = 1.0
    ran: list[str] = []
    with running(rig):
        _wait(lambda: rig.service.snapshot.reading)
        with pytest.raises(ServiceBusyError):
            rig.service.call(lambda s: ran.append("late"), timeout=0.1)
        assert rig.service.call(lambda s: "next") == "next"  # queued behind the abandoned one
    assert ran == []


def test_calls_fail_when_the_service_stops(rig: Rig, camera: ScriptedCamera) -> None:
    camera.delay = 0.5
    stop = threading.Event()
    thread = threading.Thread(target=rig.service.run, args=(stop,), daemon=True)
    thread.start()
    _wait(lambda: rig.service.snapshot.reading)
    rig.events.put(Event(EventKind.STOP))  # handled before the call below
    with pytest.raises(ServiceStoppedError):
        rig.service.call(lambda s: None, timeout=10)
    thread.join(timeout=10)
    with pytest.raises(ServiceStoppedError):
        rig.service.call(lambda s: None)  # straight away, once it has stopped


def test_errors_in_calls_reach_the_caller(rig: Rig) -> None:
    with running(rig), pytest.raises(ZeroDivisionError):
        rig.service.call(lambda s: 1 / 0)


def test_snapshot_follows_readings(rig: Rig, settings: Settings) -> None:
    before = rig.service.snapshot
    assert before.state is None and before.image is None and not before.reading
    state = rig.service.cycle()
    after = rig.service.snapshot
    assert after.state == state
    assert after.image == rig.publisher.images[-1]
    assert after.calibration_etag == calibration_etag(settings.calibration_path)
    assert after.mqtt_connected and not after.reading


def test_apply_calibration_switches_at_once(rig: Rig, settings: Settings) -> None:
    first = rig.service.cycle()
    snap = rig.service.snapshot
    at = snap.calibration.points[0].at
    edited = snap.calibration.updated(points=[CalibrationPoint(at=at, fraction=0.9)])
    with pytest.raises(CalibrationConflictError):
        rig.service.apply_calibration(edited, if_match="not the file's tag")
    published = len(rig.publisher.states)
    after = rig.service.apply_calibration(edited, if_match=snap.calibration_etag)
    assert Calibration.load(settings.calibration_path).points[0].fraction == 0.9
    assert after.calibration.points[0].fraction == 0.9
    assert after.calibration_etag == calibration_etag(settings.calibration_path)
    assert first.fraction is not None and after.state is not None
    assert after.state.fraction == pytest.approx(first.fraction * 0.9 / 0.98, abs=0.01)
    assert rig.publisher.states[published:] == [after.state]  # published straight away


def test_apply_calibration_refuses_a_stale_edit(rig: Rig, settings: Settings) -> None:
    snap = rig.service.snapshot
    at = snap.calibration.points[0].at
    # Someone runs `oiltank calibrate point` after the edit was started.
    snap.calibration.updated(points=[CalibrationPoint(at=at, fraction=0.95)]).save(
        settings.calibration_path
    )
    edited = snap.calibration.updated(points=[CalibrationPoint(at=at, fraction=0.9)])
    with pytest.raises(CalibrationConflictError) as conflict:
        rig.service.apply_calibration(edited, if_match=snap.calibration_etag)
    assert conflict.value.etag == calibration_etag(settings.calibration_path)
    assert Calibration.load(settings.calibration_path).points[0].fraction == 0.95


def test_apply_calibration_needs_a_usable_reference(rig: Rig, settings: Settings) -> None:
    snap = rig.service.snapshot
    before = settings.calibration_path.read_bytes()
    missing = snap.calibration.updated(reference_image=Path("missing.jpg"))
    with pytest.raises(CalibrationError, match="reference frame"):
        rig.service.apply_calibration(missing, if_match=snap.calibration_etag)
    assert settings.calibration_path.read_bytes() == before  # nothing saved


def _wait(condition: Callable[[], bool], timeout: float = 20.0) -> None:
    deadline = datetime.now(UTC) + timedelta(seconds=timeout)
    while not condition():
        if datetime.now(UTC) > deadline:
            raise AssertionError("timed out")
        threading.Event().wait(0.05)


def test_apply_calibration_with_a_new_reference(
    rig: Rig, settings: Settings, sample_frames: list[Frame]
) -> None:
    rig.service.cycle()
    snap = rig.service.snapshot
    gray = synth.shift(sample_frames[0].gray, 14, -9)  # the camera was bumped
    ok, jpeg = cv2.imencode(".jpg", gray)
    assert ok
    name = Path("reference-bumped.jpg")
    moved = shifted(snap.calibration, 14, -9, name)
    after = rig.service.apply_calibration(
        moved, if_match=snap.calibration_etag, reference=jpeg.tobytes()
    )
    assert (settings.data_dir / name).read_bytes() == jpeg.tobytes()
    assert after.calibration.reference_image == name
    assert after.epoch.change is Change.REBASE
    assert after.state is not None and after.state.fraction == pytest.approx(0.98, abs=0.01)
