import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import cv2
import pytest
import typer

from oiltank.archive import Archive, ArchivePolicy
from oiltank.calibration import Calibration
from oiltank.cli.common import parse_duration
from oiltank.geometry import Aligner
from oiltank.model import Frame
from oiltank.pipeline import measure
from oiltank.readers import GaugeReader
from tests.cv import synth


def _frames_at(frames: list[Frame], ts: datetime) -> list[Frame]:
    return [Frame(gray=f.gray, ts=ts, source=f.source, data=f.data) for f in frames]


def test_record_contents(
    tmp_path: Path,
    sample_frames: list[Frame],
    aligner: Aligner,
    reader: GaugeReader,
    calibration: Calibration,
) -> None:
    frames = _frames_at(sample_frames[:3], datetime(2026, 10, 13, 3, 59, 20, tzinfo=UTC))
    m = measure(frames, aligner, reader, calibration)
    archive = Archive(tmp_path, ArchivePolicy())
    path = archive.record(m, frames, calibration)
    assert path == tmp_path / "2026-10-13" / "035920Z.json"
    meta = json.loads(path.read_text())
    assert meta["t"] == pytest.approx(m.t)
    assert meta["problem"] is None
    assert len(meta["frames"]) == 3
    assert meta["calibration"]["frame_size"] == [3840, 2160]
    assert meta["calibration"]["reference_image"] == "frame0.jpg"

    strip = cv2.imread(str(path.with_name(meta["strip"])), cv2.IMREAD_GRAYSCALE)
    assert strip is not None
    assert strip.shape == (256, 128)
    # The label: the disc's strip row maps to the reading's t.
    row = meta["strip_row"]
    t_row = meta["strip_t_first_row"] + (meta["strip_t_last_row"] - meta["strip_t_first_row"]) * (
        row / 255
    )
    assert t_row == pytest.approx(meta["frames"][meta["strip_frame"]]["t"], abs=1e-3)

    # The first record keeps a full frame, byte for byte.
    assert path.with_name(meta["frame"]).read_bytes() == frames[-1].data


def test_full_frame_cadence_and_problem_frames(
    tmp_path: Path,
    sample_frames: list[Frame],
    aligner: Aligner,
    reader: GaugeReader,
    calibration: Calibration,
) -> None:
    archive = Archive(tmp_path, ArchivePolicy(full_frame_every=timedelta(hours=6)))
    t0 = datetime(2026, 10, 13, 0, 0, tzinfo=UTC)

    def record(minutes: int, problem: bool = False) -> dict[str, object]:
        frames = _frames_at(sample_frames[:1], t0 + timedelta(minutes=minutes))
        if problem:  # no disc: a low_confidence reading
            gray = synth.remove_disc(frames[0].gray)
            frames = [Frame(gray=gray, ts=frames[0].ts, source="x", data=b"jpeg")]
        m = measure(frames, aligner, reader, calibration)
        path = archive.record(m, frames, calibration)
        assert path is not None
        meta: dict[str, object] = json.loads(path.read_text())
        return meta

    assert record(0)["frame"] is not None  # first: keep
    assert record(15)["frame"] is None
    assert record(30, problem=True)["frame"] is not None  # problems always keep theirs
    assert record(6 * 60 - 1)["frame"] is None
    assert record(6 * 60)["frame"] is not None  # six hours after the last kept one


def test_size_limit_drops_the_oldest(
    tmp_path: Path,
    sample_frames: list[Frame],
    aligner: Aligner,
    reader: GaugeReader,
    calibration: Calibration,
) -> None:
    archive = Archive(tmp_path, ArchivePolicy(max_bytes=1_500_000, full_frame_every=timedelta(0)))
    t0 = datetime(2026, 10, 12, 23, 50, tzinfo=UTC)
    for k in range(8):  # ~350 KB each, across midnight
        frames = _frames_at(sample_frames[:1], t0 + timedelta(minutes=5 * k))
        archive.record(measure(frames, aligner, reader, calibration), frames, calibration)
    total = sum(f.stat().st_size for f in tmp_path.rglob("*") if f.is_file())
    assert total <= 1_500_000
    assert archive.size() == total
    assert not (tmp_path / "2026-10-12").exists()  # the old day went first, directory too
    newest = sorted((tmp_path / "2026-10-13").glob("*.json"))[-1]
    assert newest.name == "002500Z.json"


def test_same_second_records_dont_collide(
    tmp_path: Path,
    sample_frames: list[Frame],
    aligner: Aligner,
    reader: GaugeReader,
    calibration: Calibration,
) -> None:
    archive = Archive(tmp_path, ArchivePolicy())
    frames = _frames_at(sample_frames[:1], datetime(2026, 10, 13, 1, 2, 3, tzinfo=UTC))
    m = measure(frames, aligner, reader, calibration)
    first = archive.record(m, frames, calibration)
    second = archive.record(m, frames, calibration)
    assert first is not None and second is not None
    assert second.name == "010203Z-2.json"


def test_nothing_to_record(tmp_path: Path, calibration: Calibration) -> None:
    assert Archive(tmp_path, ArchivePolicy()).record(None, [], calibration) is None
    assert not any(tmp_path.iterdir())


def test_capture_policy_keeps_every_frame() -> None:
    policy = ArchivePolicy.capture()
    assert policy.max_bytes is None
    assert policy.full_frame_every == timedelta(0)


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("30s", 30),
        ("5m", 300),
        ("1h", 3600),
        ("1h30m", 5400),
        ("90", 90),
        ("2m30s", 150),
        ("0.5s", 0.5),
    ],
)
def test_parse_duration(text: str, seconds: float) -> None:
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "abc", "5x", "m"])
def test_parse_duration_rejects(text: str) -> None:
    with pytest.raises(typer.BadParameter):
        parse_duration(text)
