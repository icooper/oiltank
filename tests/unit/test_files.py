from pathlib import Path

import pytest

from oiltank.sources.files import FrameDecodeError, decode_gray, load_frame


def test_load_sample_frame_is_4k_gray(sample_frame_paths: list[Path]) -> None:
    frame = load_frame(sample_frame_paths[0])
    assert frame.size == (3840, 2160)
    assert frame.gray.ndim == 2
    assert frame.source.endswith("frame0.jpg")


def test_decode_garbage_raises() -> None:
    with pytest.raises(FrameDecodeError):
        decode_gray(b"not an image", source="test")
