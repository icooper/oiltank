"""The reader interface and factory."""

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from oiltank.calibration import Calibration
from oiltank.model import GaugeStrip, Reading


class GaugeReader(Protocol):
    """Finds the indicator in a rectified gauge strip.

    Implementations return the indicator position ``t`` (1 = top of travel, 0 = bottom) with a
    confidence. They never convert to a level; that's the calibration's job.
    """

    @property
    def name(self) -> str: ...

    def read(self, strip: GaugeStrip) -> Reading: ...


class ReaderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["classical"] = "classical"


def build_reader(config: ReaderConfig, calibration: Calibration) -> GaugeReader:
    """Construct the configured reader. Model-based readers import their runtime lazily."""
    match config.kind:
        case "classical":
            from oiltank.readers.classical import ClassicalReader

            return ClassicalReader(calibration.disc)
