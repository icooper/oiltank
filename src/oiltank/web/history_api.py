"""The History view's API: readings over a time range, the archive's records, and the gauge over
time.

Levels are always worked out with the current calibration, from each reading's ``t`` carried onto
the current axis (see :mod:`oiltank.epochs`). Archive records are matched to stored readings by
their timestamp, which is how a record's strip finds its epoch.
"""

import math
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse

from oiltank.archive import DAY_RE, ArchiveReader, ArchiveRecord
from oiltank.config import Settings
from oiltank.epochs import carry
from oiltank.filter import FilterParams
from oiltank.geometry import STRIP_ROWS
from oiltank.history import History
from oiltank.readers.classical import ClassicalReader
from oiltank.web.control import Control, Current
from oiltank.web.kymograph import Carry, DayTiles, Grid
from oiltank.web.responses import IMMUTABLE, json_response
from oiltank.web.timeline import build_series, scale

DEFAULT_RANGE = timedelta(days=7)
MAX_RANGE = timedelta(days=731)
NEAREST_FRAME = timedelta(days=2)
"""How far from a reading the inspector looks for a full frame when its own record has none."""
NO_CARRY: Carry = (1.0, 0.0)


def _pct(fraction: float | None) -> float | None:
    return None if fraction is None or math.isnan(fraction) else round(100 * fraction, 2)


def _round(value: float | None, digits: int = 5) -> float | None:
    return None if value is None else round(value, digits)


def _utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts


def grid_of(current: Current) -> Grid:
    margin = current.calibration.axis.margin
    return Grid(1.0 + margin, -margin)


def frame_view(record: ArchiveRecord, seen_at: datetime) -> dict[str, Any] | None:
    """Where to look in a record's full frame: a crop around the gauge, and the axis and disc
    in frame pixels (the reference positions moved by that frame's alignment)."""
    meta = record.meta
    cal = meta.calibration
    if record.frame_path is None or cal is None or meta.frame is None:
        return None
    last = meta.frames[-1].alignment if meta.frames else None
    dx, dy = (last.dx, last.dy) if last is not None else (0.0, 0.0)
    axis = cal.axis
    top = (axis.top[0] + dx, axis.top[1] + dy)
    bottom = (axis.bottom[0] + dx, axis.bottom[1] + dy)
    reach = axis.length * (1 + 2 * axis.margin)
    pad = max(0.6 * reach, 2 * axis.half_width)
    x0 = min(top[0], bottom[0]) - axis.half_width - pad
    x1 = max(top[0], bottom[0]) + axis.half_width + pad
    y0 = min(top[1], bottom[1]) - axis.margin * axis.length - pad
    y1 = max(top[1], bottom[1]) + axis.margin * axis.length + pad
    w, h = cal.frame_size
    x0, y0 = max(0, int(x0)), max(0, int(y0))
    x1, y1 = min(w, math.ceil(x1)), min(h, math.ceil(y1))
    disc = None if meta.position is None else [meta.position[0] + dx, meta.position[1] + dy]
    return {
        "url": f"archive/{record.day}/{meta.frame}",
        "day": record.day,
        "key": record.key,
        "age_s": round((meta.ts - seen_at).total_seconds(), 1),
        "frame_size": list(cal.frame_size),
        "crop": [x0, y0, x1 - x0, y1 - y0],
        "axis": {"top": list(top), "bottom": list(bottom), "half_width": axis.half_width},
        "disc": disc,
        "disc_size": [cal.disc.width, cal.disc.height],
    }


