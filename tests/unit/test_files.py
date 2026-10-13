from pathlib import Path

import pytest

from oiltank.sources.files import FrameDecodeError, decode_gray, load_frame
from tests.conftest import DAY, NIGHT, SAMPLES


def test_every_sample_frame_is_in_a_test_set() -> None:
    found = {p.name for p in SAMPLES.glob("frame*.jpg")}
    assert found == {*DAY, *NIGHT}, "list new sample frames in DAY or NIGHT in tests/conftest.py"


def test_sample_frames_are_4k_gray(sample_frame_paths: list[Path]) -> None:
    for path in sample_frame_paths:
        frame = load_frame(path)
        assert frame.size == (3840, 2160)
        assert frame.gray.ndim == 2
        assert frame.source.endswith(path.name)


def test_decode_garbage_raises() -> None:
    with pytest.raises(FrameDecodeError):
        decode_gray(b"not an image", source="test")
