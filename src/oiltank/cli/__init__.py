"""Command-line entry point (``oiltank``)."""

import json
from pathlib import Path
from typing import Annotated

import cv2
import typer

from oiltank import __version__
from oiltank.annotate import encode_jpeg, render, render_diagnostic
from oiltank.calibration import CalibrationError
from oiltank.cli import calibrate
from oiltank.cli.common import (
    DEFAULT_CALIBRATION,
    CalibrationPath,
    fail,
    load_calibration,
    load_frames,
    tools,
    write_image,
)
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
) -> None:
    """Read a heating-oil tank float gauge from camera frames."""


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
        list[Path],
        typer.Option(
            "--image",
            exists=True,
            dir_okay=False,
            readable=True,
            help="Image file to read. Repeat to read several frames as one burst.",
        ),
    ],
    calibration_path: CalibrationPath = DEFAULT_CALIBRATION,
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
    """Read the gauge level from one or more image files."""
    calibration = load_calibration(calibration_path)
    aligner, reader = tools(calibration)
    frames = load_frames(images)
    try:
        m = measure(frames, aligner, reader, calibration)
    except CalibrationError as e:
        fail(str(e))

    if annotate is not None:
        data = encode_jpeg(render(frames[-1].gray, m, calibration))
        try:
            annotate.write_bytes(data)
        except OSError as e:
            fail(f"cannot write {annotate}: {e}")
    if diagnostic is not None:
        write_image(diagnostic, render_diagnostic(frames[-1].gray, calibration, m))

    if as_json:
        typer.echo(json.dumps(_summary(m), indent=2))
    elif m.ok and m.fraction is not None and m.t is not None and m.indicator is not None:
        x, y = m.indicator
        typer.echo(
            f"level {m.fraction:.1%}  (t={m.t:.3f}, confidence {m.confidence:.2f}, "
            f"indicator at x={x:.1f} y={y:.1f}, {len(frames)} frame(s))"
        )
    else:
        typer.echo(f"no reading: {m.problem}  (confidence {m.confidence:.2f})")
    for notice in m.notices:
        typer.echo(f"notice: {notice}", err=True)
    if not m.ok:
        raise typer.Exit(1)
