from pathlib import Path

from typer.testing import CliRunner

from oiltank.cli import app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.startswith("oiltank ")


def test_read_image_reports_size(sample_frame_paths: list[Path]) -> None:
    result = runner.invoke(app, ["read", "--image", str(sample_frame_paths[0])])
    assert result.exit_code == 0, result.output
    assert "3840x2160" in result.stdout
