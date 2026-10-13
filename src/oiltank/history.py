"""Every reading attempt and refill, in SQLite at ``<data_dir>/oiltank.db``.

Readings keep the gauge position ``t`` as well as the fraction it mapped to at the time, so that
after a recalibration the service can replay them with the new mapping. Refills are stored too, so
"last fill" survives after it drops out of the replay window.
"""

import sqlite3
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from oiltank.filter import Fill, Observation
from oiltank.model import Problem

SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,             -- ISO 8601, UTC
    fraction REAL,                -- NULL when there was no trustworthy reading
    confidence REAL NOT NULL,
    t REAL,
    problem TEXT,
    accepted INTEGER NOT NULL,    -- passed the filter's gate
    frames INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS readings_ts ON readings (ts);
CREATE TABLE IF NOT EXISTS fills (
    ts TEXT PRIMARY KEY,
    before REAL NOT NULL,
    after REAL NOT NULL
);
"""


class History:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Only the service loop uses it, but that loop may run on a thread other than its creator.
        self._db = sqlite3.connect(path, check_same_thread=False)
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"{path} is from a newer oiltank (schema {version})")
        with self._db:
            self._db.executescript(_SCHEMA)
            self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def add(self, obs: Observation, *, accepted: bool, frames: int) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO readings (ts, fraction, confidence, t, problem, accepted, frames) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    obs.ts.isoformat(),
                    obs.fraction,
                    obs.confidence,
                    obs.t,
                    None if obs.problem is None else str(obs.problem),
                    int(accepted),
                    frames,
                ),
            )

    def add_fill(self, fill: Fill) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO fills (ts, before, after) VALUES (?, ?, ?)",
                (fill.ts.isoformat(), fill.before, fill.after),
            )

    def observations(self, since: datetime) -> list[Observation]:
        rows = self._db.execute(
            "SELECT ts, fraction, confidence, t, problem FROM readings WHERE ts >= ? "
            "ORDER BY ts, id",
            (since.isoformat(),),
        )
        return [
            Observation(
                ts=datetime.fromisoformat(ts),
                fraction=fraction,
                confidence=confidence,
                t=t,
                problem=None if problem is None else Problem(problem),
            )
            for ts, fraction, confidence, t, problem in rows
        ]

    def last_fill(self, before: datetime | None = None) -> Fill | None:
        query = "SELECT ts, before, after FROM fills"
        args: tuple[str, ...] = ()
        if before is not None:
            query += " WHERE ts < ?"
            args = (before.isoformat(),)
        row = self._db.execute(query + " ORDER BY ts DESC LIMIT 1", args).fetchone()
        if row is None:
            return None
        ts, before_level, after_level = row
        return Fill(ts=datetime.fromisoformat(ts), before=before_level, after=after_level)

    def fills(self) -> list[Fill]:
        rows = self._db.execute("SELECT ts, before, after FROM fills ORDER BY ts")
        return [Fill(datetime.fromisoformat(ts), b, a) for ts, b, a in rows]

    def count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM readings").fetchone()[0])
