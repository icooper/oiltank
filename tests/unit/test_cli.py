import json
from pathlib import Path

import cv2
import numpy as np
from typer.testing import CliRunner

from oiltank.cli import app
from tests.conftest import SAMPLE_CALIBRATION

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.startswith("oiltank ")


def test_read_single_image(sample_frame_paths: list[Path]) -> None:
    args = ["read", "-c", str(SAMPLE_CALIBRATION), "--image", str(sample_frame_paths[0])]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("level 9")


def test_read_burst_json_and_images(sample_frame_paths: list[Path], tmp_path: Path) -> None:
    out, diag = tmp_path / "gauge.jpg", tmp_path / "diagnostic.png"
    args = ["read", "-c", str(SAMPLE_CALIBRATION), "--json"]
    args += ["--annotate", str(out), "--diagnostic", str(diag)]
    for p in sample_frame_paths:
        args += ["--image", str(p)]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    summary = json.loads(result.stdout)
    assert summary["ok"] is True
    assert len(summary["frames"]) == 6
    assert 0.97 < summary["fraction"] < 0.99
    assert out.read_bytes()[:2] == b"\xff\xd8"
    assert diag.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_diagnostic_written_even_without_a_reading(
    sample_frame_paths: list[Path], tmp_path: Path
) -> None:
    # Clear the frame so the anchor can't be found: no reading, but the image still helps.
    blank = tmp_path / "blank.png"
    cv2.imwrite(str(blank), np.zeros((2160, 3840), np.uint8))
    diag = tmp_path / "diagnostic.jpg"
    args = ["read", "-c", str(SAMPLE_CALIBRATION), "--image", str(blank), "--diagnostic", str(diag)]
    result = runner.invoke(app, args)
    assert result.exit_code == 1
    assert "camera_moved" in result.stdout
    assert diag.read_bytes()[:2] == b"\xff\xd8"


def test_read_missing_calibration(sample_frame_paths: list[Path], tmp_path: Path) -> None:
    args = ["read", "-c", str(tmp_path / "nope.yaml"), "--image", str(sample_frame_paths[0])]
    result = runner.invoke(app, args)
    assert result.exit_code == 2
    assert "cannot read calibration" in result.output
