"""Command-line entry point (``oiltank``)."""

from pathlib import Path
from typing import Annotated

import cv2
import typer

from oiltank import __version__
from oiltank.sources.files import load_frame

app = typer.Typer(
    name="oiltank",
    help="Read a heating-oil tank float gauge from camera frames.",
    no_args_is_help=True,
    add_completion=False,
)


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


@app.command()
def read(
    image: Annotated[
        Path,
        typer.Option(
            "--image", exists=True, dir_okay=False, readable=True, help="Image file to read."
        ),
    ],
) -> None:
    """Read the gauge from a single image.

    Gauge detection is not implemented yet; for now this decodes the image and reports its size.
    """
    frame = load_frame(image)
    w, h = frame.size
    typer.echo(f"{image}: {w}x{h} grayscale")
