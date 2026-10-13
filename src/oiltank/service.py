"""The long-running service: read the gauge on a schedule and on Read now, filter, store, publish.

One thread does all the work. The MQTT link's callbacks only put events on the queue this loop
waits on, so readings (slow CV) never run in paho's network thread. Other threads (the web UI)
read :attr:`Service.snapshot` and change things through :meth:`Service.call`, which runs on this
thread between readings.
"""

import hashlib
import json
import logging
import queue
import random
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from oiltank.annotate import encode_jpeg, render
from oiltank.archive import Archive
from oiltank.calibration import (
    Calibration,
    CalibrationError,
    ResolutionMismatch,
    calibration_etag,
)
from oiltank.config import Settings
from oiltank.epochs import AxisEpoch, Change
from oiltank.events import Event, EventKind
from oiltank.filter import FilterParams, LevelFilter, Observation
from oiltank.geometry import Aligner
from oiltank.history import History
from oiltank.model import Frame, LevelState, Measurement, Problem
from oiltank.pipeline import measure
from oiltank.publish.discovery import Topics
from oiltank.publish.mqtt import MqttError
from oiltank.readers import GaugeReader, ReaderConfig, build_reader
from oiltank.sources.base import CameraError, FrameSource
from oiltank.sources.files import FrameDecodeError

log = logging.getLogger(__name__)

HEARTBEAT_S = 30.0
"""The loop wakes, and refreshes the heartbeat file, at least this often."""
HEALTHY_AGE_S = 120.0
"""A heartbeat older than this means the loop is stuck (used by ``oiltank healthcheck``)."""
CALL_TIMEOUT_S = 90.0
"""How long :meth:`Service.call` waits for its turn: a reading with camera retries can take about
a minute."""


class ServiceBusyError(Exception):
    """A call didn't get its turn on the service thread in time. It will never run."""


class ServiceStoppedError(Exception):
    """The service stopped before a call could run."""


class CalibrationConflictError(Exception):
    """The calibration file changed after the edit was started (e.g. by ``oiltank calibrate``)."""

    def __init__(self, etag: str) -> None:
        super().__init__("the calibration has changed since it was read")
        self.etag = etag
        """The file's tag now."""


@dataclass(frozen=True, slots=True)
class Snapshot:
    """The service as other threads see it. It's replaced whole, never changed in place."""

    calibration: Calibration
    calibration_etag: str
    """:func:`~oiltank.calibration.calibration_etag` of the file the calibration was loaded from."""
    epoch: AxisEpoch
    state: LevelState | None
    image: bytes | None
    """The last published gauge image (JPEG)."""
    reading: bool
    """A reading is in progress."""
    next_reading: datetime | None
    """When the next scheduled reading is due (None until the loop runs)."""
    mqtt_connected: bool
    archive_bytes: int | None
    """None until the first reading is archived."""


class Publisher(Protocol):
    """What the service needs from :class:`~oiltank.publish.mqtt.MqttLink`."""

    topics: Topics

    @property
    def connected(self) -> bool: ...

    def start(self, timeout_s: float = ..., *, keep_trying: bool = ...) -> bool: ...
    def stop(self, *, offline: bool = ...) -> None: ...
    def publish_discovery(self) -> None: ...
    def publish_availability(self, online: bool) -> None: ...
    def publish_state(self, state: LevelState) -> None: ...
    def publish_image(self, jpeg: bytes) -> None: ...


