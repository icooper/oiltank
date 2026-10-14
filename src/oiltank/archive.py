"""Keep what each reading saw: for checking the reader later, and for training a model on it.

A record is ``<root>/<UTC date>/<UTC time>Z`` plus:

- ``.json``: the reading, the per-frame alignment and readings, and how the strip's rows map to
  gauge positions (``t`` of the first and last row). With the disc's row, that labels the strip.
- ``-strip.png``: the rectified gauge strip that produced the reading (lossless).
- ``-frame.jpg``: the full camera frame exactly as received, when this record keeps one.

The rolling archive keeps a full frame every ``full_frame_every`` and for every problem reading.
Beyond ``max_bytes`` it deletes whole records, oldest first, down to ``EVICT_TO`` of the limit; a
record's JSON goes first and is written last (atomically), so a record whose JSON exists has all
its files. A delivery capture uses the same format with every frame kept and no limit.

:class:`ArchiveReader` reads records back, for the web UI. It tolerates records from older
versions (missing fields) and records whose strip or frame is missing.
"""

import itertools
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from pydantic import BaseModel, ConfigDict, ValidationError

from oiltank.calibration import Axis, Calibration, Disc
from oiltank.config import ArchiveSettings
from oiltank.geometry import strip_for
from oiltank.model import Alignment, Frame, FrameResult, GaugeStrip, Measurement, Point

log = logging.getLogger(__name__)

EVICT_TO = 0.95
"""Once over the size limit, delete down to this fraction of it, so that the whole archive isn't
scanned again after every new record."""
_FILE_RE = re.compile(r"(?P<time>\d{6}Z)(?:-(?P<n>\d+))?(?P<part>\.json|-strip\.png|-frame\.jpg)")
DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
KEY_RE = re.compile(r"\d{6}Z(?:-\d+)?")


@dataclass(frozen=True, slots=True)
class ArchivePolicy:
    max_bytes: int | None = 2000 * 1024 * 1024
    """None: no limit (delivery captures)."""
    full_frame_every: timedelta | None = timedelta(hours=6)
    """None: only when ``problem_frames`` asks for one, or ``record(full_frame=True)``."""
    problem_frames: bool = True

    @classmethod
    def from_settings(cls, archive: ArchiveSettings) -> ArchivePolicy:
        return cls(
            max_bytes=int(archive.max_mb * 1024 * 1024),
            full_frame_every=(
                timedelta(hours=archive.full_frame_hours) if archive.full_frame_hours > 0 else None
            ),
            problem_frames=archive.problem_frames,
        )

    @classmethod
    def capture(cls) -> ArchivePolicy:
        """Every full frame, no limit: for recording a delivery."""
        return cls(max_bytes=None, full_frame_every=timedelta(0), problem_frames=True)


def _best_result(m: Measurement) -> FrameResult | None:
    """The frame whose reading is closest to the burst's result (any with a strip if it failed)."""
    with_strip = [r for r in m.frames if r.strip is not None]
    target = m.t
    if target is None or not with_strip:
        return with_strip[0] if with_strip else None

    def distance(r: FrameResult) -> float:
        t = None if r.reading is None else r.reading.t
        return float("inf") if t is None else abs(t - target)

    return min(with_strip, key=distance)


