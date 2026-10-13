import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
import pytest
import typer

from oiltank.archive import EVICT_TO, Archive, ArchivePolicy, ArchiveReader
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
    assert total <= 1_500_000 * EVICT_TO
    assert archive.size() == total
    assert not (tmp_path / "2026-10-12").exists()  # the old day went first, directory too
    newest = sorted((tmp_path / "2026-10-13").glob("*.json"))[-1]
    assert newest.name == "002500Z.json"


def _assert_whole_records(root: Path) -> None:
    """Every file belongs to a record whose JSON exists, and every JSON's files exist."""
    for day in root.iterdir():
        names = {f.name for f in day.iterdir()}
        for name in names:
            if name.endswith(".json"):
                meta = json.loads((day / name).read_text())
                assert {meta["strip"], meta["frame"]} - {None} <= names
            else:
                stem = name.removesuffix("-strip.png").removesuffix("-frame.jpg")
                assert f"{stem}.json" in names, f"{name} outlived its record"


def test_eviction_deletes_whole_records(
    tmp_path: Path,
    sample_frames: list[Frame],
    aligner: Aligner,
    reader: GaugeReader,
    calibration: Calibration,
) -> None:
    archive = Archive(tmp_path, ArchivePolicy(max_bytes=1_000_000, full_frame_every=timedelta(0)))
    t0 = datetime(2026, 10, 12, 23, 59, tzinfo=UTC)
    m = None
    for k in range(10):  # in pairs within the same second (-2 records), across midnight
        frames = _frames_at(sample_frames[:1], t0 + timedelta(minutes=k // 2))
        m = m or measure(frames, aligner, reader, calibration)
        archive.record(m, frames, calibration)
        _assert_whole_records(tmp_path)
        assert archive.size() <= 1_000_000
    assert not list(tmp_path.rglob(".*"))  # no temporary files left behind
    assert sorted(f.name for f in (tmp_path / "2026-10-13").glob("*.json"))[-2:] == [
        "000300Z-2.json",
        "000300Z.json",
    ]


def test_a_crashed_write_is_cleaned_up_with_its_day(
    tmp_path: Path,
    sample_frames: list[Frame],
    aligner: Aligner,
    reader: GaugeReader,
    calibration: Calibration,
) -> None:
    old_day = tmp_path / "2026-10-01"
    old_day.mkdir()
    (old_day / ".120000Z.json.tmp").write_bytes(b"{")
    (old_day / "120000Z-strip.png").write_bytes(b"png")  # its record never got its JSON
    archive = Archive(tmp_path, ArchivePolicy(max_bytes=700_000, full_frame_every=timedelta(0)))
    for k in range(3):
        frames = _frames_at(sample_frames[:1], datetime(2026, 10, 13, 0, k, tzinfo=UTC))
        archive.record(measure(frames, aligner, reader, calibration), frames, calibration)
    assert not old_day.exists()


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


# ---- reading records back -------------------------------------------------------------------


def _write_day(
    root: Path,
    frames: list[Frame],
    aligner: Aligner,
    reader: GaugeReader,
    calibration: Calibration,
    minutes: list[int],
) -> list[Path]:
    archive = Archive(root, ArchivePolicy(full_frame_every=timedelta(hours=1)))
    paths = []
    for k, minute in enumerate(minutes):
        burst = _frames_at(
            frames[k % len(frames) : k % len(frames) + 2], T0 + timedelta(minutes=minute)
        )
        path = archive.record(measure(burst, aligner, reader, calibration), burst, calibration)
        assert path is not None
        paths.append(path)
    return paths


T0 = datetime(2026, 10, 13, 3, 0, tzinfo=UTC)


def test_records_read_back(
    tmp_path: Path,
    sample_frames: list[Frame],
    aligner: Aligner,
    reader: GaugeReader,
    calibration: Calibration,
) -> None:
    _write_day(tmp_path, sample_frames, aligner, reader, calibration, [0, 15, 15, 30, 90])
    archive = ArchiveReader(tmp_path)
    assert archive.days() == ["2026-10-13"]
    records = archive.records("2026-10-13")
    assert [r.key for r in records] == ["030000Z", "031500Z", "031500Z-2", "033000Z", "043000Z"]
    first = records[0]
    assert first.meta.ts == T0
    assert first.meta.t is not None and first.meta.fraction == pytest.approx(0.98, abs=0.01)
    assert first.meta.calibration is not None
    assert first.meta.calibration.reference_image == "frame0.jpg"
    assert len(first.meta.frames) == 2
    assert first.strip_path is not None and first.frame_path is not None  # first: a full frame
    assert records[1].frame_path is None
    assert archive.record("2026-10-13", "031500Z-2") is records[2]
    assert archive.record("2026-10-13", "../x") is None
    assert archive.records("2026-10-14") == [] and archive.records("..") == []


def test_a_rebuilt_strip_reads_like_the_original(
    tmp_path: Path,
    sample_frames: list[Frame],
    aligner: Aligner,
    reader: GaugeReader,
    calibration: Calibration,
) -> None:
    _write_day(tmp_path, sample_frames, aligner, reader, calibration, [0])
    [record] = ArchiveReader(tmp_path).records("2026-10-13")
    strip = record.strip()
    assert strip is not None
    assert strip.t_of_row[0] == pytest.approx(record.meta.strip_t_first_row)
    assert strip.t_of_row[-1] == pytest.approx(record.meta.strip_t_last_row)
    again = reader.read(strip)
    assert record.meta.strip_row is not None
    assert again.row == pytest.approx(record.meta.strip_row, abs=0.5)
    assert np.isfinite(again.scores).sum() > 100  # the per-row scores the archive doesn't keep


def test_partial_legacy_and_broken_records(tmp_path: Path, calibration: Calibration) -> None:
    day = tmp_path / "2026-10-13"
    day.mkdir()
    snapshot = {
        "frame_size": [3840, 2160],
        "axis": calibration.axis.model_dump(mode="json"),
        "disc": calibration.disc.model_dump(mode="json"),
    }
    # A reading from before records named their reference image, whose strip has gone.
    (day / "010000Z.json").write_text(
        json.dumps(
            {
                "ts": "2026-10-13T01:00:00+00:00",
                "t": 0.8,
                "strip": "010000Z-strip.png",
                "frame": None,
                "calibration": snapshot,
            }
        )
    )
    (day / "020000Z.json").write_text("{not json")
    (day / ".030000Z.json.tmp").write_text("{}")
    (day / "notes.txt").write_text("hi")
    [record] = ArchiveReader(tmp_path).records("2026-10-13")
    assert record.key == "010000Z" and record.meta.t == 0.8
    assert record.strip_path is None and record.strip() is None
    assert record.meta.calibration is not None and record.meta.calibration.reference_image is None


def test_records_pick_up_new_ones(
    tmp_path: Path,
    sample_frames: list[Frame],
    aligner: Aligner,
    reader: GaugeReader,
    calibration: Calibration,
) -> None:
    _write_day(tmp_path, sample_frames, aligner, reader, calibration, [0])
    archive = ArchiveReader(tmp_path)
    assert len(archive.records("2026-10-13")) == 1
    _write_day(tmp_path, sample_frames, aligner, reader, calibration, [15])
    assert len(archive.records("2026-10-13")) == 2


@pytest.mark.parametrize(
    ("day", "name", "ok"),
    [
        ("2026-10-13", "030000Z.json", True),
        ("2026-10-13", "030000Z-strip.png", True),
        ("2026-10-13", "030000Z-frame.jpg", True),
        ("2026-10-13", "missing.json", False),
        ("2026-10-13", "../../etc/passwd", False),
        ("2026-10-13", "%2e%2e/x.json", False),
        ("..", "030000Z.json", False),
        ("2026-10-13", "/etc/passwd", False),
        ("2026-10-13", "030000Z.json/..", False),
    ],
)
def test_only_record_files_are_served(tmp_path: Path, day: str, name: str, ok: bool) -> None:
    folder = tmp_path / "2026-10-13"
    folder.mkdir()
    for f in ("030000Z.json", "030000Z-strip.png", "030000Z-frame.jpg"):
        (folder / f).write_text("x")
    path = ArchiveReader(tmp_path).path(day, name)
    assert (path is not None) is ok


def test_nearest_full_frame(
    tmp_path: Path,
    sample_frames: list[Frame],
    aligner: Aligner,
    reader: GaugeReader,
    calibration: Calibration,
) -> None:
    # Full frames hourly: at 03:00 and 04:30 (the first after an hour), not 03:15 or 03:30.
    _write_day(tmp_path, sample_frames, aligner, reader, calibration, [0, 15, 30, 90])
    archive = ArchiveReader(tmp_path)
    near = archive.nearest_full_frame(T0 + timedelta(minutes=20), timedelta(hours=2))
    assert near is not None and near.key == "030000Z"
    later = archive.nearest_full_frame(T0 + timedelta(minutes=70), timedelta(hours=2))
    assert later is not None and later.key == "043000Z"
    assert archive.nearest_full_frame(T0 + timedelta(days=3), timedelta(hours=2)) is None
