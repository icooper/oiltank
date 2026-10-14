"""Every reading attempt and refill, in SQLite at ``<data_dir>/oiltank.db``.

Readings keep the gauge position ``t`` as well as the fraction it mapped to at the time, so that
after a recalibration the service can replay them with the new mapping. ``t`` is measured along the
calibration's axis, so each reading also records which axis that was (its epoch, see
:mod:`oiltank.epochs`) and is carried onto the current one when it's read back. Refills are stored
too, so "last fill" survives after it drops out of the replay window.
"""

import logging
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from oiltank.calibration import Axis
from oiltank.epochs import AxisEpoch, Change, carry, successor
from oiltank.filter import Fill, Observation
from oiltank.model import Problem

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2
_SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,             -- ISO 8601, UTC
    fraction REAL,                -- NULL when there was no trustworthy reading
    confidence REAL NOT NULL,
    t REAL,
    problem TEXT,
    accepted INTEGER NOT NULL,    -- passed the filter's gate
    frames INTEGER NOT NULL,
    epoch INTEGER                 -- the axis t is measured along (epochs.id)
);
CREATE INDEX IF NOT EXISTS readings_ts ON readings (ts);
CREATE TABLE IF NOT EXISTS fills (
    ts TEXT PRIMARY KEY,
    before REAL NOT NULL,
    after REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS epochs (
    id INTEGER PRIMARY KEY,
    since TEXT NOT NULL,          -- first used (ISO 8601, UTC)
    reference TEXT NOT NULL,      -- the calibration's reference_image
    top_x REAL NOT NULL,
    top_y REAL NOT NULL,
    bottom_x REAL NOT NULL,
    bottom_y REAL NOT NULL,
    scale REAL NOT NULL,          -- the first epoch's t is scale * t + shift
    shift REAL NOT NULL,
    change TEXT NOT NULL,         -- how it followed the epoch before it
    current INTEGER NOT NULL      -- 1 for the one in use
);
"""
_EPOCH_COLUMNS = "id, reference, top_x, top_y, bottom_x, bottom_y, scale, shift, change"


@dataclass(frozen=True, slots=True)
class StoredReading:
    """A reading attempt as stored, with what the filter made of it."""

    ts: datetime
    fraction: float | None
    confidence: float
    t: float | None
    problem: Problem | None
    accepted: bool
    frames: int
    epoch: int | None

    def observation(self) -> Observation:
        return Observation(self.ts, self.fraction, self.confidence, self.t, self.problem)


class History:
    def __init__(self, path: Path) -> None:
        """Open (creating or upgrading) the history for writing: the service's own connection.

        Other readers, such as the web UI, use :meth:`open_readonly`; WAL mode lets them read
        while the service writes.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        # Only the service loop uses it, but that loop may run on a thread other than its creator.
        self._db = sqlite3.connect(path, check_same_thread=False)
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"{path} is from a newer oiltank (schema {version})")
        mode = self._db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if mode != "wal":  # e.g. on a network filesystem
            log.warning("%s can't use WAL mode (%s); readers may wait for the service", path, mode)
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        with self._db:
            self._db.executescript(_SCHEMA)
            columns = {row[1] for row in self._db.execute("PRAGMA table_info(readings)")}
            if "epoch" not in columns:  # schema 1
                self._db.execute("ALTER TABLE readings ADD COLUMN epoch INTEGER")
            self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._has_epochs = True

    @classmethod
    def open_readonly(cls, path: Path) -> Self:
        """Open the history to read only: no schema changes, safe alongside the service.

        A history the service hasn't upgraded yet (schema 1, from before epochs) reads as if
        every reading were on the current axis, which is how the service treated them then.
        Raises OSError if there's no history, and RuntimeError if it's from a newer version.
        """
        if not path.is_file():
            raise FileNotFoundError(f"no history at {path}")
        uri = path.resolve().as_uri()
        try:
            db, version = _check_version(_connect(f"{uri}?mode=ro"), path)
        except sqlite3.OperationalError:
            # A read-only directory (a copy, mounted read-only) can't hold WAL's shared-memory
            # file; read the database file alone instead.
            db, version = _check_version(_connect(f"{uri}?immutable=1"), path)
        history = cls.__new__(cls)
        history._db = db
        history._has_epochs = version >= 2
        return history

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

    def add(
        self, obs: Observation, *, accepted: bool, frames: int, epoch: AxisEpoch | None = None
    ) -> None:
        """Store a reading attempt; ``epoch`` is the axis its ``t`` was measured along."""
        if epoch is not None and epoch.id is None:
            raise ValueError("register the epoch before storing readings in it")
        with self._db:
            self._db.execute(
                "INSERT INTO readings "
                "(ts, fraction, confidence, t, problem, accepted, frames, epoch) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    obs.ts.isoformat(),
                    obs.fraction,
                    obs.confidence,
                    obs.t,
                    None if obs.problem is None else str(obs.problem),
                    int(accepted),
                    frames,
                    None if epoch is None else epoch.id,
                ),
            )

    def add_fill(self, fill: Fill) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO fills (ts, before, after) VALUES (?, ?, ?)",
                (fill.ts.isoformat(), fill.before, fill.after),
            )

    def observations(
        self, since: datetime, until: datetime | None = None, *, onto: AxisEpoch | None = None
    ) -> list[Observation]:
        """Reading attempts from ``since`` (until before ``until``), oldest first.

        With ``onto``, each ``t`` is carried onto that epoch's axis. The fractions are as stored;
        recompute them from ``t`` with :meth:`Observation.recalibrated`.
        """
        return [r.observation() for r in self.readings(since, until, onto=onto)]

    def readings(
        self, since: datetime, until: datetime | None = None, *, onto: AxisEpoch | None = None
    ) -> list[StoredReading]:
        """Like :meth:`observations`, with what the filter made of each and its epoch."""
        query = (
            "SELECT ts, fraction, confidence, t, problem, accepted, frames, "
            f"{'epoch' if self._has_epochs else 'NULL'} FROM readings "
            "WHERE ts >= ?"
        )
        args = [since.isoformat()]
        if until is not None:
            query += " AND ts < ?"
            args.append(until.isoformat())
        rows = self._db.execute(query + " ORDER BY ts, id", args)
        epochs = {} if onto is None else self.epochs()
        out = []
        for ts, fraction, confidence, t, problem, accepted, frames, epoch in rows:
            src = epochs.get(epoch)
            if onto is not None and src is not None and t is not None and epoch != onto.id:
                a, b = carry(src, onto)
                t = a * t + b
            out.append(
                StoredReading(
                    ts=datetime.fromisoformat(ts),
                    fraction=fraction,
                    confidence=confidence,
                    t=t,
                    problem=None if problem is None else Problem(problem),
                    accepted=bool(accepted),
                    frames=frames,
                    epoch=epoch,
                )
            )
        return out

    # ---- epochs --------------------------------------------------------------------------------

    def epoch_for(self, reference: str, axis: Axis) -> tuple[AxisEpoch, Change | None]:
        """The epoch for ``axis`` on ``reference``, and how it follows the current one (None if
        it is the current one). Stores nothing: an epoch not seen before comes back without an
        ``id``. An axis used before gets its old epoch back."""
        current = self.current_epoch()
        if current is not None and current.matches(reference, axis):
            return current, None
        follows = successor(current, reference, axis)
        known = next((e for e in self.epochs().values() if e.matches(reference, axis)), None)
        return known or follows, follows.change

    def register_epoch(
        self, reference: str, axis: Axis, now: datetime
    ) -> tuple[AxisEpoch, Change | None]:
        """:meth:`epoch_for`, storing a new epoch and making the result the current one.

        The first epoch also claims readings stored before there were epochs.
        """
        epoch, change = self.epoch_for(reference, axis)
        if change is None:
            return epoch, None
        with self._db:
            if epoch.id is None:
                row = self._db.execute(
                    "INSERT INTO epochs (since, reference, top_x, top_y, bottom_x, bottom_y, "
                    "scale, shift, change, current) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                    (
                        now.isoformat(),
                        reference,
                        *epoch.top,
                        *epoch.bottom,
                        epoch.scale,
                        epoch.shift,
                        str(epoch.change),
                    ),
                )
                epoch = replace(epoch, id=row.lastrowid)
                if change is Change.FIRST:
                    self._db.execute(
                        "UPDATE readings SET epoch = ? WHERE epoch IS NULL", (epoch.id,)
                    )
            self._db.execute("UPDATE epochs SET current = (id = ?)", (epoch.id,))
        return epoch, change

    def current_epoch(self) -> AxisEpoch | None:
        if not self._has_epochs:
            return None
        row = self._db.execute(f"SELECT {_EPOCH_COLUMNS} FROM epochs WHERE current").fetchone()
        return None if row is None else _epoch(row)

    def epochs(self) -> dict[int, AxisEpoch]:
        if not self._has_epochs:
            return {}
        rows = self._db.execute(f"SELECT {_EPOCH_COLUMNS} FROM epochs")
        return {row[0]: _epoch(row) for row in rows}

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

    def fills(self, since: datetime | None = None, until: datetime | None = None) -> list[Fill]:
        query, args = "SELECT ts, before, after FROM fills WHERE 1", []
        if since is not None:
            query += " AND ts >= ?"
            args.append(since.isoformat())
        if until is not None:
            query += " AND ts < ?"
            args.append(until.isoformat())
        rows = self._db.execute(query + " ORDER BY ts", args)
        return [Fill(datetime.fromisoformat(ts), b, a) for ts, b, a in rows]

    def count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM readings").fetchone()[0])


def _epoch(row: tuple[int, str, float, float, float, float, float, float, str]) -> AxisEpoch:
    id_, reference, top_x, top_y, bottom_x, bottom_y, scale, shift, change = row
    return AxisEpoch(
        reference, (top_x, top_y), (bottom_x, bottom_y), scale, shift, Change(change), id_
    )


def _check_version(db: sqlite3.Connection, path: Path) -> tuple[sqlite3.Connection, int]:
    """``db`` and its schema version, if this version can read it (closed otherwise)."""
    try:
        version = int(db.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.Error:
        db.close()
        raise
    if not 1 <= version <= SCHEMA_VERSION:
        db.close()
        raise RuntimeError(f"{path} is from a newer oiltank (schema {version})")
    return db, version


def _connect(uri: str) -> sqlite3.Connection:
    # A web request may open it on one worker thread and use it on another (never at once).
    return sqlite3.connect(uri, uri=True, check_same_thread=False)