class Archive:
    def __init__(self, root: Path, policy: ArchivePolicy) -> None:
        self.root = root
        self._policy = policy
        self._last_periodic: datetime | None = None  # problem frames don't move the schedule
        self._size: int | None = None  # bytes under root, scanned lazily

    def record(
        self,
        m: Measurement | None,
        frames: Sequence[Frame],
        calibration: Calibration,
        *,
        full_frame: bool = False,
    ) -> Path | None:
        """Write a record for one reading; returns its JSON path (None if there was nothing).

        ``full_frame`` forces keeping the full frame. Raises OSError if the disk refuses.
        """
        if m is None and not frames:
            return None
        ts = (frames[-1].ts if frames else m.ts if m else datetime.now(UTC)).astimezone(UTC)
        stem = self._stem(ts)
        stem.parent.mkdir(parents=True, exist_ok=True)
        meta: dict[str, Any] = {"ts": ts.isoformat(), "strip": None, "frame": None}

        if m is not None:
            meta |= {
                "reader": m.reader,
                "t": m.t,
                "fraction": m.fraction,
                "confidence": round(m.confidence, 4),
                "problem": None if m.problem is None else str(m.problem),
                "position": None if m.position is None else [round(v, 2) for v in m.position],
                "notices": [str(n) for n in m.notices],
                "frames": [
                    {
                        "source": r.frame.source,
                        "ts": r.frame.ts.isoformat(),
                        "alignment": {
                            "dx": round(r.alignment.dx, 3),
                            "dy": round(r.alignment.dy, 3),
                            "score": round(r.alignment.score, 4),
                            "ok": r.alignment.ok,
                        },
                        "t": None if r.reading is None else r.reading.t,
                        "row": None if r.reading is None else r.reading.row,
                        "confidence": None if r.reading is None else round(r.reading.confidence, 4),
                    }
                    for r in m.frames
                ],
            }
            best = _best_result(m)
            if best is not None and best.strip is not None:
                path = stem.with_name(stem.name + "-strip.png")
                ok, buf = cv2.imencode(".png", best.strip.pixels)
                if ok:
                    self._write(path, buf.tobytes())
                    strip = best.strip
                    meta["strip"] = path.name
                    meta["strip_frame"] = next(i for i, r in enumerate(m.frames) if r is best)
                    meta["strip_row"] = None if best.reading is None else best.reading.row
                    meta["strip_t_first_row"] = float(strip.t_of_row[0])
                    meta["strip_t_last_row"] = float(strip.t_of_row[-1])

        meta["calibration"] = {
            "reference_image": calibration.reference_image.as_posix(),
            "frame_size": list(calibration.frame_size),
            "axis": calibration.axis.model_dump(mode="json"),
            "disc": calibration.disc.model_dump(mode="json"),
        }
        if frames and self._keep_full(ts, m, forced=full_frame):
            data = frames[-1].data
            if data is not None:
                path = stem.with_name(stem.name + "-frame.jpg")
                self._write(path, data)
                meta["frame"] = path.name
        json_path = stem.with_name(stem.name + ".json")
        tmp = json_path.with_name(f".{json_path.name}.tmp")
        self._write(tmp, json.dumps(meta, indent=1).encode())
        tmp.replace(json_path)  # last, and all at once: the JSON says the record is complete
        self._enforce_limit()
        return json_path

    def _stem(self, ts: datetime) -> Path:
        day = self.root / ts.strftime("%Y-%m-%d")
        base = ts.strftime("%H%M%SZ")
        stem, n = day / base, 2
        while stem.with_name(stem.name + ".json").exists():
            stem, n = day / f"{base}-{n}", n + 1
        return stem

    def _keep_full(self, ts: datetime, m: Measurement | None, *, forced: bool) -> bool:
        p = self._policy
        if forced:
            return True
        last = self._last_periodic
        if p.full_frame_every is not None and (last is None or ts - last >= p.full_frame_every):
            self._last_periodic = ts
            return True
        return p.problem_frames and (m is None or m.problem is not None)

    def _write(self, path: Path, data: bytes) -> None:
        path.write_bytes(data)
        if self._size is not None:
            self._size += len(data)

    def size(self) -> int:
        """Bytes used by the archive."""
        if self._size is None:
            self._size = sum(f.stat().st_size for f in self.root.rglob("*") if f.is_file())
        return self._size

    def _enforce_limit(self) -> None:
        limit = self._policy.max_bytes
        if limit is None or self.size() <= limit:
            return
        files = sorted((f for f in self.root.rglob("*") if f.is_file()), key=_record_key)
        for _, record in itertools.groupby(files, key=_record_key):
            if self.size() <= limit * EVICT_TO:
                break
            for f in sorted(record, key=lambda f: f.suffix != ".json"):  # the JSON first
                size = f.stat().st_size
                f.unlink()
                self._size = self.size() - size
        for day in sorted(self.root.iterdir()):
            if day.is_dir() and not any(day.iterdir()):
                day.rmdir()
        log.info("archive trimmed to %.0f MB", self.size() / 1e6)


def _record_key(path: Path) -> tuple[str, str, int]:
    """``(day, time, n)``: the same for all of a record's files, in the order records were made.
    Any other file (a temporary file left by a crash) is a record of its own."""
    m = _FILE_RE.fullmatch(path.name)
    if m is None:
        return path.parent.name, path.name, 0
    return path.parent.name, m["time"], int(m["n"] or 1)


# ---- reading records back -----------------------------------------------------------------------


