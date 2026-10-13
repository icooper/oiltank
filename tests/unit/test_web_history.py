"""The History view's API, over a data directory made the way the service makes one: real
readings of the sample frames, archived, and stored in the history."""

import shutil
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from oiltank.archive import Archive, ArchivePolicy
from oiltank.calibration import Calibration, CalibrationPoint
from oiltank.config import Settings
from oiltank.filter import Observation
from oiltank.geometry import Aligner
from oiltank.history import History
from oiltank.model import Frame
from oiltank.pipeline import measure
from oiltank.readers import GaugeReader
from oiltank.web.app import create_app
from oiltank.web.control import FileControl
from tests.conftest import SAMPLES
from tests.cv import synth

START = datetime(2026, 10, 13, 3, 0, tzinfo=UTC)
STEADY, LOWER = 5, 3
"""Readings at the samples' 98%, then readings with the disc moved down."""
DROP_PX = 30
DAY = "2026-10-13"


def _burst(frames: list[Frame], ts: datetime, drop: int) -> list[Frame]:
    out = []
    for f in frames:
        gray = synth.move_disc(f.gray, synth.DISC_CENTRE[1] + drop) if drop else f.gray
        ok, jpeg = cv2.imencode(".jpg", gray)
        assert ok
        out.append(Frame(gray=gray, ts=ts, source=f.source, data=jpeg.tobytes()))
    return out


@pytest.fixture(scope="module")
def populated(
    tmp_path_factory: pytest.TempPathFactory,
    sample_frames: list[Frame],
    aligner: Aligner,
    reader: GaugeReader,
    calibration: Calibration,
) -> Path:
    data = tmp_path_factory.mktemp("populated") / "data"
    data.mkdir()
    shutil.copyfile(SAMPLES / "calibration.yaml", data / "calibration.yaml")
    shutil.copyfile(SAMPLES / "frame0.jpg", data / "frame0.jpg")
    archive = Archive(data / "archive", ArchivePolicy(full_frame_every=timedelta(hours=1)))
    with History(data / "oiltank.db") as h:
        epoch, _ = h.register_epoch("frame0.jpg", calibration.axis, START)
        for k in range(STEADY + LOWER):
            ts = START + timedelta(minutes=15 * k)
            frames = _burst(sample_frames[k % 5 : k % 5 + 2], ts, DROP_PX if k >= STEADY else 0)
            m = measure(frames, aligner, reader, calibration)
            archive.record(m, frames, calibration)
            obs = Observation(m.ts, m.fraction if m.ok else None, m.confidence, m.t, m.problem)
            h.add(obs, accepted=True, frames=len(frames), epoch=epoch)
    return data


