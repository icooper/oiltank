"""Helpers shared by the CLI commands."""

import errno
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, NoReturn

import cv2
import numpy as np
import typer
from pydantic import ValidationError

from oiltank.calibration import Calibration, CalibrationError
from oiltank.config import (
    Settings,
    settings_error,
    unknown_variables,
    unknown_variables_message,
)
from oiltank.geometry import Aligner
from oiltank.model import Frame
from oiltank.readers import GaugeReader, ReaderConfig, build_reader
from oiltank.sources.base import CameraConfigError, CameraError
from oiltank.sources.files import FrameDecodeError, load_frame
from oiltank.sources.http import HttpSource

CalibrationPath = Annotated[
    Path | None,
    typer.Option(
        "--calibration",
        "-c",
        help="Calibration YAML. Default: calibration.yaml in OILTANK_DATA_DIR (./data).",
    ),
]
CameraUrl = Annotated[
    str | None,
    typer.Option("--url", help="Fetch frames from this URL instead of OILTANK_CAMERA_URL."),
]
BurstFrames = Annotated[
    int | None,
    typer.Option(
        "--frames", min=1, max=30, help="Frames per burst. Default: OILTANK_CAMERA_BURST_FRAMES."
    ),
]
BurstSpacing = Annotated[
    float | None,
    typer.Option(
        "--spacing",
        min=0,
        help="Seconds between frames. Default: OILTANK_CAMERA_BURST_SPACING_S.",
    ),
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


def load_settings() -> Settings:
    """Settings from the environment and ``.env`` (see ``.env.example``)."""
    if unknown := unknown_variables():
        fail(unknown_variables_message(unknown))
    try:
        return Settings()
    except ValidationError as e:
        fail(f"invalid settings: {settings_error(e)}")


def calibration_file(path: Path | None) -> Path:
    """``path``, or the calibration in the configured data directory."""
    return path if path is not None else load_settings().calibration_path


def load_calibration(path: Path | None) -> Calibration:
    path = calibration_file(path)
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


def grab_frames(
    url: str | None = None, frames: int | None = None, spacing: float | None = None
) -> list[Frame]:
    """A burst from the camera; burst size and spacing default to the configured ones."""
    camera = load_settings().camera
    n = frames or camera.burst_frames
    gap = camera.burst_spacing_s if spacing is None else spacing
    try:
        with HttpSource.from_settings(camera, url=url) as source:
            spread = f", {gap:g} s apart" if n > 1 else ""
            typer.echo(f"fetching {n} frame(s) from {source.host}{spread}", err=True)
            return source.grab(n, gap)
    except CameraConfigError as e:
        fail(f"camera: {e}")
    except CameraError as e:
        fail(f"camera unreachable: {e}", 1)


def tools(calibration: Calibration) -> tuple[Aligner, GaugeReader]:
    """The aligner and reader for ``calibration``."""
    try:
        return Aligner(calibration.anchor, calibration.anchor_patch), build_reader(
            ReaderConfig(), calibration
        )
    except (OSError, FrameDecodeError, CalibrationError) as e:
        fail(f"cannot use the reference frame: {e}")


def write_bytes(path: Path, data: bytes) -> None:
    try:
        path.write_bytes(data)
    except OSError as e:
        hint = " (in the container, write under /data)" if e.errno == errno.EROFS else ""
        fail(f"cannot write {path}: {e.strerror}{hint}")


def write_image(path: Path, image: np.ndarray) -> None:
    """Write ``image`` in the format its suffix names (JPEG unless it's .png)."""
    if path.suffix.lower() == ".png":
        ok, buf = cv2.imencode(".png", image)
    else:
        ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        fail(f"could not encode {path}")
    write_bytes(path, buf.tobytes())