class _Meta(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class AlignmentMeta(_Meta):
    dx: float
    dy: float
    score: float
    ok: bool


class FrameMeta(_Meta):
    """One frame of the burst."""

    source: str = ""
    ts: datetime | None = None
    alignment: AlignmentMeta | None = None
    t: float | None = None
    row: float | None = None
    """The disc's row in that frame's strip."""
    confidence: float | None = None


class CalibrationSnapshot(_Meta):
    """The calibration's geometry when the record was made (not its scale points)."""

    reference_image: str | None = None
    frame_size: tuple[int, int]
    axis: Axis
    disc: Disc


class RecordMeta(_Meta):
    """A record's JSON. Fields are optional: a failed reading has few, and older versions wrote
    fewer."""

    ts: datetime
    reader: str | None = None
    t: float | None = None
    fraction: float | None = None
    confidence: float | None = None
    problem: str | None = None
    position: Point | None = None
    """The disc's centre in reference-frame pixels."""
    notices: tuple[str, ...] = ()
    frames: tuple[FrameMeta, ...] = ()
    strip: str | None = None
    strip_frame: int | None = None
    """Which of ``frames`` the strip is from (the full frame is always the last)."""
    strip_row: float | None = None
    strip_t_first_row: float | None = None
    strip_t_last_row: float | None = None
    frame: str | None = None
    calibration: CalibrationSnapshot | None = None


@dataclass(frozen=True, slots=True)
class ArchiveRecord:
    day: str
    key: str
    """``HHMMSSZ``, or ``HHMMSSZ-n`` for a later record in the same second."""
    meta: RecordMeta
    strip_path: Path | None
    """None if the record has no strip (any more)."""
    frame_path: Path | None

    def strip(self) -> GaugeStrip | None:
        """The strip, with the geometry it was rectified with (None if it can't be read)."""
        cal = self.meta.calibration
        if self.strip_path is None or cal is None:
            return None
        pixels = cv2.imread(str(self.strip_path), cv2.IMREAD_GRAYSCALE)
        if pixels is None:
            return None
        frame = (
            self.meta.frames[self.meta.strip_frame]
            if self.meta.strip_frame is not None and self.meta.strip_frame < len(self.meta.frames)
            else None
        )
        a = frame.alignment if frame is not None else None
        alignment = None if a is None else Alignment(a.dx, a.dy, a.score, a.ok)
        return strip_for(np.asarray(pixels, dtype=np.uint8), cal.axis, alignment)


class ArchiveReader:
    """Reads the records under an archive root (``data/archive``, or a capture's directory)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._days: dict[str, tuple[int, list[ArchiveRecord]]] = {}

    def days(self) -> list[str]:
        """The UTC dates with records, oldest first."""
        try:
            return sorted(
                d.name for d in self.root.iterdir() if d.is_dir() and DAY_RE.fullmatch(d.name)
            )
        except OSError:
            return []

    def records(self, day: str) -> list[ArchiveRecord]:
        """A day's records, in the order they were made (cached until the directory changes)."""
        if not DAY_RE.fullmatch(day):
            return []
        folder = self.root / day
        try:
            mtime = folder.stat().st_mtime_ns
        except OSError:
            return []
        cached = self._days.get(day)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        records = self._read_day(folder)
        self._days[day] = (mtime, records)
        return records

    def record(self, day: str, key: str) -> ArchiveRecord | None:
        if not KEY_RE.fullmatch(key):
            return None
        return next((r for r in self.records(day) if r.key == key), None)

    def path(self, day: str, name: str) -> Path | None:
        """A record's file, if ``day`` and ``name`` are an archive day and record file that exist.

        Anything else (``..``, other directories, other files) is None.
        """
        if not DAY_RE.fullmatch(day) or not _FILE_RE.fullmatch(name):
            return None
        path = self.root / day / name
        try:
            if not path.resolve().is_relative_to(self.root.resolve()) or not path.is_file():
                return None
        except OSError:
            return None
        return path

    def nearest_full_frame(self, ts: datetime, within: timedelta) -> ArchiveRecord | None:
        """The record with a full frame closest in time to ``ts``, no further than ``within``."""
        utc = ts.astimezone(UTC)
        days = {
            (utc + timedelta(days=k)).strftime("%Y-%m-%d")
            for k in range(-(within.days + 1), within.days + 2)
        }
        candidates = [
            r
            for day in sorted(days)
            for r in self.records(day)
            if r.frame_path is not None and abs(r.meta.ts - ts) <= within
        ]
        return min(candidates, key=lambda r: abs(r.meta.ts - ts), default=None)

    def _read_day(self, folder: Path) -> list[ArchiveRecord]:
        try:
            names = {f.name for f in folder.iterdir()}
        except OSError:
            return []
        records = []
        for name in names:
            m = _FILE_RE.fullmatch(name)
            if m is None or m["part"] != ".json":
                continue
            key = name.removesuffix(".json")
            try:
                meta = RecordMeta.model_validate_json((folder / name).read_bytes())
            except (OSError, ValidationError) as e:
                log.debug("skipping archive record %s/%s: %s", folder.name, name, e)
                continue
            records.append(
                ArchiveRecord(
                    day=folder.name,
                    key=key,
                    meta=meta,
                    strip_path=folder / meta.strip if meta.strip in names else None,
                    frame_path=folder / meta.frame if meta.frame in names else None,
                )
            )
        return sorted(records, key=lambda r: _record_key(folder / f"{r.key}.json"))