def client_for(data: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("OILTANK_DATA_DIR", str(data))
    monkeypatch.setenv("OILTANK_TANK_CAPACITY", "250")
    settings = Settings()
    control = FileControl(settings.data_dir, settings.calibration_path)
    return TestClient(create_app(control, settings, read_only=False), base_url="http://oiltank.lan")


@pytest.fixture
def client(populated: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    yield client_for(populated, monkeypatch)


@pytest.fixture
def own_copy(populated: Path, tmp_path: Path) -> Path:
    """The data directory, to change."""
    copy = tmp_path / "data"
    shutil.copytree(populated, copy)
    return copy


RANGE = {"start": START.isoformat(), "end": (START + timedelta(days=1)).isoformat()}


# ---- readings -------------------------------------------------------------------------------


def test_readings(client: TestClient) -> None:
    r = client.get("/api/readings", params=RANGE)
    assert r.status_code == 200
    body = r.json()
    cols = body["readings"]
    assert len(cols["ts"]) == STEADY + LOWER
    assert cols["ts"][0] == START.timestamp()
    assert cols["level"][:STEADY] == pytest.approx([98] * STEADY, abs=1)
    assert max(cols["level"][STEADY:]) < 90  # the disc moved down
    assert cols["smoothed"][0] == pytest.approx(98, abs=1)
    assert body["unit"] == "gal" and body["capacity"] == 250
    labels = [tick["label"] for tick in body["scale"]["ticks"]]
    assert labels == ["F", "E", "98%"]
    again = client.get("/api/readings", params=RANGE, headers={"If-None-Match": r.headers["etag"]})
    assert again.status_code == 304


def test_readings_follow_the_calibration_file(
    own_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = own_copy / "calibration.yaml"
    cal = Calibration.load(path)
    at = cal.points[0].at
    cal.updated(points=[CalibrationPoint(at=at, fraction=0.9)]).save(path)
    levels = client_for(own_copy, monkeypatch).get("/api/readings", params=RANGE).json()
    assert levels["readings"]["level"][0] == pytest.approx(90, abs=1)


@pytest.mark.parametrize(
    "params",
    [
        {"start": "2026-10-14T00:00:00Z", "end": "2026-10-13T00:00:00Z"},
        {"start": "2023-01-01T00:00:00Z", "end": "2026-10-13T00:00:00Z"},
        {"start": "yesterday"},
    ],
)
def test_bad_ranges(client: TestClient, params: dict[str, str]) -> None:
    assert client.get("/api/readings", params=params).status_code == 422


def test_no_history_yet(own_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (own_copy / "oiltank.db").unlink()
    body = client_for(own_copy, monkeypatch).get("/api/readings", params=RANGE).json()
    assert body["readings"]["ts"] == [] and body["fills"] == []


def test_no_calibration(own_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (own_copy / "calibration.yaml").unlink()
    r = client_for(own_copy, monkeypatch).get("/api/readings", params=RANGE)
    assert r.status_code == 409
    assert "calibration" in r.json()["detail"]


# ---- the archive ----------------------------------------------------------------------------


def test_a_day_of_records_and_its_tile(client: TestClient) -> None:
    body = client.get(f"/api/archive/days/{DAY}").json()
    records = body["records"]
    assert len(records) == STEADY + LOWER
    assert records[0]["key"] == "030000Z" and records[0]["ts"] == START.timestamp()
    assert all(r["strip"] for r in records)
    assert records[0]["frame"] and not records[1]["frame"]
    assert records[0]["level"] == pytest.approx(98, abs=1)

    tile = client.get(body["tile"])
    assert tile.status_code == 200
    assert tile.headers["content-type"] == "image/png"
    assert "immutable" in tile.headers["cache-control"]
    image = np.asarray(
        cv2.imdecode(np.frombuffer(tile.content, np.uint8), cv2.IMREAD_UNCHANGED), np.uint8
    )
    assert image.shape == (256, STEADY + LOWER, 4)
    assert image[:, :, 3].max() == 255

    # The disc moved down the gauge, so its band moved down the image.
    ticks = client.get("/api/readings", params=RANGE).json()["scale"]
    column = image[:, :, 0].astype(float)
    steady, lower = column[:, 0], column[:, -1]
    shift = _band_shift(steady, lower)
    rows_per_t = ticks["rows"] / (ticks["t_top"] - ticks["t_bottom"])
    expected = records[0]["t"] - records[-1]["t"]
    assert shift == pytest.approx(expected * rows_per_t, abs=3)

    stale = client.get(f"/api/kymograph/{DAY}.png?v=old")
    assert stale.headers["cache-control"] == "no-cache"


def _band_shift(a: np.ndarray, b: np.ndarray) -> int:
    """How many rows down ``b`` has to be shifted to match ``a`` best."""

    def score(k: int) -> float:
        return float(np.corrcoef(a[: 256 - k], b[k:])[0, 1])

    return max(range(0, 60), key=score)


def test_unknown_days_and_records(client: TestClient) -> None:
    assert client.get("/api/archive/days/2026-13-45").status_code == 404
    assert client.get("/api/archive/days/../etc").status_code == 404
    assert client.get("/api/archive/days/2026-10-12").json()["records"] == []
    assert client.get("/api/kymograph/2026-10-12.png").status_code == 404
    assert client.get(f"/api/archive/records/{DAY}/999999Z").status_code == 404


def test_a_record_for_the_inspector(client: TestClient) -> None:
    detail = client.get(f"/api/archive/records/{DAY}/030000Z").json()
    assert detail["level"] == pytest.approx(98, abs=1)
    assert detail["rows"] == 256
    assert detail["t_first"] > detail["t"] > detail["t_last"]
    rows = {tick["label"]: tick["row"] for tick in detail["ticks"]}
    assert rows["F"] < rows["98%"] < rows["E"]  # F at the top
    assert rows["98%"] == pytest.approx(detail["disc_row"], abs=2)  # the owner's point is the disc
    assert len(detail["frames"]) == 2
    assert detail["frames"][0]["alignment"]["ok"] is True

    strip = client.get(detail["strip"])
    assert strip.status_code == 200 and "immutable" in strip.headers["cache-control"]

    frame = detail["frame"]
    assert frame["key"] == "030000Z" and frame["age_s"] == 0
    x, y, w, h = frame["crop"]
    disc_x, disc_y = frame["disc"]
    assert x < disc_x < x + w and y < disc_y < y + h
    assert disc_y == pytest.approx(1048, abs=6)
    assert client.get(frame["url"]).content[:2] == b"\xff\xd8"


def test_a_record_without_a_full_frame_points_to_the_nearest(client: TestClient) -> None:
    detail = client.get(f"/api/archive/records/{DAY}/031500Z").json()
    assert detail["frame"]["key"] == "030000Z"
    assert detail["frame"]["age_s"] == -15 * 60


def test_scores_read_the_strip_again(client: TestClient) -> None:
    detail = client.get(f"/api/archive/records/{DAY}/030000Z").json()
    scores = client.get(f"/api/archive/records/{DAY}/030000Z/scores").json()
    assert len(scores["scores"]) == 256
    assert scores["row"] == pytest.approx(detail["disc_row"], abs=0.5)
    best = max(range(256), key=lambda i: scores["scores"][i] or -1)
    assert best == pytest.approx(detail["disc_row"], abs=1)


@pytest.mark.parametrize(
    "path",
    [
        f"/archive/{DAY}/oiltank.db",
        f"/archive/{DAY}/..%2F..%2Foiltank.db",
        "/archive/../oiltank.db",
        f"/archive/{DAY}/030000Z-strip.png/..",
    ],
)
def test_only_archive_files_are_served(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 404


def test_an_older_history_is_browsable(own_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A history from the MVP's service (schema 1, no epochs) still shows its readings."""
    db = sqlite3.connect(own_copy / "oiltank.db")
    db.executescript(
        "DROP TABLE epochs; ALTER TABLE readings DROP COLUMN epoch; PRAGMA user_version = 1;"
    )
    db.close()
    body = client_for(own_copy, monkeypatch).get("/api/readings", params=RANGE).json()
    assert body["readings"]["level"][0] == pytest.approx(98, abs=1)
