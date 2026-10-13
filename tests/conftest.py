from pathlib import Path

import pytest

from oiltank.calibration import Calibration
from oiltank.geometry import Aligner
from oiltank.model import Frame
from oiltank.readers import GaugeReader, ReaderConfig, build_reader
from oiltank.sources.files import load_frame

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
SAMPLE_CALIBRATION = SAMPLES / "calibration.yaml"


@pytest.fixture(scope="session")
def sample_frame_paths() -> list[Path]:
    """The six native-4K go2rtc frames in ``samples/``."""
    paths = sorted(SAMPLES.glob("frame*.jpg"))
    assert len(paths) == 6, f"expected 6 sample frames in {SAMPLES}, found {len(paths)}"
    return paths


@pytest.fixture(scope="session")
def sample_frames(sample_frame_paths: list[Path]) -> list[Frame]:
    return [load_frame(p) for p in sample_frame_paths]


@pytest.fixture(scope="session")
def calibration() -> Calibration:
    return Calibration.load(SAMPLE_CALIBRATION)


@pytest.fixture(scope="session")
def aligner(calibration: Calibration) -> Aligner:
    return Aligner(calibration.anchor, calibration.anchor_patch)


@pytest.fixture(scope="session")
def reader(calibration: Calibration) -> GaugeReader:
    return build_reader(ReaderConfig(), calibration)
