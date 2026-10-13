"""Command-line entry point (``oiltank``)."""

import json
import logging
from datetime import UTC
from pathlib import Path
from typing import Annotated

import cv2
import typer
from pydantic import ValidationError

from oiltank import __version__
from oiltank.annotate import encode_jpeg, render, render_diagnostic
from oiltank.calibration import CalibrationError
from oiltank.cli import calibrate
from oiltank.cli.common import (
    BurstFrames,
    BurstSpacing,
    CalibrationPath,
    CameraUrl,
    fail,
    grab_frames,
    load_calibration,
    load_frames,
    tools,
    write_bytes,
    write_image,
)
from oiltank.config import Settings
from oiltank.model import Measurement
from oiltank.pipeline import measure

app = typer.Typer(
    name="oiltank",
    help="Read a heating-oil tank float gauge from camera frames.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(calibrate.app, name="calibrate")


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"oiltank {__version__} (OpenCV {cv2.__version__})")
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_print_version, is_eager=True, help="Show version and exit."
        ),
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Log in detail.")] = False,
) -> None:
    """Read a heating-oil tank float gauge from camera frames.

    Settings come from OILTANK_... environment variables or a .env file; see .env.example.
    """
    try:
        level = "DEBUG" if verbose else Settings().log_level
    except ValidationError:  # reported properly by the command that needs the settings
        level = "INFO"
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s", force=True)
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(max(logging.getLevelName(level), logging.WARNING))


def _summary(m: Measurement) -> dict[str, object]:
    return {
        "ts": m.ts.isoformat(),
        "ok": m.ok,
        "fraction": None if m.fraction is None else round(m.fraction, 4),
        "t": None if m.t is None else round(m.t, 4),
        "confidence": round(m.confidence, 3),
        "indicator": None if m.indicator is None else [round(v, 1) for v in m.indicator],
        "position": None if m.position is None else [round(v, 1) for v in m.position],
        "problem": m.problem,
        "notices": list(m.notices),
        "reader": m.reader,
        "frames": [
            {
                "source": r.frame.source,
                "alignment": {
                    "dx": round(r.alignment.dx, 2),
                    "dy": round(r.alignment.dy, 2),
                    "score": round(r.alignment.score, 3),
                    "ok": r.alignment.ok,
                },
                "t": None if r.reading is None or r.reading.t is None else round(r.reading.t, 4),
                "confidence": None if r.reading is None else round(r.reading.confidence, 3),
            }
            for r in m.frames
        ],
    }


@app.command()
def read(
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
    annotate: Annotated[
        Path | None, typer.Option("--annotate", help="Write an annotated JPEG of the gauge here.")
    ] = None,
    diagnostic: Annotated[
        Path | None,
        typer.Option(
            "--diagnostic",
            help="Write a larger image for checking the calibration (.jpg or .png): the disc "
            "outlined at the calibrated 0% and 100% positions, the detected disc, the reader's "
            "search area and the alignment anchor.",
        ),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the result as JSON.")] = False,
) -> None:
    """Read the gauge: a burst of frames from the camera, or image files with --image."""
    if images and (url or frames or spacing is not None):
        fail("--url, --frames and --spacing are for the camera; leave them out with --image")
    calibration = load_calibration(calibration_path)
    aligner, reader = tools(calibration)
    burst = load_frames(images) if images else grab_frames(url, frames, spacing)
    try:
        m = measure(burst, aligner, reader, calibration)
    except CalibrationError as e:
        fail(str(e))

    if annotate is not None:
        write_bytes(annotate, encode_jpeg(render(burst[-1].gray, m, calibration)))
    if diagnostic is not None:
        write_image(diagnostic, render_diagnostic(burst[-1].gray, calibration, m))

    if as_json:
        typer.echo(json.dumps(_summary(m), indent=2))
    elif m.ok and m.fraction is not None and m.t is not None and m.indicator is not None:
        x, y = m.indicator
        typer.echo(
            f"level {m.fraction:.1%}  (t={m.t:.3f}, confidence {m.confidence:.2f}, "
            f"indicator at x={x:.1f} y={y:.1f}, {len(burst)} frame(s))"
        )
    else:
        typer.echo(f"no reading: {m.problem}  (confidence {m.confidence:.2f})")
    for notice in m.notices:
        typer.echo(f"notice: {notice}", err=True)
    if not m.ok:
        raise typer.Exit(1)


@app.command()
def grab(
    out: Annotated[
        Path,
        typer.Option(
            "--out", help="File to write (frame.jpg), or an existing directory for named files."
        ),
    ],
    url: CameraUrl = None,
    frames: Annotated[int, typer.Option("--frames", min=1, max=30, help="How many.")] = 1,
    spacing: BurstSpacing = None,
) -> None:
    """Save frames from the camera exactly as received, e.g. for calibration or test samples."""
    burst = grab_frames(url, frames, spacing)
    for k, frame in enumerate(burst, start=1):
        if out.is_dir():
            stamp = frame.ts.astimezone(UTC).strftime("%Y%m%d-%H%M%S")
            path = out / f"frame-{stamp}-{k}.jpg"
        elif len(burst) == 1:
            path = out
        else:
            path = out.with_name(f"{out.stem}-{k}{out.suffix or '.jpg'}")
        assert frame.data is not None
        write_bytes(path, frame.data)
        w, h = frame.size
        typer.echo(f"wrote {path} ({w}x{h}, {len(frame.data) // 1024} KB)")
