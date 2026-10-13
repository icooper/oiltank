"""``oiltank mqtt``: publish to Home Assistant by hand. The service (milestone M5) does it
continuously; these commands are for setting up and checking the Home Assistant side.
"""

import logging
import queue
import random
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from oiltank.annotate import encode_jpeg, render
from oiltank.calibration import Calibration, CalibrationError
from oiltank.cli.common import (
    BurstFrames,
    BurstSpacing,
    CalibrationPath,
    CameraUrl,
    fail,
    load_calibration,
    load_frames,
    load_settings,
    tools,
)
from oiltank.config import Settings
from oiltank.model import LevelState, Problem
from oiltank.pipeline import measure
from oiltank.publish.discovery import state_from_measurement
from oiltank.publish.mqtt import Event, EventKind, MqttError, MqttLink
from oiltank.sources.base import CameraError
from oiltank.sources.http import HttpSource

app = typer.Typer(help="Publish to Home Assistant over MQTT.", no_args_is_help=True)
log = logging.getLogger(__name__)


class _Reader:
    """Takes a reading and turns it into a state and a gauge image; camera failures become a
    ``camera_unreachable`` state rather than an exit."""

    def __init__(
        self,
        settings: Settings,
        calibration: Calibration,
        images: list[Path] | None,
        url: str | None,
        frames: int | None,
        spacing: float | None,
    ) -> None:
        self._settings = settings
        self._calibration = calibration
        self._aligner, self._reader = tools(calibration)
        self._images = images
        self._url = url
        self._frames = frames or settings.camera.burst_frames
        self._spacing = settings.camera.burst_spacing_s if spacing is None else spacing

    def read(self) -> tuple[LevelState, bytes | None]:
        tank = self._settings.tank
        if self._images:
            burst = load_frames(self._images)
        else:
            try:
                with HttpSource.from_settings(self._settings.camera, url=self._url) as source:
                    burst = source.grab(self._frames, self._spacing)
            except CameraError as e:
                typer.echo(f"camera unreachable: {e}", err=True)
                state = LevelState(
                    ts=datetime.now(UTC),
                    fraction=None,
                    volume=None,
                    confidence=0.0,
                    t=None,
                    problem=Problem.CAMERA_UNREACHABLE,
                )
                return state, None
        try:
            m = measure(burst, self._aligner, self._reader, self._calibration)
        except CalibrationError as e:
            fail(str(e))
        state = state_from_measurement(m, tank.capacity)
        image = render(burst[-1].gray, m, self._calibration, capacity=tank.capacity, unit=tank.unit)
        if state.fraction is not None and state.volume is not None:
            typer.echo(
                f"level {state.fraction:.1%}, {state.volume:.0f} {tank.unit} "
                f"(confidence {state.confidence:.2f}, {len(burst)} frame(s))"
            )
        else:
            typer.echo(f"no reading: {state.problem} (confidence {state.confidence:.2f})")
        return state, encode_jpeg(image)


def _connect(settings: Settings, events: queue.Queue[Event], *, announce: bool = True) -> MqttLink:
    try:
        link = MqttLink(
            settings, events, client_id=f"{settings.mqtt.client_id}-cli", announce=announce
        )
    except MqttError as e:
        fail(str(e))
    try:
        link.start()
    except MqttError as e:
        fail(f"MQTT: {e}", 1)
    return link


@app.command()
def publish(
    images: Annotated[
        list[Path] | None,
        typer.Option(
            "--image",
            exists=True,
            dir_okay=False,
            readable=True,
            help="Read this image file instead of the camera. Repeat for a burst.",
        ),
    ] = None,
    url: CameraUrl = None,
    frames: BurstFrames = None,
    spacing: BurstSpacing = None,
    calibration_path: CalibrationPath = None,
    listen: Annotated[
        int,
        typer.Option(min=0, help="Stay connected this many seconds, answering Read now presses."),
    ] = 0,
) -> None:
    """Read the gauge once and publish discovery, state and the gauge image.

    With --listen N it stays connected for N seconds: each Read now press takes and publishes a
    new reading, and a Home Assistant restart gets discovery and state again. The device shows as
    available while connected, and unavailable once this exits. Don't run it alongside the
    service; they share the availability topic.
    """
    settings = load_settings()
    calibration = load_calibration(calibration_path)
    reader = _Reader(settings, calibration, images, url, frames, spacing)
    events: queue.Queue[Event] = queue.Queue()
    link = _connect(settings, events)
    topics = link.topics
    typer.echo(
        f"connected to {settings.mqtt.host}:{settings.mqtt.port}; discovery on {topics.discovery}"
    )
    try:
        state, jpeg = reader.read()
        link.publish_state(state)
        if jpeg is not None:
            link.publish_image(jpeg)
        typer.echo(f"published to {topics.state}" + (f" and {topics.image}" if jpeg else ""))
        deadline = time.monotonic() + listen
        if listen:
            typer.echo(f"listening for {listen} s (press Read now in Home Assistant)")
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                event = events.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            match event.kind:
                case EventKind.READ_NOW:
                    typer.echo("Read now pressed")
                    state, new_jpeg = reader.read()
                    link.publish_state(state)
                    if new_jpeg is not None:
                        jpeg = new_jpeg
                        link.publish_image(jpeg)
                case EventKind.HA_ONLINE:
                    time.sleep(random.uniform(0, 5))  # don't all answer HA's birth at once
                    link.publish_discovery()
                    link.publish_availability(True)
                    link.publish_state(state)
                    if jpeg is not None:
                        link.publish_image(jpeg)
                    typer.echo("Home Assistant restarted; republished")
                case EventKind.CONNECTED:
                    pass  # (re)connected: availability and discovery were already announced
    except KeyboardInterrupt:
        typer.echo("stopping")
    except MqttError as e:
        fail(f"MQTT: {e}", 1)
    finally:
        link.stop()
    typer.echo("disconnected" + (" (device now unavailable in Home Assistant)" if listen else ""))


@app.command()
def clear() -> None:
    """Remove the device from Home Assistant, and its retained messages from the broker."""
    settings = load_settings()
    link = _connect(settings, queue.Queue(), announce=False)
    try:
        link.clear()
    except MqttError as e:
        fail(f"MQTT: {e}", 1)
    finally:
        link.stop(offline=False)
    typer.echo(
        f"removed {link.topics.discovery} and the retained messages under {link.topics.base}"
    )
