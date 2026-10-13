import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from oiltank.calibration import Axis
from oiltank.epochs import Change, successor
from oiltank.filter import Fill, Observation
from oiltank.history import History
from oiltank.model import Problem

T0 = datetime(2026, 11, 1, 6, 0, tzinfo=UTC)


def test_readings_round_trip(tmp_path: Path) -> None:
    with History(tmp_path / "data" / "oiltank.db") as h:
        good = Observation(T0, 0.81, 0.97, 0.8, None)
        bad = Observation(T0 + timedelta(minutes=15), None, 0.0, None, Problem.CAMERA_MOVED)
        h.add(good, accepted=True, frames=5)
        h.add(bad, accepted=False, frames=5)
        assert h.observations(T0) == [good, bad]
        assert h.observations(T0 + timedelta(minutes=1)) == [bad]
        assert h.count() == 2


def test_fills(tmp_path: Path) -> None:
    with History(tmp_path / "oiltank.db") as h:
        assert h.last_fill() is None
        old = Fill(T0, 0.2, 0.9)
        new = Fill(T0 + timedelta(days=30), 0.25, 0.95)
        h.add_fill(old)
        h.add_fill(new)
        h.add_fill(new)  # the same fill again (e.g. found again by a replay) is stored once
        assert h.fills() == [old, new]
        assert h.last_fill() == new
        assert h.last_fill(before=T0 + timedelta(days=1)) == old


def test_survives_reopening(tmp_path: Path) -> None:
    path = tmp_path / "oiltank.db"
    with History(path) as h:
        h.add(Observation(T0, 0.5, 1.0, 0.5, None), accepted=True, frames=1)
    with History(path) as h:
        assert h.count() == 1


def test_refuses_a_newer_schema(tmp_path: Path) -> None:
    path = tmp_path / "oiltank.db"
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version = 99")
    db.close()
    with pytest.raises(RuntimeError, match="newer oiltank"):
        History(path)


SAMPLE_AXIS = Axis(top=(2166.0, 1020.0), bottom=(2166.0, 1172.0), half_width=50.0)
LONGER_AXIS = SAMPLE_AXIS.updated(top=(2166.0, 990.0))

SCHEMA_1 = """
CREATE TABLE readings (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, fraction REAL, confidence REAL NOT NULL, t REAL,
    problem TEXT, accepted INTEGER NOT NULL, frames INTEGER NOT NULL
);
CREATE INDEX readings_ts ON readings (ts);
CREATE TABLE fills (ts TEXT PRIMARY KEY, before REAL NOT NULL, after REAL NOT NULL);
PRAGMA user_version = 1;
"""


def test_upgrades_schema_1_and_the_first_epoch_claims_its_readings(tmp_path: Path) -> None:
    path = tmp_path / "oiltank.db"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA_1)
    db.execute(
        "INSERT INTO readings (ts, fraction, confidence, t, problem, accepted, frames) "
        "VALUES (?, 0.5, 0.9, 0.6, NULL, 1, 5)",
        (T0.isoformat(),),
    )
    db.commit()
    db.close()
    with History(path) as h:
        epoch, change = h.register_epoch("frame0.jpg", SAMPLE_AXIS, T0)
        assert change is Change.FIRST
        assert epoch.id is not None
        later, change = h.register_epoch("frame0.jpg", LONGER_AXIS, T0 + timedelta(days=1))
        assert change is Change.AXIS_EDIT
        [obs] = h.observations(T0, onto=later)
        assert obs.t == pytest.approx(LONGER_AXIS.t_of(SAMPLE_AXIS.point(0.6)))
    with History(path) as h:  # reopening a schema 2 database changes nothing
        assert h.count() == 1
        assert h.current_epoch() == later


def test_readings_are_carried_onto_the_current_axis(tmp_path: Path) -> None:
    with History(tmp_path / "oiltank.db") as h:
        first, _ = h.register_epoch("frame0.jpg", SAMPLE_AXIS, T0)
        h.add(Observation(T0, 0.98, 0.9, 0.8, None), accepted=True, frames=5, epoch=first)
        longer, _ = h.register_epoch("frame0.jpg", LONGER_AXIS, T0 + timedelta(hours=1))
        h.add(
            Observation(T0 + timedelta(hours=1), 0.98, 0.9, 0.7, None),
            accepted=True,
            frames=5,
            epoch=longer,
        )
        moved = SAMPLE_AXIS.t_of(LONGER_AXIS.point(0.7))
        assert [o.t for o in h.observations(T0)] == [0.8, 0.7]  # as stored
        assert [o.t for o in h.observations(T0, onto=first)] == pytest.approx([0.8, moved])
        assert [o.t for o in h.observations(T0, onto=longer)] == pytest.approx(
            [LONGER_AXIS.t_of(SAMPLE_AXIS.point(0.8)), 0.7]
        )