def record_detail(
    record: ArchiveRecord, current: Current, carried: Carry, full: ArchiveRecord | None
) -> dict[str, Any]:
    """What the inspector shows of a record. Strip rows map linearly to ``t`` (carried onto the
    current axis) from ``t_first`` at the top row to ``t_last`` at the bottom."""
    meta = record.meta
    a, b = carried

    def on_axis(t: float | None) -> float | None:
        return None if t is None else a * t + b

    t = on_axis(meta.t)
    first, last = on_axis(meta.strip_t_first_row), on_axis(meta.strip_t_last_row)

    def row(t_cur: float | None) -> float | None:
        if t_cur is None or first is None or last is None or first == last:
            return None
        return round((t_cur - first) / (last - first) * (STRIP_ROWS - 1), 2)

    level = None
    if meta.fraction is not None:
        level = _pct(meta.fraction if t is None else current.calibration.fraction(t))
    return {
        "day": record.day,
        "key": record.key,
        "ts": meta.ts.isoformat(),
        "level": level,
        "t": _round(t),
        "confidence": meta.confidence,
        "problem": meta.problem,
        "notices": list(meta.notices),
        "reader": meta.reader,
        "strip": None if record.strip_path is None else f"archive/{record.day}/{meta.strip}",
        "rows": STRIP_ROWS,
        "t_first": _round(first),
        "t_last": _round(last),
        "disc_row": meta.strip_row,
        "strip_frame": meta.strip_frame,
        "frames": [
            {
                "t": _round(on_axis(f.t)),
                "row": f.row,
                "confidence": f.confidence,
                "alignment": None if f.alignment is None else f.alignment.model_dump(),
            }
            for f in meta.frames
        ],
        "ticks": [
            {"label": tick["label"], "row": row(tick["t"])}
            for tick in scale(current.calibration)["ticks"]
        ],
        "frame": None if full is None else frame_view(full, meta.ts),
    }


