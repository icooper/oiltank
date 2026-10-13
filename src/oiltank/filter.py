"""Turn a stream of readings into the level that gets published. Pure: no I/O, no clock.

- **Gate.** A reading with a problem, or below ``min_confidence``, is rejected and the last good
  level stays published. After ``failures_for_problem`` rejections in a row, ``problem`` is set
  (with the latest reason) until a good reading arrives.
- **Smoothing.** The level is the median of the last ``smoothing`` good readings, so one bad read
  can't move it. Small rises are allowed: oil expands as it warms.
- **Refill.** A rise of more than ``refill_jump`` over the level is held back until the next good
  reading confirms it. Then it's a refill: the smoothing window restarts from the two high
  readings, and the fill's time and size are kept. An unconfirmed jump is dropped as an outlier.
- **Usage.** A robust (Theil-Sen) trend of volume over time, across the last
  ``usage_window_days`` since the latest refill. It's only reported with at least
  ``usage_min_points`` readings spanning ``usage_min_hours``. Days left divides the volume above
  the reserve by it.

The service rebuilds a filter on start (and after recalibrating) by replaying stored readings, so
everything here is a function of the readings alone.
"""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

import numpy as np

from oiltank.config import ServiceSettings
from oiltank.model import LevelState, Problem

MIN_USAGE_RATE = 0.05
"""Volume per day below which "days left" is meaningless (e.g. the heating is off)."""


@dataclass(frozen=True, slots=True)
class FilterParams:
    min_confidence: float = 0.5
    failures_for_problem: int = 3
    smoothing: int = 3
    refill_jump: float = 0.15
    usage_window_days: float = 7.0
    usage_min_hours: float = 24.0
    usage_min_points: int = 12

    @classmethod
    def from_settings(cls, service: ServiceSettings) -> FilterParams:
        return cls(
            min_confidence=service.min_confidence,
            failures_for_problem=service.failures_for_problem,
            smoothing=service.smoothing,
            refill_jump=service.refill_jump,
            usage_window_days=service.usage_window_days,
            usage_min_hours=service.usage_min_hours,
            usage_min_points=service.usage_min_points,
        )


@dataclass(frozen=True, slots=True)
class Observation:
    """One reading attempt, as stored and as the filter sees it."""

    ts: datetime
    fraction: float | None
    confidence: float
    t: float | None
    problem: Problem | None

    def recalibrated(self, fraction_of_t: Callable[[float], float]) -> Observation:
        """The same reading with its fraction recomputed from ``t`` (after a recalibration)."""
        if self.fraction is None or self.t is None:
            return self
        return replace(self, fraction=fraction_of_t(self.t))


@dataclass(frozen=True, slots=True)
class Fill:
    """A refill: when it started, and the level before and after (fractions)."""

    ts: datetime
    before: float
    after: float

    def volume(self, capacity: float) -> float:
        return (self.after - self.before) * capacity


@dataclass(frozen=True, slots=True)
class Update:
    state: LevelState
    accepted: bool
    """The reading passed the gate (it may still be held back as a possible refill)."""
    fill: Fill | None
    """A refill this reading confirmed."""


def theil_sen_slope(x: Sequence[float], y: Sequence[float]) -> float:
    """Median of the slopes between all pairs of points: robust to a few bad readings."""
    xs, ys = np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)
    i, j = np.triu_indices(len(xs), k=1)
    dx = xs[j] - xs[i]
    keep = dx != 0
    return float(np.median((ys[j] - ys[i])[keep] / dx[keep]))


class LevelFilter:
    def __init__(
        self,
        params: FilterParams,
        capacity: float,
        reserve: float = 0.0,
        last_fill: Fill | None = None,
    ) -> None:
        self._p = params
        self._capacity = capacity
        self._reserve = reserve
        self._last_fill = last_fill
        self._samples: list[tuple[datetime, float]] = []  # good readings since the last refill
        self._pending: tuple[datetime, float] | None = None  # a jump awaiting confirmation
        self._failures = 0
        self._problem: Problem | None = None
        self._confidence = 0.0
        self._t: float | None = None

    @property
    def level(self) -> float | None:
        """The smoothed level (fraction), or None before the first good reading."""
        if not self._samples:
            return None
        recent = [f for _, f in self._samples[-self._p.smoothing :]]
        return float(np.median(recent))

    def replay(self, observations: Iterable[Observation]) -> None:
        for obs in observations:
            self.update(obs)

    def update(self, obs: Observation) -> Update:
        p = self._p
        self._confidence = obs.confidence
        good = (
            obs.problem is None and obs.fraction is not None and obs.confidence >= p.min_confidence
        )
        if not good or obs.fraction is None:
            self._failures += 1
            if self._failures >= p.failures_for_problem:
                self._problem = obs.problem or Problem.LOW_CONFIDENCE
            return Update(self.state(obs.ts), accepted=False, fill=None)

        self._failures = 0
        self._problem = None
        self._t = obs.t
        sample = (obs.ts, obs.fraction)
        fill = None
        current = self.level
        if current is not None and obs.fraction - current > p.refill_jump:
            if self._pending is None:
                self._pending = sample  # wait for the next good reading
            else:
                after = float(np.median([self._pending[1], obs.fraction]))
                fill = Fill(ts=self._pending[0], before=current, after=after)
                self._last_fill = fill
                self._samples = [self._pending, sample]
                self._pending = None
        else:
            self._pending = None  # an unconfirmed jump was an outlier
            self._samples.append(sample)
            self._trim(obs.ts)
        return Update(self.state(obs.ts), accepted=True, fill=fill)

    def _trim(self, now: datetime) -> None:
        cutoff = now - timedelta(days=self._p.usage_window_days)
        keep = max(self._p.smoothing, 1)
        while len(self._samples) > keep and self._samples[0][0] < cutoff:
            self._samples.pop(0)

    def usage_rate(self) -> float | None:
        """Volume used per day (positive), or None until there's enough data."""
        p = self._p
        if len(self._samples) < p.usage_min_points:
            return None
        t0, _ = self._samples[0]
        t1, _ = self._samples[-1]
        if (t1 - t0) < timedelta(hours=p.usage_min_hours):
            return None
        days = [(ts - t0).total_seconds() / 86400 for ts, _ in self._samples]
        volumes = [f * self._capacity for _, f in self._samples]
        return max(0.0, -theil_sen_slope(days, volumes))

    def state(self, ts: datetime) -> LevelState:
        """What to publish now (``ts`` is the time of the latest reading attempt)."""
        level = self.level
        volume = None if level is None else level * self._capacity
        usage = self.usage_rate()
        days_left = None
        if volume is not None and usage is not None and usage >= MIN_USAGE_RATE:
            days_left = max(0.0, (volume - self._reserve) / usage)
        fill = self._last_fill
        return LevelState(
            ts=ts,
            fraction=level,
            volume=volume,
            confidence=self._confidence,
            t=self._t,
            problem=self._problem,
            usage_rate=usage,
            days_left=days_left,
            last_fill=None if fill is None else fill.ts,
            last_fill_volume=None if fill is None else fill.volume(self._capacity),
        )
