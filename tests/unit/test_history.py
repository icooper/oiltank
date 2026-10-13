import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

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
