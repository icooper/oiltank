import json
import os
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from oiltank.calibration import Calibration
from oiltank.cli import app
from oiltank.filter import Fill, Observation
from oiltank.history import History
from tests.conftest import SAMPLE_CALIBRATION

runner = CliRunner()


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OILTANK_DATA_DIR", str(tmp_path))
    return tmp_path


def test_healthcheck(data: Path) -> None:
    missing = runner.invoke(app, ["healthcheck"])
    assert missing.exit_code == 1
    assert "no heartbeat" in missing.stdout

    beat = data / "heartbeat"
    beat.write_text(
        json.dumps({"ts": "x", "last_reading": "2026-10-13T03:41:00+00:00", "mqtt_connected": True})
    )
    ok = runner.invoke(app, ["healthcheck"])
    assert ok.exit_code == 0, ok.stdout
    assert "MQTT connected" in ok.stdout

    old = time.time() - 600
    os.utime(beat, (old, old))
    stale = runner.invoke(app, ["healthcheck"])
    assert stale.exit_code == 1
    assert "600 s ago" in stale.stdout or "s ago" in stale.stdout


def test_history(data: Path) -> None:
    assert runner.invoke(app, ["history"]).exit_code == 2  # nothing yet
    now = datetime.now(UTC)
    with History(data / "oiltank.db") as h:
        for k in range(4):
            ts = now - timedelta(hours=4 - k)
            h.add(Observation(ts, 0.6 - 0.001 * k, 0.97, 0.6, None), accepted=True, frames=5)
        h.add_fill(Fill(now - timedelta(days=3), 0.2, 0.9))
    result = runner.invoke(app, ["history", "--days", "1"])
    assert result.exit_code == 0, result.output
    assert "4 readings stored; 4 in the last 1 day(s)" in result.stdout
    assert "levels as stored" in result.stdout  # no calibration to work them out with
    assert "refill" in result.stdout
    assert "now: level 59.8%" in result.stdout
    assert "usage not enough data yet" in result.stdout


def test_history_works_levels_out_with_the_current_calibration(data: Path) -> None:
    shutil.copyfile(SAMPLE_CALIBRATION, data / "calibration.yaml")
    owner_t = Calibration.load(SAMPLE_CALIBRATION).t_for(0.98)
    now = datetime.now(UTC)
    with History(data / "oiltank.db") as h:
        for k in range(3):  # stored before the owner's 98% point was added
            h.add(
                Observation(now - timedelta(hours=3 - k), 0.5, 0.97, owner_t, None),
                accepted=True,
                frames=5,
            )
    result = runner.invoke(app, ["history", "--days", "1"])
    assert result.exit_code == 0, result.output
    assert "98.0%" in result.stdout
    assert "now: level 98.0%" in result.stdout
    assert "as stored" not in result.stdout


def test_run_needs_a_calibration(data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OILTANK_MQTT_HOST", "broker")
    monkeypatch.setenv("OILTANK_CAMERA_URL", "http://go2rtc:1984/api/frame.jpeg?src=t")
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 2
    assert "calibrate init" in result.output


def test_run_needs_a_broker_and_camera(data: Path) -> None:
    (data / "calibration.yaml").write_text("")
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 2
    assert "OILTANK_MQTT_HOST" in result.output
