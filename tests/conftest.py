from pathlib import Path

import pytest

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


@pytest.fixture
def sample_frame_paths() -> list[Path]:
    """The six native-4K go2rtc frames in ``samples/``."""
    paths = sorted(SAMPLES.glob("frame*.jpg"))
    assert len(paths) == 6, f"expected 6 sample frames in {SAMPLES}, found {len(paths)}"
    return paths
