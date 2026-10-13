"""Keep what each reading saw: for checking the reader later, and for training a model on it.

A record is ``<root>/<UTC date>/<UTC time>Z`` plus:

- ``.json``: the reading, the per-frame alignment and readings, and how the strip's rows map to
  gauge positions (``t`` of the first and last row). With the disc's row, that labels the strip.
- ``-strip.png``: the rectified gauge strip that produced the reading (lossless).
- ``-frame.jpg``: the full camera frame exactly as received, when this record keeps one.

The rolling archive keeps a full frame every ``full_frame_every`` and for every problem reading,
and deletes the oldest files beyond ``max_bytes``. A delivery capture uses the same format with
every frame kept and no limit.
"""

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import cv2

from oiltank.calibration import Calibration
from oiltank.config import ArchiveSettings
from oiltank.model import Frame, FrameResult, Measurement

log = logging.getLogger(__name__)


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
        self._write(json_path, json.dumps(meta, indent=1).encode())
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
        files = sorted(f for f in self.root.rglob("*") if f.is_file())  # names sort by time
        for f in files:
            if self.size() <= limit:
                break
            size = f.stat().st_size
            f.unlink()
            self._size = self.size() - size
        for day in sorted(self.root.iterdir()):
            if day.is_dir() and not any(day.iterdir()):
                day.rmdir()
        log.info("archive trimmed to %.0f MB", self.size() / 1e6)