def test_an_axis_used_before_gets_its_epoch_back(tmp_path: Path) -> None:
    with History(tmp_path / "oiltank.db") as h:
        first, _ = h.register_epoch("frame0.jpg", SAMPLE_AXIS, T0)
        assert h.register_epoch("frame0.jpg", SAMPLE_AXIS, T0) == (first, None)
        longer, _ = h.register_epoch("frame0.jpg", LONGER_AXIS, T0)
        again, change = h.register_epoch("frame0.jpg", SAMPLE_AXIS, T0)
        assert again == first
        assert change is Change.AXIS_EDIT
        assert h.current_epoch() == first
        assert set(h.epochs()) == {first.id, longer.id}


def test_epoch_for_stores_nothing(tmp_path: Path) -> None:
    with History(tmp_path / "oiltank.db") as h:
        first, _ = h.register_epoch("frame0.jpg", SAMPLE_AXIS, T0)
        assert h.epoch_for("frame0.jpg", SAMPLE_AXIS) == (first, None)
        draft, change = h.epoch_for("frame0.jpg", LONGER_AXIS)
        assert draft.id is None and change is Change.AXIS_EDIT
        assert h.current_epoch() == first
        assert list(h.epochs()) == [first.id]


def test_unregistered_epochs_cannot_be_stored(tmp_path: Path) -> None:
    with History(tmp_path / "oiltank.db") as h, pytest.raises(ValueError, match="register"):
        h.add(
            Observation(T0, 0.5, 0.9, 0.5, None),
            accepted=True,
            frames=1,
            epoch=successor(None, "frame0.jpg", SAMPLE_AXIS),
        )


def test_readers_see_writes_and_cannot_write(tmp_path: Path) -> None:
    path = tmp_path / "oiltank.db"
    with History(path) as writer:
        assert writer._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        epoch, _ = writer.register_epoch("frame0.jpg", SAMPLE_AXIS, T0)
        writer.add(Observation(T0, 0.9, 0.97, 0.75, None), accepted=True, frames=5, epoch=epoch)
        with History.open_readonly(path) as reader:
            assert reader.count() == 1
            writer.add(
                Observation(T0 + timedelta(minutes=15), None, 0.2, None, Problem.LOW_CONFIDENCE),
                accepted=False,
                frames=5,
                epoch=epoch,
            )
            assert reader.count() == 2  # while the writer is still open
            [first, second] = reader.readings(T0)
            assert (first.accepted, first.frames, first.epoch) == (True, 5, epoch.id)
            assert (second.accepted, second.problem) == (False, Problem.LOW_CONFIDENCE)
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                reader.add_fill(Fill(T0, 0.2, 0.9))


def test_reading_a_copy_in_a_read_only_directory(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root can write anywhere")
    copy = tmp_path / "copy"
    copy.mkdir()
    with History(copy / "oiltank.db") as h:
        h.add(Observation(T0, 0.9, 0.97, 0.75, None), accepted=True, frames=5)
    copy.chmod(0o555)
    try:
        with History.open_readonly(copy / "oiltank.db") as reader:
            assert reader.count() == 1
    finally:
        copy.chmod(0o755)


def test_read_only_needs_an_upgraded_history(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        History.open_readonly(tmp_path / "missing.db")
    old = tmp_path / "old.db"
    db = sqlite3.connect(old)
    db.executescript(SCHEMA_1)
    db.close()
    with pytest.raises(RuntimeError, match="upgrades it"):
        History.open_readonly(old)


def test_ranges(tmp_path: Path) -> None:
    with History(tmp_path / "oiltank.db") as h:
        for k in range(4):
            h.add(
                Observation(T0 + timedelta(hours=k), 0.5, 0.9, 0.5, None), accepted=True, frames=1
            )
            h.add_fill(Fill(T0 + timedelta(hours=k), 0.2, 0.9))
        window = (T0 + timedelta(hours=1), T0 + timedelta(hours=3))
        assert [r.ts.hour for r in h.readings(*window)] == [7, 8]
        assert [o.ts.hour for o in h.observations(*window)] == [7, 8]
        assert [f.ts.hour for f in h.fills(*window)] == [7, 8]
        assert len(h.fills()) == 4
