"""The gauge over time: one column per archived reading, each the middle of that reading's strip,
lined up on the current axis. The disc reads as a band moving slowly down the image, and a refill
as a step up.

Each strip's rows are resampled onto one grid of ``t`` (the current axis, with its margins), using
the record's own row-to-``t`` mapping carried onto the current axis. So the band stays put across
axis edits and rebases. Each column is stretched to its own contrast, which evens out daylight and
IR. The core is pure; :class:`DayTiles` reads the strips and keeps a PNG per day.
"""

import hashlib
import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import cv2
import numpy as np
from numpy.typing import NDArray

from oiltank.archive import ArchiveReader, ArchiveRecord
from oiltank.geometry import STRIP_COLS, STRIP_ROWS
from oiltank.model import GrayImage

type Column = NDArray[np.float64]
"""Brightness per grid row; NaN where the strip didn't reach."""
type Carry = tuple[float, float]
"""``(a, b)``: a record's ``t`` is ``a * t + b`` on the current axis."""

MIN_SPAN = 8.0
"""The smallest brightness range a column is stretched over, so a flat one isn't all noise."""


@dataclass(frozen=True, slots=True)
class Grid:
    """The rows of a tile, top first: ``t`` from ``t_top`` down to ``t_bottom``."""

    t_top: float
    t_bottom: float
    rows: int = STRIP_ROWS

    def t(self) -> NDArray[np.float64]:
        """Each row's centre."""
        step = (self.t_top - self.t_bottom) / self.rows
        return self.t_top - (np.arange(self.rows, dtype=np.float64) + 0.5) * step


def band(disc_width: float, half_width: float, cols: int = STRIP_COLS) -> int:
    """Half the width, in strip columns, of the middle band a column is averaged over: a quarter
    of the disc's width either side of the axis."""
    px_per_col = 2.0 * half_width / cols
    return int(np.clip(round(0.25 * disc_width / px_per_col), 2, cols // 2))


def profile(pixels: GrayImage, half_band: int) -> Column:
    """The mean of the strip's middle ``2 * half_band`` columns, per strip row."""
    mid = pixels.shape[1] // 2
    middle = pixels[:, max(0, mid - half_band) : mid + half_band]
    return np.asarray(middle.mean(axis=1), dtype=np.float64)


def resample(values: Column, t_first: float, t_last: float, grid: Grid) -> Column:
    """``values`` (one per strip row, the first row at ``t_first``) on ``grid``'s rows."""
    t_rows = np.linspace(t_first, t_last, len(values))
    if t_first > t_last:  # rows run down the gauge; np.interp wants increasing x
        t_rows, values = t_rows[::-1], values[::-1]
    out = np.interp(grid.t(), t_rows, values, left=np.nan, right=np.nan)
    return np.asarray(out, dtype=np.float64)


def normalise(column: Column) -> tuple[NDArray[np.uint8], NDArray[np.uint8]]:
    """``(gray, alpha)``: the column stretched between its 2nd and 98th percentiles (opaque where
    there's data)."""
    finite = np.isfinite(column)
    gray = np.zeros(column.shape, np.uint8)
    if finite.sum() < 2:
        return gray, gray.copy()
    lo, hi = np.percentile(column[finite], [2, 98])
    scaled = (column - lo) / max(hi - lo, MIN_SPAN)
    gray[finite] = np.round(np.clip(scaled[finite], 0, 1) * 255).astype(np.uint8)
    return gray, np.where(finite, 255, 0).astype(np.uint8)


def tile(columns: Sequence[Column | None], rows: int = STRIP_ROWS) -> NDArray[np.uint8]:
    """A BGRA image, one column per entry (transparent where there's none)."""
    image = np.zeros((rows, max(1, len(columns)), 4), np.uint8)
    for i, column in enumerate(columns):
        if column is None:
            continue
        gray, alpha = normalise(column)
        image[:, i, :3] = gray[:, None]
        image[:, i, 3] = alpha
    return image


def record_column(record: ArchiveRecord, carry: Carry, grid: Grid) -> Column | None:
    """A record's column, or None if its strip or geometry is missing."""
    meta = record.meta
    cal = meta.calibration
    if (
        record.strip_path is None
        or cal is None
        or meta.strip_t_first_row is None
        or meta.strip_t_last_row is None
    ):
        return None
    pixels = cv2.imread(str(record.strip_path), cv2.IMREAD_GRAYSCALE)
    if pixels is None:
        return None
    a, b = carry
    values = profile(
        np.asarray(pixels, np.uint8), band(cal.disc.width, cal.axis.half_width, pixels.shape[1])
    )
    return resample(values, a * meta.strip_t_first_row + b, a * meta.strip_t_last_row + b, grid)


class DayTiles:
    """A PNG per UTC day, kept in memory (up to ``budget`` bytes, least recently used first).

    A tile depends on the day's records, how each is carried onto the current axis, and the
    grid; :meth:`version` names that combination, so a URL with it can be cached for good.
    """

    def __init__(self, archive: ArchiveReader, budget: int = 64 * 1024 * 1024) -> None:
        self._archive = archive
        self._budget = budget
        self._cache: OrderedDict[str, bytes] = OrderedDict()
        self._size = 0
        self._lock = threading.Lock()

    @staticmethod
    def version(records: Sequence[ArchiveRecord], carries: Sequence[Carry], grid: Grid) -> str:
        h = hashlib.sha256(repr((grid.t_top, grid.t_bottom, grid.rows)).encode())
        for r, (a, b) in zip(records, carries, strict=True):
            h.update(f"{r.day}/{r.key}:{r.strip_path is not None}:{a:.9g},{b:.9g};".encode())
        return h.hexdigest()[:16]

    def png(
        self, day: str, carry_of: Callable[[ArchiveRecord], Carry], grid: Grid
    ) -> tuple[str, bytes]:
        """``(version, PNG bytes)`` for a day's tile, one column per record in order."""
        records = self._archive.records(day)
        carries = [carry_of(r) for r in records]
        version = self.version(records, carries, grid)
        with self._lock:
            cached = self._cache.get(version)
            if cached is not None:
                self._cache.move_to_end(version)
                return version, cached
        columns = [record_column(r, c, grid) for r, c in zip(records, carries, strict=True)]
        ok, buf = cv2.imencode(".png", tile(columns, grid.rows))
        if not ok:
            raise RuntimeError(f"couldn't encode the {day} tile")
        data = buf.tobytes()
        with self._lock:
            if version not in self._cache:
                self._cache[version] = data
                self._size += len(data)
            while self._size > self._budget and len(self._cache) > 1:
                _, old = self._cache.popitem(last=False)
                self._size -= len(old)
        return version, data