def history_router(
    settings: Settings, control: Control, archive: ArchiveReader, history_path: Path
) -> APIRouter:
    router = APIRouter()
    tiles = DayTiles(archive)

    def current() -> Current:
        cur = control.current()
        if cur is None:
            raise HTTPException(409, "there's no usable calibration to work out levels with")
        return cur

    @contextmanager
    def history() -> Iterator[History | None]:
        """The history, read-only; None if there isn't one yet."""
        try:
            h = History.open_readonly(history_path)
        except FileNotFoundError:
            yield None
            return
        except (OSError, RuntimeError, sqlite3.Error) as e:
            raise HTTPException(503, f"can't read the history: {e}") from e
        try:
            yield h
        finally:
            h.close()

    def day_range(day: str) -> tuple[datetime, datetime]:
        if not DAY_RE.fullmatch(day):
            raise HTTPException(404, "not a day")
        try:
            start = datetime.combine(date.fromisoformat(day), datetime.min.time(), UTC)
        except ValueError as e:
            raise HTTPException(404, "not a day") from e
        return start, start + timedelta(days=1)

    def carries(day: str, cur: Current) -> Callable[[ArchiveRecord], Carry]:
        """How each of a day's records is carried onto the current axis, by its reading's epoch."""
        start, end = day_range(day)
        with history() as h:
            if h is None:
                return lambda record: NO_CARRY
            epoch_of = {r.ts: r.epoch for r in h.readings(start, end)}
            epochs = h.epochs()

        def carry_of(record: ArchiveRecord) -> Carry:
            epoch_id = epoch_of.get(record.meta.ts)
            src = None if epoch_id is None else epochs.get(epoch_id)
            return NO_CARRY if src is None or src.id == cur.epoch.id else carry(src, cur.epoch)

        return carry_of

    def found(day: str, key: str) -> ArchiveRecord:
        record = archive.record(day, key)
        if record is None:
            raise HTTPException(404, "no such record")
        return record

    @router.get("/api/readings")
    def readings(
        request: Request, start: datetime | None = None, end: datetime | None = None
    ) -> Response:
        """Readings from ``start`` until before ``end`` (default: the last week)."""
        end = _utc(end) if end is not None else datetime.now(UTC)
        start = _utc(start) if start is not None else end - DEFAULT_RANGE
        if not start < end:
            raise HTTPException(422, "start must be before end")
        if end - start > MAX_RANGE:
            raise HTTPException(422, f"at most {MAX_RANGE.days} days at a time")
        cur = current()
        params = FilterParams.from_settings(settings.service)
        tank = settings.tank
        warm_up = start - timedelta(days=params.usage_window_days + 1)
        with history() as h:
            rows = [] if h is None else h.readings(warm_up, end, onto=cur.epoch)
            last_fill = None if h is None else h.last_fill(warm_up)
        series = build_series(
            rows, start, cur.calibration, params, tank.capacity, tank.reserve, last_fill
        )
        return json_response(
            request,
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "unit": tank.unit,
                "capacity": tank.capacity,
                "scale": scale(cur.calibration),
                **series,
            },
        )

    @router.get("/api/archive/days/{day}")
    def archive_day(request: Request, day: str) -> Response:
        """A day's records, in order: the columns of its gauge-over-time tile."""
        day_range(day)
        cur = current()
        records = archive.records(day)
        carry_of = carries(day, cur)
        carried = [carry_of(r) for r in records]
        t = np.array(
            [
                np.nan if r.meta.t is None else a * r.meta.t + b
                for r, (a, b) in zip(records, carried, strict=True)
            ],
            dtype=np.float64,
        )
        levels = cur.calibration.fractions(t) if len(t) else t
        items = [
            {
                "key": r.key,
                "ts": round(r.meta.ts.timestamp(), 3),
                "t": None if math.isnan(t_i) else round(float(t_i), 5),
                "level": None if r.meta.fraction is None else _pct(float(level)),
                "confidence": r.meta.confidence,
                "problem": r.meta.problem,
                "strip": r.strip_path is not None,
                "frame": r.frame_path is not None,
            }
            for r, t_i, level in zip(records, t, levels, strict=True)
        ]
        version = DayTiles.version(records, carried, grid_of(cur))
        return json_response(
            request,
            {
                "day": day,
                "records": items,
                "tile": f"api/kymograph/{day}.png?v={version}" if records else None,
            },
        )

    @router.get("/api/kymograph/{day}.png", include_in_schema=False)
    def kymograph(day: str, v: str | None = None) -> Response:
        day_range(day)
        if not archive.records(day):
            raise HTTPException(404, "no records that day")
        cur = current()
        version, png = tiles.png(day, carries(day, cur), grid_of(cur))
        cache = IMMUTABLE if v == version else "no-cache"
        return Response(
            png, media_type="image/png", headers={"Cache-Control": cache, "ETag": f'"{version}"'}
        )

    @router.get("/api/archive/records/{day}/{key}")
    def record(request: Request, day: str, key: str) -> Response:
        rec = found(day, key)
        cur = current()
        full = (
            rec
            if rec.frame_path is not None
            else archive.nearest_full_frame(rec.meta.ts, NEAREST_FRAME)
        )
        return json_response(request, record_detail(rec, cur, carries(day, cur)(rec), full))

    @router.get("/api/archive/records/{day}/{key}/scores")
    def scores(request: Request, day: str, key: str) -> Response:
        """The reader's score for each strip row, worked out again (the archive doesn't keep
        them): how disc-like the strip looks centred on that row."""
        rec = found(day, key)
        strip = rec.strip()
        if strip is None or rec.meta.calibration is None:
            raise HTTPException(404, "that record has no strip")
        reading = ClassicalReader(rec.meta.calibration.disc).read(strip)
        values = [None if math.isnan(x) else round(float(x), 4) for x in reading.scores]
        return json_response(
            request,
            {"scores": values, "row": reading.row, "confidence": round(reading.confidence, 4)},
        )

    @router.get("/archive/{day}/{name}", include_in_schema=False)
    def archive_file(day: str, name: str) -> FileResponse:
        path = archive.path(day, name)
        if path is None:
            raise HTTPException(404, "no such file")
        return FileResponse(path, headers={"Cache-Control": IMMUTABLE})

    return router
