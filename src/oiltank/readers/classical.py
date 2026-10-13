"""Classical-CV reader: slide an elliptical disc template along the gauge strip.

Seen by the IR camera, the indicator is a bright, roughly elliptical disc (its top face, viewed
from slightly above) inside the clear dome. The template is a bright ellipse of the calibrated
disc size inside a darker ring, built from geometry rather than cut from a photo, so it doesn't
carry over whatever wall texture happened to be behind the disc at calibration time. Matching uses
masked normalized cross-correlation, so it is insensitive to IR brightness and contrast changes.

The disc rides on a central rod, so the horizontal search is limited to a few px either side of
the axis; this keeps bright wall texture or glare at the dome's edges from being picked up.
"""

from dataclasses import dataclass

import cv2
import numpy as np

from oiltank.calibration import Disc
from oiltank.model import FloatArray, GaugeStrip, Reading
from oiltank.readers.profile import find_peak, peak_confidence


@dataclass(frozen=True, slots=True)
class ClassicalParams:
    ring: float = 0.15
    """Width of the dark ring around the template ellipse, as a fraction of the disc height."""
    lateral: float = 0.1
    """How far the disc centre may sit either side of the axis, as a fraction of disc width."""
    blur_sigma: float = 1.0
    """Gaussian blur applied to the strip first, in strip px (tames JPEG blocking)."""
    score_floor: float = 0.30
    score_good: float = 0.50
    prominence_good: float = 0.15


def ellipse_template(width: float, height: float, ring: float) -> tuple[FloatArray, FloatArray]:
    """A ``(template, mask)`` pair: +1 inside the ellipse, -1 in the ring around it, masked outside.

    All sizes are in strip px. Template dimensions are odd so the centre falls on a pixel.
    """
    w = int(np.ceil(width + 2 * ring)) | 1
    h = int(np.ceil(height + 2 * ring)) | 1
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    cx, cy = (w - 1) / 2, (h - 1) / 2
    inner = ((xx - cx) / (width / 2)) ** 2 + ((yy - cy) / (height / 2)) ** 2
    outer = ((xx - cx) / (width / 2 + ring)) ** 2 + ((yy - cy) / (height / 2 + ring)) ** 2
    template = np.zeros((h, w), np.float32)
    template[inner <= 1] = 1.0
    template[(inner > 1) & (outer <= 1)] = -1.0
    mask = (outer <= 1).astype(np.float32)
    return template, mask


class ClassicalReader:
    name = "classical"

    def __init__(self, disc: Disc, params: ClassicalParams | None = None) -> None:
        self._disc = disc
        self._params = params or ClassicalParams()
        self._cache: dict[tuple[float, float], tuple[FloatArray, FloatArray]] = {}

    def _template(self, strip: GaugeStrip) -> tuple[FloatArray, FloatArray]:
        key = (round(strip.px_per_col, 4), round(strip.px_per_row, 4))
        if key not in self._cache:
            p = self._params
            ring = p.ring * self._disc.height / min(strip.px_per_col, strip.px_per_row)
            self._cache[key] = ellipse_template(
                self._disc.width / strip.px_per_col, self._disc.height / strip.px_per_row, ring
            )
        return self._cache[key]

    def read(self, strip: GaugeStrip) -> Reading:
        p = self._params
        template, mask = self._template(strip)
        th, tw = template.shape
        scores = np.full(strip.rows, np.nan, np.float32)
        if th > strip.rows or tw > strip.cols:
            return Reading(t=None, confidence=0.0, row=None, col=None, scores=scores)

        image = np.asarray(cv2.GaussianBlur(strip.pixels, (0, 0), p.blur_sigma), dtype=np.float32)
        result = np.asarray(
            cv2.matchTemplate(image, template, cv2.TM_CCOEFF_NORMED, mask=mask), dtype=np.float32
        )
        result = np.nan_to_num(result, nan=-1.0, posinf=-1.0, neginf=-1.0)

        # Limit the horizontal search to near the axis (template centred = strip centre).
        centred = (strip.cols - tw) // 2
        reach = max(1, round(p.lateral * self._disc.width / strip.px_per_col))
        lo, hi = max(0, centred - reach), min(result.shape[1], centred + reach + 1)
        band = result[:, lo:hi]
        per_row = band.max(axis=1)
        best_col = band.argmax(axis=1) + lo

        top = th // 2
        scores[top : top + len(per_row)] = per_row

        peak = find_peak(scores, exclusion=self._disc.height / strip.px_per_row)
        if peak is None:
            return Reading(t=None, confidence=0.0, row=None, col=None, scores=scores)
        confidence = peak_confidence(
            peak,
            score_floor=p.score_floor,
            score_good=p.score_good,
            prominence_good=p.prominence_good,
        )
        k = min(max(round(peak.row) - top, 0), len(best_col) - 1)
        col = float(best_col[k] + tw // 2)
        return Reading(
            t=strip.t_at(peak.row),
            confidence=confidence,
            row=peak.row,
            col=col,
            scores=scores,
            debug={"score": peak.score, "runner_up": peak.runner_up},
        )
