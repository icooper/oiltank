"""Helpers shared by the CLI commands."""

from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, NoReturn

import cv2
import numpy as np
import typer
from pydantic import ValidationError

from oiltank.calibration import Calibration, CalibrationError
from oiltank.geometry import Aligner
from oiltank.model import Frame
from oiltank.readers import GaugeReader, ReaderConfig, build_reader
from oiltank.sources.files import FrameDecodeError, load_frame

DEFAULT_CALIBRATION = Path("/data/calibration.yaml")

CalibrationPath = Annotated[
    Path, typer.Option("--calibration", "-c", help="Calibration YAML.", show_default=True)
]


def fail(message: str, code: int = 2) -> NoReturn:
    """Print ``message`` as an error and exit (2 = configuration or usage error)."""
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code)


def validation_message(e: ValidationError) -> str:
    """The first validation error, in words."""
    err = e.errors()[0]
    where = ".".join(str(part) for part in err["loc"])
    msg = str(err["msg"]).removeprefix("Value error, ")
    return f"{where}: {msg}" if where else msg


def load_calibration(path: Path) -> Calibration:
    try:
        return Calibration.load(path)
    except CalibrationError as e:
        fail(str(e))
    except ValidationError as e:
        fail(f"invalid calibration {path}: {validation_message(e)}")


def load_frames(paths: Sequence[Path]) -> list[Frame]:
    try:
        return [load_frame(p) for p in paths]
    except (OSError, FrameDecodeError) as e:
        fail(str(e))


def tools(calibration: Calibration) -> tuple[Aligner, GaugeReader]:
    """The aligner and reader for ``calibration``."""
    try:
        return Aligner(calibration.anchor, calibration.anchor_patch), build_reader(
            ReaderConfig(), calibration
        )
    except (OSError, FrameDecodeError, CalibrationError) as e:
        fail(f"cannot use the reference frame: {e}")


def write_image(path: Path, image: np.ndarray) -> None:
    """Write ``image`` in the format its suffix names (JPEG unless it's .png)."""
    if path.suffix.lower() == ".png":
        ok, buf = cv2.imencode(".png", image)
    else:
        ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        fail(f"could not encode {path}")
    try:
        path.write_bytes(buf.tobytes())
    except OSError as e:
        fail(f"cannot write {path}: {e}")
