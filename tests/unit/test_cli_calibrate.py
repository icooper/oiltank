"""``oiltank calibrate`` commands, run against copies of the sample frames in a temp dir."""

import shutil
from pathlib import Path

import cv2
import pytest
from typer.testing import CliRunner, Result

from oiltank.calibration import Calibration
from oiltank.cli import app
from oiltank.sources.files import load_frame
from tests.conftest import SAMPLE_CALIBRATION
from tests.cv import synth

runner = CliRunner()
TOP = ["--top", "2166", "1020"]
BOTTOM = ["--bottom", "2166", "1172"]


def _run(*args: str) -> Result:
    return runner.invoke(app, ["calibrate", *args])


@pytest.fixture
def frame0(sample_frame_paths: list[Path]) -> Path:
    return sample_frame_paths[0]


@pytest.fixture
def initialized(tmp_path: Path, frame0: Path) -> Path:
    """A calibration made by `calibrate init` from frame0."""
    path = tmp_path / "data" / "calibration.yaml"
    result = _run("init", "-c", str(path), "--image", str(frame0), *TOP, *BOTTOM)
    assert result.exit_code == 0, result.output
    return path


def test_init_scans_copies_reference_and_saves(initialized: Path) -> None:
    cal = Calibration.load(initialized)
    assert cal.frame_size == (3840, 2160)
    assert cal.points == []
    assert cal.reference_image.name.startswith("reference-")
    assert (
        cal.reference_path.read_bytes() == (SAMPLE_CALIBRATION.parent / "frame0.jpg").read_bytes()
    )
    # Scanned size: the reading holds around it (see test_calibrate.py for the scan itself).
    assert 50 <= cal.disc.width <= 75
    assert 35 <= cal.disc.height <= 55
    assert not (initialized.parent / "calibration.yaml.bak").exists()


def test_init_report(tmp_path: Path, frame0: Path) -> None:
    path = tmp_path / "calibration.yaml"
    result = _run("init", "-c", str(path), "--image", str(frame0), *TOP, *BOTTOM, "--dry-run")
    assert result.exit_code == 0, result.output
    assert "scanned for the disc size" in result.stdout
    assert "reading      8" in result.stdout  # 82%: no points yet, so the top of travel is F
    assert "none: E is the bottom of travel and F the top" in result.stdout
    assert "dry run: not saved" in result.stdout
    assert not path.exists()
    assert not list(tmp_path.iterdir())


def test_init_refuses_to_overwrite_without_force(initialized: Path, frame0: Path) -> None:
    args = ["init", "-c", str(initialized), "--image", str(frame0), *TOP, *BOTTOM]
    result = _run(*args)
    assert result.exit_code == 2
    assert "--force" in result.output
    assert _run(*args, "--force", "--disc", "60", "40").exit_code == 0
    assert (initialized.parent / "calibration.yaml.bak").exists()
    assert Calibration.load(initialized).disc.width == 60


def test_init_rejects_a_degenerate_axis(tmp_path: Path, frame0: Path) -> None:
    path = tmp_path / "c.yaml"
    args = ["init", "-c", str(path), "--image", str(frame0), *TOP, "--bottom", "2166", "1022"]
    result = _run(*args)
    assert result.exit_code == 2
    assert "5 px apart" in result.output


def test_point_measures_and_stores_a_position(initialized: Path, frame0: Path) -> None:
    result = _run("point", "-c", str(initialized), "--fraction", "0.98", "--note", "owner")
    assert result.exit_code == 0, result.output
    assert "measured the disc at (2168" in result.stdout
    assert "reading      98.0%" in result.stdout
    cal = Calibration.load(initialized)
    (point,) = cal.points
    assert point.at is not None
    assert point.at[1] == pytest.approx(1047.7, abs=2)
    assert point.note.endswith(": owner")

    # Same reading again replaces it; --remove takes it out.
    again = _run("point", "-c", str(initialized), "--fraction", "0.98")
    assert "replaces the 98.0% point" in again.stdout
    assert len(Calibration.load(initialized).points) == 1
    assert _run("point", "-c", str(initialized), "--fraction", "0.98", "--remove").exit_code == 0
    assert Calibration.load(initialized).points == []
    missing = _run("point", "-c", str(initialized), "--fraction", "0.98", "--remove")
    assert missing.exit_code == 2


def test_point_rejects_low_confidence(initialized: Path, tmp_path: Path) -> None:
    frame = load_frame(SAMPLE_CALIBRATION.parent / "frame0.jpg")
    removed = tmp_path / "no-disc.png"
    cv2.imwrite(str(removed), synth.remove_disc(frame.gray))
    args = ["point", "-c", str(initialized), "--fraction", "0.5", "--image", str(removed)]
    result = _run(*args)
    assert result.exit_code == 1
    assert "no reading to record" in result.output or "confidence" in result.output


def test_axis_edit_keeps_measured_points(initialized: Path) -> None:
    assert _run("point", "-c", str(initialized), "--fraction", "0.98").exit_code == 0
    result = _run("axis", "-c", str(initialized), "--bottom", "2166", "1185")
    assert result.exit_code == 0, result.output
    assert "reading      98.0%" in result.stdout
    assert Calibration.load(initialized).axis.bottom == (2166, 1185)
    assert _run("axis", "-c", str(initialized)).exit_code == 2


