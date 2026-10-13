"""``oiltank run``, ``healthcheck`` and ``history``: the service and its companions."""

import json
import queue
import signal
import threading
import time
from datetime import UTC, datetime, timedelta
from types import FrameType
from typing import Annotated

import typer
from pydantic import ValidationError

from oiltank.archive import Archive, ArchivePolicy
from oiltank.calibration import CalibrationError
from oiltank.cli.common import (
    CalibrationPath,
    calibration_file,
    fail,
    load_settings,
    validation_message,
)
from oiltank.filter import FilterParams, LevelFilter
from oiltank.history import History
from oiltank.publish.mqtt import Event, EventKind, MqttError, MqttLink
from oiltank.service import HEALTHY_AGE_S, Service
from oiltank.sources.base import CameraConfigError
from oiltank.sources.http import HttpSource


def run(calibration_path: CalibrationPath = None) -> None:
    """Run the service: read the gauge every OILTANK_SERVICE_INTERVAL_MIN minutes, and whenever
    Read now is pressed, and publish to Home Assistant until stopped (SIGTERM or Ctrl-C)."""
    settings = load_settings()
    path = calibration_file(calibration_path)
    if not path.exists():
        fail(f"no calibration at {path}; create one with `oiltank calibrate init` (see README)")
    events: queue.Queue[Event] = queue.Queue()
    try:
        link = MqttLink(settings, events)
        source = HttpSource.from_settings(settings.camera)
    except (MqttError, CameraConfigError) as e:
        fail(str(e))
    try:
        history = History(settings.data_dir / "oiltank.db")
    except (OSError, RuntimeError) as e:
        fail(f"can't open the history database: {e}")
    archive = None
    if settings.archive.enabled:
        policy = ArchivePolicy.from_settings(settings.archive)
        archive = Archive(settings.data_dir / "archive", policy)
    try:
        service = Service(settings, path, source, link, events, history, archive=archive)
    except CalibrationError as e:
        fail(str(e))
    except ValidationError as e:
        fail(f"invalid calibration {path}: {validation_message(e)}")

    stop = threading.Event()

    def request_stop(signum: int, frame: FrameType | None) -> None:
        stop.set()
        events.put(Event(EventKind.STOP))

    # As PID 1 in a container, Python ignores SIGTERM unless there's a handler.
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        service.run(stop)
    finally:
        source.close()
        history.close()


def healthcheck(
    max_age: Annotated[
        float, typer.Option(help="Oldest acceptable heartbeat, in seconds.")
    ] = HEALTHY_AGE_S,
) -> None:
    """Exit 0 if the service's heartbeat is recent: for Docker's healthcheck."""
    path = load_settings().data_dir / "heartbeat"
    try:
        age = time.time() - path.stat().st_mtime
        detail = json.loads(path.read_text())
    except FileNotFoundError:
        typer.echo(f"unhealthy: no heartbeat at {path} (is `oiltank run` running?)")
        raise typer.Exit(1) from None
    except (OSError, ValueError) as e:
        typer.echo(f"unhealthy: can't read {path}: {e}")
        raise typer.Exit(1) from None
    if age > max_age:
        typer.echo(f"unhealthy: the last heartbeat was {age:.0f} s ago")
        raise typer.Exit(1)
    mqtt = "connected" if detail.get("mqtt_connected") else "NOT connected"
    typer.echo(
        f"ok: heartbeat {age:.0f} s ago; MQTT {mqtt}; last reading {detail.get('last_reading')}"
    )


def history(
    days: Annotated[float, typer.Option(min=0, help="How far back to show.")] = 2.0,
) -> None:
    """Show recent readings and refills from the service's history, and what they add up to."""
    settings = load_settings()
    path = settings.data_dir / "oiltank.db"
    if not path.exists():
        fail(f"no history at {path} yet (the service creates it)")
    tank = settings.tank
    params = FilterParams.from_settings(settings.service)
    with History(path) as h:
        now = datetime.now(UTC)
        since = now - timedelta(days=max(days, params.usage_window_days + 1))
        observations = h.observations(since)
        f = LevelFilter(params, tank.capacity, tank.reserve, last_fill=h.last_fill(since))
        f.replay(observations)
        shown = [o for o in observations if o.ts >= now - timedelta(days=days)]
        typer.echo(f"{h.count()} readings stored; {len(shown)} in the last {days:g} day(s):")
        for o in shown:
            when = o.ts.astimezone().strftime("%Y-%m-%d %H:%M")
            level = "   --  " if o.fraction is None else f"{o.fraction:6.1%}"
            problem = f"  {o.problem}" if o.problem else ""
            typer.echo(f"  {when}  {level}  confidence {o.confidence:.2f}{problem}")
        for fill in h.fills():
            when = fill.ts.astimezone().strftime("%Y-%m-%d %H:%M")
            typer.echo(
                f"refill {when}: {fill.before:.0%} -> {fill.after:.0%} "
                f"({fill.volume(tank.capacity):.0f} {tank.unit})"
            )
    if observations:
        state = f.state(observations[-1].ts)
        parts = []
        if state.fraction is not None and state.volume is not None:
            parts.append(f"level {state.fraction:.1%} ({state.volume:.0f} {tank.unit})")
        usage = (
            "not enough data yet"
            if state.usage_rate is None
            else (f"{state.usage_rate:.1f} {tank.unit}/day")
        )
        parts.append(f"usage {usage}")
        if state.days_left is not None:
            parts.append(f"{state.days_left:.0f} days left")
        if state.problem is not None:
            parts.append(f"PROBLEM: {state.problem}")
        typer.echo("now: " + "; ".join(parts))
