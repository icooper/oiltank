from datetime import UTC, datetime, timedelta

import pytest

from oiltank.filter import Fill, FilterParams, LevelFilter, Observation, theil_sen_slope
from oiltank.model import Problem

T0 = datetime(2026, 11, 1, tzinfo=UTC)
CAPACITY = 250.0


def good(hours: float, fraction: float, confidence: float = 0.95) -> Observation:
    return Observation(T0 + timedelta(hours=hours), fraction, confidence, t=fraction, problem=None)


def bad(hours: float, problem: Problem | None = Problem.CAMERA_UNREACHABLE) -> Observation:
    return Observation(T0 + timedelta(hours=hours), None, 0.0, t=None, problem=problem)


def declining(hours: int, start: float, per_day: float) -> list[Observation]:
    """Hourly readings falling by ``per_day`` gallons a day."""
    return [good(h, start - per_day * h / 24 / CAPACITY) for h in range(hours)]


def test_theil_sen_ignores_outliers() -> None:
    x = list(range(20))
    y = [10 - 0.5 * v for v in x]
    y[3], y[11] = 100.0, -50.0
    assert theil_sen_slope(x, y) == pytest.approx(-0.5)


def test_usage_rate_and_days_left() -> None:
    f = LevelFilter(FilterParams(), CAPACITY, reserve=25)
    update = None
    for obs in declining(72, start=0.8, per_day=2.0):
        update = f.update(obs)
    assert update is not None
    state = update.state
    assert state.usage_rate == pytest.approx(2.0, rel=1e-6)
    assert state.volume == pytest.approx(0.8 * CAPACITY - 2.0 * 71 / 24, abs=0.2)
    assert state.volume is not None
    assert state.days_left == pytest.approx((state.volume - 25) / 2.0)
    assert state.problem is None


def test_usage_needs_enough_points_and_hours() -> None:
    f = LevelFilter(FilterParams(), CAPACITY)
    for obs in declining(20, start=0.8, per_day=2.0):  # 20 points but only 19 hours
        state = f.update(obs).state
    assert state.usage_rate is None
    assert state.days_left is None
    sparse = LevelFilter(FilterParams(), CAPACITY)
    for h in range(0, 48, 6):  # 48 hours but only 8 points
        state = sparse.update(good(h, 0.8 - h / 1000)).state
    assert state.usage_rate is None


def test_flat_usage_means_days_left_unknown() -> None:
    f = LevelFilter(FilterParams(), CAPACITY)
    for h in range(48):
        state = f.update(good(h, 0.6)).state
    assert state.usage_rate == pytest.approx(0.0)
    assert state.days_left is None


def test_one_bad_read_does_not_move_the_level() -> None:
    f = LevelFilter(FilterParams(), CAPACITY)
    for h, frac in enumerate([0.70, 0.70, 0.40, 0.70]):  # a misread at 40%
        state = f.update(good(h, frac)).state
        assert state.fraction == pytest.approx(0.70)


def test_refill_needs_confirmation() -> None:
    f = LevelFilter(FilterParams(), CAPACITY)
    for obs in declining(72, start=0.35, per_day=2.0):
        f.update(obs)
    before = f.level
    assert before is not None

    first = f.update(good(73, 0.95))
    assert first.accepted and first.fill is None
    assert first.state.fraction == pytest.approx(before)  # held back

    second = f.update(good(74, 0.96))
    assert second.fill == Fill(ts=T0 + timedelta(hours=73), before=before, after=0.955)
    state = second.state
    assert state.fraction == pytest.approx(0.955)
    assert state.last_fill == T0 + timedelta(hours=73)
    assert state.last_fill_volume == pytest.approx((0.955 - before) * CAPACITY)
    assert state.usage_rate is None  # restarts after a refill


def test_unconfirmed_jump_is_an_outlier() -> None:
    f = LevelFilter(FilterParams(), CAPACITY)
    for h in range(5):
        f.update(good(h, 0.40))
    f.update(good(5, 0.95))
    update = f.update(good(6, 0.40))
    assert update.fill is None
    assert update.state.fraction == pytest.approx(0.40)
    assert update.state.last_fill is None


def test_problem_after_repeated_failures() -> None:
    f = LevelFilter(FilterParams(failures_for_problem=3), CAPACITY)
    f.update(good(0, 0.5))
    for h in (1, 2):
        update = f.update(bad(h))
        assert not update.accepted
        assert update.state.problem is None
        assert update.state.fraction == pytest.approx(0.5)  # last good level stays
    update = f.update(bad(3, Problem.CAMERA_MOVED))
    assert update.state.problem is Problem.CAMERA_MOVED
    assert update.state.fraction == pytest.approx(0.5)
    assert update.state.confidence == 0.0
    recovered = f.update(good(4, 0.49)).state
    assert recovered.problem is None


def test_low_confidence_is_a_failure() -> None:
    f = LevelFilter(FilterParams(min_confidence=0.5, failures_for_problem=1), CAPACITY)
    update = f.update(good(0, 0.5, confidence=0.3))
    assert not update.accepted
    assert update.state.problem is Problem.LOW_CONFIDENCE
    assert update.state.fraction is None


def test_old_readings_leave_the_usage_window() -> None:
    f = LevelFilter(FilterParams(usage_window_days=2), CAPACITY)
    # A week of steep use, then two days of slower use: only the recent rate counts.
    for obs in declining(24 * 7, start=0.9, per_day=6.0):
        f.update(obs)
    start = f.level
    assert start is not None
    for h in range(24 * 7, 24 * 9 + 1):
        state = f.update(good(h, start - 1.0 * (h - 24 * 7) / 24 / CAPACITY)).state
    assert state.usage_rate == pytest.approx(1.0, rel=0.02)


def test_replay_reproduces_the_state() -> None:
    observations = [*declining(40, 0.5, 3.0), bad(40), good(41, 0.95), good(42, 0.95)]
    live = LevelFilter(FilterParams(), CAPACITY)
    for obs in observations:
        last = live.update(obs).state
    replayed = LevelFilter(FilterParams(), CAPACITY)
    replayed.replay(observations)
    assert replayed.update(good(43, 0.95)).state.fraction == pytest.approx(0.95)
    assert last.last_fill == T0 + timedelta(hours=41)


def test_recalibrated_observation() -> None:
    obs = good(0, 0.5)
    assert obs.recalibrated(lambda t: t * 1.1).fraction == pytest.approx(0.55)
    assert bad(0).recalibrated(lambda t: 1.0).fraction is None
