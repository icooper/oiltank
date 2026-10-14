"""Changing the calibration's points from the web UI: points from readings and deliveries, the
draft's preview, and saving (to the file, or through the service)."""

import shutil
import time
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from oiltank.calibration import Calibration, calibration_etag
from oiltank.config import Settings
from oiltank.filter import FilterParams, LevelFilter, Observation
from oiltank.history import History
from oiltank.model import Problem
from oiltank.web.app import create_app
from oiltank.web.control import FileControl, ServiceControl
from tests.conftest import SAMPLES
from tests.fakes import Rig, ScriptedCamera, running

START = datetime(2026, 10, 13, 3, 0, tzinfo=UTC)
LOW, HIGH = 0.40, 0.98
"""Readings at 40% (as the sample calibration has it), then a refill to the samples' 98%."""
LOWS, HIGHS = 6, 3
FAILED = 2
"""This reading found no disc."""


def at(k: int) -> datetime:
    return START + timedelta(minutes=15 * k)


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, calibration: Calibration) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    shutil.copyfile(SAMPLES / "calibration.yaml", data / "calibration.yaml")
    shutil.copyfile(SAMPLES / "frame0.jpg", data / "frame0.jpg")
    lf = LevelFilter(FilterParams(), 250)
    with History(data / "oiltank.db") as h:
        epoch, _ = h.register_epoch("frame0.jpg", calibration.axis, START)
        for k in range(LOWS + HIGHS):
            if k == FAILED:
                obs = Observation(at(k), None, 0.1, None, Problem.LOW_CONFIDENCE)
            else:
                f = LOW if k < LOWS else HIGH
                obs = Observation(at(k), f, 0.97, calibration.t_for(f), None)
            update = lf.update(obs)
            h.add(obs, accepted=update.accepted, frames=5, epoch=epoch)
            if update.fill is not None:
                h.add_fill(update.fill)
    monkeypatch.setenv("OILTANK_DATA_DIR", str(data))
    monkeypatch.setenv("OILTANK_TANK_CAPACITY", "250")
    return data


def client_for(data: Path, *, read_only: bool = False) -> TestClient:
    settings = Settings()
    control = FileControl(settings.data_dir, settings.calibration_path)
    app = create_app(control, settings, read_only=read_only)
    return TestClient(app, base_url="http://oiltank.lan")


@pytest.fixture
def client(data: Path) -> Iterator[TestClient]:
    yield client_for(data)


def calibration_of(c: TestClient) -> dict[str, Any]:
    r = c.get("/api/calibration")
    assert r.status_code == 200
    body: dict[str, Any] = r.json()
    return body


def test_the_calibration(client: TestClient, data: Path, calibration: Calibration) -> None:
    body = calibration_of(client)
    assert body["etag"] == calibration_etag(data / "calibration.yaml")
    assert body["points"] == [
        {"at": [2168.0, 1047.5], "fraction": 0.98, "note": body["points"][0]["note"]}
    ]
    assert body["positions"] == [pytest.approx(calibration.t_for(0.98), abs=1e-4)]
    assert body["warnings"] == []
    assert body["read_only"] is False
    [fill] = body["fills"]
    assert fill["ts"] == at(LOWS).isoformat()
    assert fill["before"] == pytest.approx(40, abs=0.1) and fill["after"] == pytest.approx(
        98, abs=0.1
    )


# ---- points from readings -------------------------------------------------------------------