def test_axis_edit_warns_about_t_points(initialized: Path) -> None:
    assert _run("point", "-c", str(initialized), "--fraction", "0.98", "--t", "0.82").exit_code == 0
    result = _run("axis", "-c", str(initialized), "--top", "2166", "1010", "--dry-run")
    assert "1 point(s) given as t move with the axis" in result.stdout


def test_disc_size_and_scan(initialized: Path) -> None:
    assert _run("disc", "-c", str(initialized), "--size", "70", "45").exit_code == 0
    assert Calibration.load(initialized).disc == Calibration.load(initialized).disc.updated(
        width=70, height=45
    )
    result = _run("disc", "-c", str(initialized), "--scan")
    assert result.exit_code == 0, result.output
    assert "#" in result.stdout  # the chosen size, marked in the table
    assert _run("disc", "-c", str(initialized)).exit_code == 2
    assert _run("disc", "-c", str(initialized), "--size", "1", "1", "--scan").exit_code == 2


def test_anchor_edit_reports_problems(initialized: Path) -> None:
    result = _run("anchor", "-c", str(initialized), "--rect", "2100", "1000", "140", "120")
    assert result.exit_code == 0, result.output
    assert "overlaps the disc's travel" in result.output
    outside = _run("anchor", "-c", str(initialized), "--rect", "3800", "2100", "100", "100")
    assert outside.exit_code == 2
    assert "outside the 3840x2160 frame" in outside.output


def test_rebase_follows_a_bumped_camera(initialized: Path, tmp_path: Path) -> None:
    assert _run("point", "-c", str(initialized), "--fraction", "0.98").exit_code == 0
    frame = load_frame(SAMPLE_CALIBRATION.parent / "frame0.jpg")
    bumped = tmp_path / "bumped.png"
    cv2.imwrite(str(bumped), synth.shift(frame.gray, 40, -25))
    result = _run("rebase", "-c", str(initialized), "--image", str(bumped))
    assert result.exit_code == 0, result.output
    assert "the camera moved +40.0, -25.0 px" in result.stdout
    assert "reading      98.0%" in result.stdout
    cal = Calibration.load(initialized)
    assert cal.axis.top == pytest.approx((2166 + 40, 1020 - 25), abs=0.1)
    assert cal.reference_path.suffix == ".png"


def test_rebase_gives_up_when_the_view_changed(initialized: Path, tmp_path: Path) -> None:
    # Mirrored, the scene still has a similar-looking fitting within reach (it scores ~0.77).
    frame = load_frame(SAMPLE_CALIBRATION.parent / "frame0.jpg")
    other = tmp_path / "other.png"
    cv2.imwrite(str(other), cv2.flip(frame.gray, 1))
    result = _run("rebase", "-c", str(initialized), "--image", str(other))
    assert result.exit_code == 1
    assert "calibrate init" in result.output


def test_show_and_diagnostic(tmp_path: Path, sample_frame_paths: list[Path]) -> None:
    diag = tmp_path / "check.jpg"
    args = ["show", "-c", str(SAMPLE_CALIBRATION), "--diagnostic", str(diag)]
    for p in sample_frame_paths[-2:]:
        args += ["--image", str(p)]
    result = _run(*args)
    assert result.exit_code == 0, result.output
    for label in ("axis", "disc", "anchor", "scale", "points", "reading"):
        assert f"\n{label}" in result.stdout
    assert "reading      9" in result.stdout
    assert diag.read_bytes()[:2] == b"\xff\xd8"


def test_show_reports_a_broken_calibration(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(SAMPLE_CALIBRATION.read_text().replace("half_width: 50.0", "half_width: -1"))
    result = _run("show", "-c", str(path))
    assert result.exit_code == 2
    assert "axis.half_width" in result.output


def test_grid(tmp_path: Path, frame0: Path) -> None:
    out = tmp_path / "grid.png"
    assert _run("grid", "--image", str(frame0), "--out", str(out)).exit_code == 0
    img = cv2.imread(str(out))
    assert img is not None
    assert img.shape[1] == 1600
    zoom = tmp_path / "zoom.jpg"
    args = [
        "grid",
        "--image",
        str(frame0),
        "--out",
        str(zoom),
        "--region",
        "2050",
        "950",
        "250",
        "300",
    ]
    assert _run(*args, "--width", "500").exit_code == 0
    zoomed = cv2.imread(str(zoom))
    assert zoomed is not None
    assert zoomed.shape[:2] == (600, 500)
    bad = _run(
        "grid", "--image", str(frame0), "--out", str(zoom), "--region", "5000", "0", "10", "10"
    )
    assert bad.exit_code == 2


def test_reference_in_the_calibration_dir_is_used_in_place(tmp_path: Path, frame0: Path) -> None:
    local = tmp_path / "frame0.jpg"
    shutil.copyfile(frame0, local)
    path = tmp_path / "calibration.yaml"
    result = _run("init", "-c", str(path), "--image", str(local), *TOP, *BOTTOM)
    assert result.exit_code == 0, result.output
    assert Calibration.load(path).reference_image == Path("frame0.jpg")
