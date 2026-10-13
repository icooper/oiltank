"""``oiltank capture``: record the gauge at short intervals, e.g. through an oil delivery."""

import signal
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Annotated

import typer

from oiltank.archive import Archive, ArchivePolicy
from oiltank.calibration import CalibrationError
from oiltank.cli.common import (
    BurstFrames,
    CalibrationPath,
    CameraUrl,
    fail,
    load_calibration,
    load_settings,
    parse_duration,
    tools,
)
from oiltank.pipeline import measure
from oiltank.sources.base import CameraConfigError, CameraError
from oiltank.sources.http import HttpSource


def capture(
    every: Annotated[
        str, typer.Option(help="Time between captures: 30s, 2m, ...", show_default=True)
    ] = "30s",
    duration: Annotated[
        str, typer.Option(help="How long to keep capturing: 1h, 45m, ...", show_default=True)
    ] = "1h",
    out: Annotated[
        Path | None,
        typer.Option(help="Directory for the capture. Default: <data dir>/captures/<start time>."),
    ] = None,
    frames: BurstFrames = None,
    url: CameraUrl = None,
    calibration_path: CalibrationPath = None,
) -> None:
    """Record full frames and readings at short intervals, e.g. through an oil delivery.

    Each capture keeps the camera's frame exactly as received, the gauge strip and the reading,
    in the archive's format. A delivery sweeps the disc across most of its travel, which makes
    it the best data there is for checking the reader and adding calibration points. Stop early
    with Ctrl-C; what's captured so far is kept. It can run alongside the service.
    """
    interval, total = parse_duration(every), parse_duration(duration)
    if interval <= 0:
        fail("--every must be more than 0")
    settings = load_settings()
    calibration = load_calibration(calibration_path)
    aligner, reader = tools(calibration)
    start = datetime.now(UTC)
    root = (out or settings.data_dir / "captures" / start.strftime("%Y%m%d-%H%M%SZ")).absolute()
    archive = Archive(root, ArchivePolicy.capture())
    n = frames or 1
    try:
        source = HttpSource.from_settings(settings.camera, url=url)
    except CameraConfigError as e:
        fail(f"camera: {e}")

    stop = threading.Event()

    def request_stop(signum: int, frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    count = int(total / interval + 1e-9) + 1  # tolerate 0.6 / 0.2 = 2.9999...
    typer.echo(
        f"capturing {count} time(s), every {interval:g} s for {total:g} s, into {root} "
        "(Ctrl-C stops early)"
    )
    ts_: list[float] = []
    failures = 0
    begun = datetime.now(UTC).timestamp()
    try:
        for k in range(count):
            due = begun + k * interval
            if stop.wait(max(0.0, due - datetime.now(UTC).timestamp())):
                break
            try:
                burst = source.grab(n, settings.camera.burst_spacing_s)
            except CameraError as e:
                failures += 1
                typer.echo(f"{datetime.now().strftime('%H:%M:%S')}  camera unreachable: {e}")
                continue
            try:
                m = measure(burst, aligner, reader, calibration)
            except CalibrationError as e:
                fail(str(e))
            try:
                archive.record(m, burst, calibration, full_frame=True)
            except OSError as e:
                fail(f"can't write to {root}: {e}", 1)
            when = burst[-1].ts.astimezone().strftime("%H:%M:%S")
            if m.ok and m.fraction is not None and m.t is not None:
                ts_.append(m.t)
                typer.echo(f"{when}  {m.fraction:6.1%}  t={m.t:.3f}  confidence {m.confidence:.2f}")
            else:
                failures += 1
                typer.echo(f"{when}  no reading: {m.problem} (confidence {m.confidence:.2f})")
    finally:
        source.close()
    summary = f"{len(ts_)} reading(s), {failures} failed"
    if ts_:
        lo, hi = min(ts_), max(ts_)
        summary += (
            f"; the disc moved between t={lo:.3f} ({calibration.fraction(lo):.0%}) and "
            f"t={hi:.3f} ({calibration.fraction(hi):.0%})"
        )
    typer.echo(f"{summary}. Saved in {root}")
