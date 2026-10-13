from pathlib import Path

import pytest

from oiltank.calibration import Calibration
from oiltank.geometry import Aligner
from oiltank.model import Frame
from oiltank.readers import GaugeReader, ReaderConfig, build_reader
from oiltank.sources.files import load_frame

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
SAMPLE_CALIBRATION = SAMPLES / "calibration.yaml"

# Native-4K go2rtc frames, all with the tank at about 98%. frame0 is the calibration reference.
DAY = tuple(f"frame{i}.jpg" for i in range(6))
"""2026-10-12 17:03 local: daylight through the basement window, IR on the gauge."""
NIGHT = tuple(f"frame{i}.jpg" for i in range(6, 10))
"""2026-10-12 22:44-22:47 local: IR only; the window is dark and the exposure a little brighter."""


@pytest.fixture(scope="session")
def sample_frame_paths() -> list[Path]:
    """Every sample frame: day, then night."""
    return [SAMPLES / name for name in (*DAY, *NIGHT)]


@pytest.fixture(scope="session")
def sample_frames(sample_frame_paths: list[Path]) -> list[Frame]:
    return [load_frame(p) for p in sample_frame_paths]


@pytest.fixture(scope="session")
def day_frames(sample_frames: list[Frame]) -> list[Frame]:
    return sample_frames[: len(DAY)]


@pytest.fixture(scope="session")
def night_frames(sample_frames: list[Frame]) -> list[Frame]:
    return sample_frames[len(DAY) :]


@pytest.fixture(scope="session")
def calibration() -> Calibration:
    return Calibration.load(SAMPLE_CALIBRATION)


@pytest.fixture(scope="session")
def aligner(calibration: Calibration) -> Aligner:
    return Aligner(calibration.anchor, calibration.anchor_patch)


@pytest.fixture(scope="session")
def reader(calibration: Calibration) -> GaugeReader:
    return build_reader(ReaderConfig(), calibration)
