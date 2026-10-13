"""Peak finding and confidence over a per-row score profile, shared by all readers.

Every reader turns a :class:`~oiltank.model.GaugeStrip` into one score per strip row ("how much
does the indicator look centred on this row?"). The classical reader gets it from template
matching; a learned model would output it as a 1-D heatmap. Interpreting the scores is common.
"""

from dataclasses import dataclass

import numpy as np

from oiltank.model import FloatArray


@dataclass(frozen=True, slots=True)
class Peak:
    row: float
    """Sub-row position of the best score."""
    score: float
    runner_up: float
    """Best score at least ``exclusion`` rows away from the peak (``-inf`` if none)."""

    @property
    def prominence(self) -> float:
        return self.score - self.runner_up


def find_peak(scores: FloatArray, exclusion: float) -> Peak | None:
    """Locate the best-scoring row. NaN scores (rows that couldn't be evaluated) are ignored."""
    valid = np.isfinite(scores)
    if not valid.any():
        return None
    filled = np.where(valid, scores, -np.inf)
    k = int(np.argmax(filled))
    best = float(filled[k])

    offset = 0.0
    if 0 < k < len(scores) - 1 and valid[k - 1] and valid[k + 1]:
        a, b, c = float(filled[k - 1]), best, float(filled[k + 1])
        d = a - 2.0 * b + c
        if d != 0.0:
            offset = float(np.clip(0.5 * (a - c) / d, -0.5, 0.5))

    far = np.abs(np.arange(len(scores)) - k) >= exclusion  # "at least", as documented
    runner_up = float(filled[far].max()) if far.any() else float("-inf")
    return Peak(row=k + offset, score=best, runner_up=runner_up)


def _ramp(x: float, lo: float, hi: float) -> float:
    return float(np.clip((x - lo) / (hi - lo), 0.0, 1.0))


def peak_confidence(
    peak: Peak, *, score_floor: float, score_good: float, prominence_good: float
) -> float:
    """Confidence in [0, 1]: the peak must be both strong and clearly better than any other.

    Scores at or below ``score_floor`` give 0, at or above ``score_good`` count fully; likewise the
    margin over the runner-up must reach ``prominence_good`` to count fully.
    """
    strength = _ramp(peak.score, score_floor, score_good)
    if not np.isfinite(peak.runner_up):
        return strength
    return strength * _ramp(peak.prominence, 0.0, prominence_good)