def test_a_point_from_a_reading(client: TestClient, calibration: Calibration) -> None:
    draft = calibration_of(client)["points"]
    r = client.post(
        "/api/calibration/points/from-reading",
        json={"points": draft, "ts": at(1).isoformat(), "fraction": 0.45, "note": "dipstick"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert [p["fraction"] for p in body["points"]] == [0.45, 0.98]  # in order up the gauge
    point = body["point"]
    assert point["note"] == f"{date.today().isoformat()}: dipstick"
    assert point["at"] == pytest.approx(
        list(calibration.axis.point(calibration.t_for(LOW))), abs=0.1
    )
    assert body["positions"][0] == pytest.approx(calibration.t_for(LOW), abs=1e-3)
    assert body["replaced"] == []


def test_a_point_with_the_same_reading_replaces_it(client: TestClient) -> None:
    draft = calibration_of(client)["points"]
    r = client.post(
        "/api/calibration/points/from-reading",
        json={"points": draft, "ts": at(LOWS + 1).isoformat(), "fraction": 0.98},
    )
    body = r.json()
    assert len(body["points"]) == 1
    assert [p["fraction"] for p in body["replaced"]] == [0.98]
    assert "the reading at 2026-10-13 04:45 UTC" in body["point"]["note"]


@pytest.mark.parametrize(
    ("k", "fraction", "status", "words"),
    [
        (FAILED, 0.4, 422, "didn't find the disc"),
        (100, 0.4, 404, "no reading"),
        (1, 0.99, 422, "contradicts the 98%"),  # 99% below the 98% point
    ],
)
def test_points_from_readings_that_cant_be(
    client: TestClient, k: int, fraction: float, status: int, words: str
) -> None:
    draft = calibration_of(client)["points"]
    r = client.post(
        "/api/calibration/points/from-reading",
        json={"points": draft, "ts": at(k).isoformat(), "fraction": fraction},
    )
    assert r.status_code == status
    assert words in r.json()["detail"]


# ---- points from deliveries -----------------------------------------------------------------


def test_a_point_from_a_delivery(client: TestClient, calibration: Calibration) -> None:
    draft = calibration_of(client)["points"]
    # The ticket says 140 gal: 40% + 140/250 = 96%, not the 98% the gauge showed. The disc is
    # where the 98% point is, so that one is in the way...
    ask = {"fill_ts": at(LOWS).isoformat(), "delivered": 140}
    r = client.post("/api/calibration/points/from-fill", json={"points": draft, **ask})
    assert r.status_code == 422
    assert "contradicts the 98% (" in r.json()["detail"]
    # ...until it's removed from the draft.
    draft = []
    r = client.post("/api/calibration/points/from-fill", json={"points": draft, **ask})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["before"] == pytest.approx(40, abs=0.1)
    assert body["after"] == pytest.approx(96, abs=0.1)
    assert body["point"]["fraction"] == pytest.approx(0.96, abs=1e-3)
    assert body["point"]["at"] == pytest.approx(
        list(calibration.axis.point(calibration.t_for(HIGH))), abs=0.1
    )
    assert "delivery on 2026-10-13: 140 gal on the ticket" in body["point"]["note"]
    assert [p["fraction"] for p in body["replaced"]] == []
    assert len(body["points"]) == 1


@pytest.mark.parametrize(
    ("fill_ts", "delivered", "status", "words"),
    [
        (at(LOWS) + timedelta(hours=5), 140, 404, "no refill"),
        (at(LOWS), 200, 422, "more than the tank holds"),
    ],
)
def test_deliveries_that_cant_be(
    client: TestClient, fill_ts: datetime, delivered: float, status: int, words: str
) -> None:
    draft = calibration_of(client)["points"]
    r = client.post(
        "/api/calibration/points/from-fill",
        json={"points": draft, "fill_ts": fill_ts.isoformat(), "delivered": delivered},
    )
    assert r.status_code == status
    assert words in r.json()["detail"]


# ---- previews and saving --------------------------------------------------------------------


RANGE = {"start": START.isoformat(), "end": (START + timedelta(days=1)).isoformat()}


def test_preview(client: TestClient) -> None:
    draft = calibration_of(client)["points"]
    draft[0]["fraction"] = 0.90
    r = client.post("/api/calibration/preview", json={"points": draft, **RANGE})
    assert r.status_code == 200
    levels = r.json()["readings"]["level"]
    assert levels[-1] == pytest.approx(90, abs=0.1)
    assert levels[0] == pytest.approx(LOW * 90 / 98 * 100, abs=0.5)
    nonsense = [{"t": 0.2, "fraction": 0.9}, {"t": 0.8, "fraction": 0.1}]
    assert client.post("/api/calibration/preview", json={"points": nonsense}).status_code == 422
    extra = [{"t": 0.2, "fraction": 0.9, "position": 3}]
    assert client.post("/api/calibration/preview", json={"points": extra}).status_code == 422


def test_saving(client: TestClient, data: Path) -> None:
    before = calibration_of(client)
    draft = before["points"]
    draft[0]["fraction"] = 0.95
    headers = {"If-Match": f'"{before["etag"]}"'}
    r = client.put("/api/calibration", json={"points": draft}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["etag"] == calibration_etag(data / "calibration.yaml") != before["etag"]
    assert Calibration.load(data / "calibration.yaml").points[0].fraction == 0.95
    assert (data / "calibration.yaml.bak").exists()
    levels = client.get("/api/readings", params=RANGE).json()["readings"]["level"]
    assert levels[-1] == pytest.approx(95, abs=0.1)  # the history follows at once

    again = client.put("/api/calibration", json={"points": draft}, headers=headers)  # stale
    assert again.status_code == 412
    assert "changed since" in again.json()["detail"]


def test_saving_needs_if_match_and_a_writable_ui(client: TestClient, data: Path) -> None:
    draft = calibration_of(client)["points"]
    assert client.put("/api/calibration", json={"points": draft}).status_code == 428
    etag = calibration_of(client)["etag"]
    locked = client_for(data, read_only=True)
    r = locked.put("/api/calibration", json={"points": draft}, headers={"If-Match": etag})
    assert r.status_code == 403
    assert calibration_of(locked)["read_only"] is True


def test_saving_through_the_service(rig: Rig, settings: Settings, camera: ScriptedCamera) -> None:
    rig.service.cycle()
    c = TestClient(
        create_app(ServiceControl(rig.service, rig.events), settings, read_only=False),
        base_url="http://oiltank.lan",
    )
    body = calibration_of(c)
    draft = body["points"]
    draft[0]["fraction"] = 0.90
    published = len(rig.publisher.states)
    with running(rig):
        wait_for(lambda: camera.calls == 2)  # the loop's first reading
        r = c.put(
            "/api/calibration",
            json={"points": draft, "read_now": True},
            headers={"If-Match": body["etag"]},
        )
        assert r.status_code == 200, r.text
        snap = rig.service.snapshot
        assert snap.calibration.points[0].fraction == 0.90
        assert snap.calibration_etag == r.json()["etag"]
        assert snap.state is not None and snap.state.fraction == pytest.approx(0.90, abs=0.01)
        assert len(rig.publisher.states) > published  # published straight away
        wait_for(lambda: camera.calls == 3)  # and read now, as asked


def wait_for(condition: Callable[[], bool], timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.05)