class Service:
    def __init__(
        self,
        settings: Settings,
        calibration_path: Path,
        source: FrameSource,
        publisher: Publisher,
        events: queue.Queue[Event],
        history: History,
        *,
        archive: Archive | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        republish_delay: Callable[[], float] = lambda: random.uniform(0, 5),
    ) -> None:
        """Raises CalibrationError or ValidationError if the calibration can't be used."""
        self._settings = settings
        self._calibration_path = calibration_path
        self._source = source
        self._publisher = publisher
        self._events = events
        self._history = history
        self._archive = archive
        self._clock = clock
        self._monotonic = monotonic
        self._republish_delay = republish_delay
        self._heartbeat = settings.data_dir / "heartbeat"
        self._params = FilterParams.from_settings(settings.service)
        self._state: LevelState | None = None
        self._image: bytes | None = None
        self._last_digests: set[bytes] = set()
        self._reading = False
        self._next_reading: datetime | None = None
        self._archive_bytes: int | None = None
        self._stopping = False
        self._load_calibration()
        self._filter = self._replay()
        self._refresh()

    # ---- calibration and history ---------------------------------------------------------------

    def _load_calibration(self) -> None:
        path = self._calibration_path
        try:
            mtime = path.stat().st_mtime_ns  # first, so an edit made while loading is seen later
        except OSError as e:
            raise CalibrationError(f"cannot read calibration {path}: {e}") from e
        etag = calibration_etag(path)
        calibration = Calibration.load(path)
        aligner = Aligner(calibration.anchor, calibration.anchor_patch)
        reader = build_reader(ReaderConfig(), calibration)
        epoch, change = self._history.register_epoch(
            calibration.reference_image.as_posix(), calibration.axis, self._clock()
        )
        self._reader: GaugeReader = reader
        self._calibration = calibration
        self._aligner = aligner
        self._calibration_mtime = mtime
        self._calibration_etag = etag
        self._epoch = epoch
        match change:
            case Change.AXIS_EDIT:
                log.info("the gauge axis changed; stored readings are mapped onto the new one")
            case Change.REBASE:
                log.info("the calibration was rebased; stored readings keep their gauge positions")
            case Change.NEW_REFERENCE:
                log.warning(
                    "the reference frame changed and the gauge axis didn't just move with the "
                    "camera, so stored readings can't be mapped onto it; levels from before may "
                    "be off"
                )

    def _reload_if_changed(self) -> None:
        """Pick up calibration edits (e.g. `calibrate point` run alongside the service)."""
        try:
            mtime = self._calibration_path.stat().st_mtime_ns
        except OSError as e:
            log.error("can't read the calibration (%s); still using the one loaded", e)
            return
        if mtime == self._calibration_mtime:
            return
        try:
            self._load_calibration()
        except (CalibrationError, ValidationError, OSError) as e:
            self._calibration_mtime = mtime  # don't retry until it changes again
            log.error("the edited calibration is invalid (%s); still using the previous one", e)
            return
        log.info("calibration changed; reloaded it and replayed history with it")
        self._filter = self._replay()

    def _replay(self) -> LevelFilter:
        """A filter rebuilt from stored readings, their fractions recomputed with the current
        calibration."""
        tank = self._settings.tank
        since = self._clock() - timedelta(days=self._params.usage_window_days + 1)
        f = LevelFilter(
            self._params, tank.capacity, tank.reserve, last_fill=self._history.last_fill(since)
        )
        observations = self._history.observations(since, onto=self._epoch)
        f.replay(o.recalibrated(self._calibration.fraction) for o in observations)
        if observations:
            self._state = f.state(observations[-1].ts)
        return f

    # ---- one reading ---------------------------------------------------------------------------

    def apply_calibration(self, calibration: Calibration, *, if_match: str) -> Snapshot:
        """Save ``calibration`` as the calibration file and switch to it now: reload, replay the
        history with it and publish the result. Call it through :meth:`call`.

        ``if_match`` is the tag of the file the change was made to. Raises
        CalibrationConflictError if the file has changed since, and CalibrationError if the
        reference frame can't be used (nothing is saved then).
        """
        current = calibration_etag(self._calibration_path)
        if current != if_match:
            raise CalibrationConflictError(current)
        try:
            Aligner(calibration.anchor, calibration.anchor_patch)  # the reference must be usable
        except (OSError, FrameDecodeError) as e:
            raise CalibrationError(f"cannot use the reference frame: {e}") from e
        calibration.save(self._calibration_path)
        self._load_calibration()
        log.info("calibration saved; reloaded it and replayed history with it")
        self._filter = self._replay()
        self._publish(image=False)  # the image changes at the next reading
        self._refresh()
        return self.snapshot

    def cycle(self) -> LevelState:
        """Take a reading, filter it, store it and publish the result."""
        self._reading = True
        self._refresh()
        try:
            return self._cycle()
        finally:
            self._reading = False
            self._refresh()

    def _cycle(self) -> LevelState:
        self._reload_if_changed()
        camera = self._settings.camera
        frames: list[Frame] = []
        m: Measurement | None = None
        try:
            frames = self._source.grab(camera.burst_frames, camera.burst_spacing_s)
        except CameraError as e:
            log.warning("camera: %s", e)
        digests = {hashlib.sha256(f.data).digest() for f in frames if f.data is not None}
        if frames and digests <= self._last_digests:
            log.warning("the camera sent exactly the same frames as last time; is it frozen?")
            frames = []
        if frames:
            self._last_digests = digests
            try:
                m = measure(frames, self._aligner, self._reader, self._calibration)
            except ResolutionMismatch as e:
                log.error("%s", e)
        if self._archive is not None:
            try:
                self._archive.record(m, frames, self._calibration)
            except OSError as e:
                log.error("couldn't archive the reading: %s", e)
            self._archive_bytes = self._archive.size()
        now = frames[-1].ts if frames else self._clock()
        if m is not None:
            obs = Observation(m.ts, m.fraction if m.ok else None, m.confidence, m.t, m.problem)
        else:
            problem = Problem.WRONG_RESOLUTION if frames else Problem.CAMERA_UNREACHABLE
            obs = Observation(now, None, 0.0, None, problem)

        update = self._filter.update(obs)
        self._history.add(obs, accepted=update.accepted, frames=len(frames), epoch=self._epoch)
        if update.fill is not None:
            self._history.add_fill(update.fill)
            log.info(
                "refill detected: %.0f%% -> %.0f%% (%.0f %s)",
                100 * update.fill.before,
                100 * update.fill.after,
                update.fill.volume(self._settings.tank.capacity),
                self._settings.tank.unit,
            )
        self._state = update.state
        if m is not None:
            tank = self._settings.tank
            image = render(
                frames[-1].gray, m, self._calibration, capacity=tank.capacity, unit=tank.unit
            )
            self._image = encode_jpeg(image)
            for notice in m.notices:
                log.warning("notice: %s", notice)
        self._log_state(obs, update.accepted)
        self._publish(image=m is not None)  # no new frames: leave the last image as it was
        return update.state

    def _log_state(self, obs: Observation, accepted: bool) -> None:
        state = self._state
        if state is None:
            return
        unit = self._settings.tank.unit
        confidence = f"confidence {obs.confidence:.2f}"
        if accepted and state.fraction is not None and state.volume is not None:
            msg = f"level {state.fraction:.1%} ({state.volume:.0f} {unit}), {confidence}"
        else:
            msg = f"reading rejected ({obs.problem or 'low confidence'}, {confidence})"
        if state.usage_rate is not None:
            msg += f"; using {state.usage_rate:.1f} {unit}/day"
        if state.days_left is not None:
            msg += f", {state.days_left:.0f} days left"
        if state.problem is not None:
            msg += f"; PROBLEM: {state.problem}"
        log.info("%s", msg)

    # ---- publishing ----------------------------------------------------------------------------

    def _publish(self, *, image: bool = True) -> None:
        """Publish the latest state, and the latest gauge image unless ``image`` is False."""
        if self._state is None:
            return
        try:
            self._publisher.publish_state(self._state)
            if image and self._image is not None:
                self._publisher.publish_image(self._image)
        except MqttError as e:
            log.warning("not published (%s); will republish when reconnected", e)

    def _announce(self) -> None:
        """After Home Assistant restarts: discovery, availability, then the latest state."""
        try:
            self._publisher.publish_discovery()
            self._publisher.publish_availability(True)
        except MqttError as e:
            log.warning("couldn't republish discovery (%s)", e)
            return
        self._publish()

    def _refresh(self) -> None:
        self.snapshot = Snapshot(
            calibration=self._calibration,
            calibration_etag=self._calibration_etag,
            epoch=self._epoch,
            state=self._state,
            image=self._image,
            reading=self._reading,
            next_reading=self._next_reading,
            mqtt_connected=self._publisher.connected,
            archive_bytes=self._archive_bytes,
        )

    def _beat(self) -> None:
        payload = {
            "ts": self._clock().isoformat(timespec="seconds"),
            "last_reading": None if self._state is None else self._state.ts.isoformat(),
            "mqtt_connected": self._publisher.connected,
        }
        tmp = self._heartbeat.with_name(".heartbeat.tmp")
        try:
            tmp.write_text(json.dumps(payload))
            tmp.replace(self._heartbeat)
        except OSError as e:
            log.error("can't write the heartbeat file %s: %s", self._heartbeat, e)

    # ---- the loop ------------------------------------------------------------------------------

    def run(self, stop: threading.Event) -> None:
        """Run until ``stop`` is set (put an EventKind.STOP event on the queue to wake it)."""
        interval = self._settings.service.interval_min * 60
        log.info(
            "starting: a reading every %g min, publishing to %s:%d",
            self._settings.service.interval_min,
            self._settings.mqtt.host,
            self._settings.mqtt.port,
        )
        self._publisher.start(keep_trying=True)
        next_reading = self._monotonic()
        republish_at: float | None = None
        try:
            while not stop.is_set():
                self._beat()
                due_in = max(0.0, next_reading - self._monotonic())
                self._next_reading = self._clock() + timedelta(seconds=due_in)
                self._refresh()
                deadline = min(next_reading, republish_at or next_reading)
                timeout = max(0.0, min(deadline - self._monotonic(), HEARTBEAT_S))
                try:
                    event: Event | None = self._events.get(timeout=timeout)
                except queue.Empty:
                    event = None
                if event is not None:
                    match event.kind:
                        case EventKind.STOP:
                            break
                        case EventKind.READ_NOW:
                            log.info("Read now pressed")
                            next_reading = self._monotonic()
                        case EventKind.CONNECTED:
                            self._publish()  # whatever we couldn't publish while disconnected
                        case EventKind.HA_ONLINE:
                            republish_at = self._monotonic() + self._republish_delay()
                        case EventKind.CALL if event.task is not None:
                            event.task()
                now = self._monotonic()
                if republish_at is not None and now >= republish_at:
                    republish_at = None
                    log.info("Home Assistant restarted; republishing")
                    self._announce()
                if now >= next_reading and not stop.is_set():
                    try:
                        self.cycle()
                    except Exception:
                        log.exception("reading failed unexpectedly; trying again next time")
                    next_reading = self._monotonic() + interval
        finally:
            log.info("stopping")
            self._stopping = True
            self._fail_waiting_calls()
            self._publisher.stop()

    # ---- calls from other threads --------------------------------------------------------------

    def call[T](self, fn: Callable[[Service], T], timeout: float = CALL_TIMEOUT_S) -> T:
        """Run ``fn(service)`` on the service thread, between readings, and return its result.

        For other threads: the service's state only ever changes on its own thread. Raises
        ServiceBusyError if ``fn`` doesn't get its turn within ``timeout`` (it then never runs),
        ServiceStoppedError if the service stops first, and whatever ``fn`` raises.
        """
        if self._stopping:
            raise ServiceStoppedError("the service is stopping")
        future: Future[T] = Future()

        def task() -> None:
            if not future.set_running_or_notify_cancel():
                return  # the caller gave up: don't make a change it was told didn't happen
            if self._stopping:
                future.set_exception(ServiceStoppedError("the service stopped"))
                return
            try:
                future.set_result(fn(self))
            except Exception as e:
                future.set_exception(e)

        self._events.put(Event(EventKind.CALL, task))
        try:
            return future.result(timeout)
        except TimeoutError:
            if future.cancel():
                raise ServiceBusyError(f"the service was busy for {timeout:g} s") from None
            return future.result()  # it got its turn just as time ran out

    def _fail_waiting_calls(self) -> None:
        """Tell calls still on the queue that the service has stopped."""
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                return
            if event.kind is EventKind.CALL and event.task is not None:
                event.task()  # fails its caller: _stopping is set
