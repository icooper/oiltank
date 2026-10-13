"""Editing the calibration's geometry from the web UI: frames, checking a draft, the disc-size
scan, grabbing from the camera, and rebasing after the camera moved."""

import base64
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from oiltank.archive import Archive, ArchivePolicy
from oiltank.calibration import Calibration, calibration_etag
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
from tests.fakes import serve_go2rtc

START = datetime(2026, 10, 13, 3, 0, tzinfo=UTC)
BUMP = (14, -9)
"""The camera was nudged this far (px) before the second reading."""
DAY = "2026-10-13"


def _burst(frames: list[Frame], ts: datetime, shift: tuple[int, int] | None) -> list[Frame]:
    out = []
    for f in frames:
        gray = synth.shift(f.gray, *shift) if shift else f.gray
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
    data = tmp_path_factory.mktemp("geometry") / "data"
    data.mkdir()
    shutil.copyfile(SAMPLES / "calibration.yaml", data / "calibration.yaml")
    shutil.copyfile(SAMPLES / "frame0.jpg", data / "frame0.jpg")
    archive = Archive(data / "archive", ArchivePolicy(full_frame_every=timedelta(minutes=30)))
    with History(data / "oiltank.db") as h:
        epoch, _ = h.register_epoch("frame0.jpg", calibration.axis, START)
        for k, shift in enumerate([None, BUMP]):
            frames = _burst(sample_frames[k : k + 2], START + timedelta(hours=k), shift)
            m = measure(frames, aligner, reader, calibration)
            archive.record(m, frames, calibration)
            obs = Observation(m.ts, m.fraction, m.confidence, m.t, m.problem)
            h.add(obs, accepted=True, frames=2, epoch=epoch)
    return data


@pytest.fixture
def data(populated: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    copy = tmp_path / "data"
    shutil.copytree(populated, copy)
    monkeypatch.setenv("OILTANK_DATA_DIR", str(copy))
    monkeypatch.setenv("OILTANK_TANK_CAPACITY", "250")
    return copy


def client_for(data: Path) -> TestClient:
    settings = Settings()
    control = FileControl(settings.data_dir, settings.calibration_path)
    return TestClient(create_app(control, settings, read_only=False), base_url="http://oiltank.lan")


@pytest.fixture
def client(data: Path) -> Iterator[TestClient]:
    yield client_for(data)


def saved(c: TestClient) -> dict[str, Any]:
    body: dict[str, Any] = c.get("/api/calibration").json()
    return body


def draft_of(cal: dict[str, Any]) -> dict[str, Any]:
    return {k: cal[k] for k in ("points", "axis", "disc", "anchor")}


ARCHIVED = {"kind": "archive", "day": DAY, "key": "030000Z"}
BUMPED = {"kind": "archive", "day": DAY, "key": "040000Z"}


# ---- frames ---------------------------------------------------------------------------------


def test_frames_to_edit_on(client: TestClient) -> None:
    body = client.get("/api/frames").json()
    assert body["reference"] == {"name": "frame0.jpg", "url": "api/frames/reference.jpg"}
    assert body["camera"] is False
    assert [f["key"] for f in body["archive"] if f["day"] == DAY] == ["040000Z", "030000Z"]
    reference = client.get("/api/frames/reference.jpg")
    assert reference.content == (SAMPLES / "frame0.jpg").read_bytes()


# ---- checking a draft -----------------------------------------------------------------------


def test_checking_the_saved_geometry(client: TestClient) -> None:
    r = client.post("/api/calibration/check", json=draft_of(saved(client)))
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] and body["warnings"] == []
    assert body["anchor"]["ok"] and body["anchor"]["problems"] == []
    reading = body["reading"]
    assert reading["ok"] and reading["level"] == pytest.approx(98, abs=1)
    assert reading["position"][1] == pytest.approx(1048, abs=4)
    assert [m["label"] for m in body["marks"]] == ["F", "E", "98%"]


def test_checking_on_a_bumped_frame(client: TestClient) -> None:
    body = client.post(
        "/api/calibration/check", json={**draft_of(saved(client)), "frames": [BUMPED]}
    ).json()
    [frame] = body["reading"]["frames"][:1]
    assert (frame["dx"], frame["dy"]) == pytest.approx(BUMP, abs=0.5)
    assert body["reading"]["level"] == pytest.approx(98, abs=1)  # the anchor took up the bump


def test_a_draft_that_isnt_a_calibration(client: TestClient) -> None:
    draft = draft_of(saved(client))
    draft["axis"]["bottom"] = draft["axis"]["top"]
    body = client.post("/api/calibration/check", json=draft).json()
    assert body["valid"] is False
    assert "5 px apart" in body["errors"][0]


def test_a_draft_that_reads_differently(client: TestClient) -> None:
    draft = draft_of(saved(client))
    draft["axis"]["top"][1] -= 40  # a longer axis: the disc is lower down it
    body = client.post("/api/calibration/check", json=draft).json()
    assert body["valid"]
    assert body["reading"]["t"] < saved(client)["positions"][0]


def test_the_disc_size_scan(client: TestClient) -> None:
    r = client.post("/api/calibration/scan", json=draft_of(saved(client)))
    assert r.status_code == 200
    body = r.json()
    assert len(body["widths"]) == len(body["heights"]) == 9
    assert body["current"] == [4, 4]
    assert body["stable"][4][4] is True
    assert body["best"] is not None
    assert body["consensus_t"] == pytest.approx(saved(client)["positions"][0], abs=0.02)


def test_missing_frames(client: TestClient) -> None:
    gone = {"kind": "archive", "day": DAY, "key": "235959Z"}
    r = client.post("/api/calibration/check", json={**draft_of(saved(client)), "frames": [gone]})
    assert r.status_code == 404
    expired = {"kind": "grab", "grab": "nope", "index": 0}
    r = client.post("/api/calibration/check", json={**draft_of(saved(client)), "frames": [expired]})
    assert r.status_code == 404


# ---- rebasing -------------------------------------------------------------------------------


def test_rebasing_after_the_camera_moved(client: TestClient, data: Path) -> None:
    before = saved(client)
    r = client.post("/api/calibration/rebase", json={"frame": BUMPED})
    assert r.status_code == 200, r.text
    moved = r.json()
    assert (moved["shift"]["dx"], moved["shift"]["dy"]) == pytest.approx(BUMP, abs=0.5)
    assert moved["axis"]["top"][0] == pytest.approx(before["axis"]["top"][0] + BUMP[0], abs=0.5)
    save = {**draft_of(moved), "reference": BUMPED}
    put = client.put("/api/calibration", json=save, headers={"If-Match": before["etag"]})
    assert put.status_code == 200, put.text
    cal = Calibration.load(data / "calibration.yaml")
    assert cal.reference_image.name == "reference-20261013-040000.jpg"
    assert (data / cal.reference_image).read_bytes() == (
        data / "archive" / DAY / "040000Z-frame.jpg"
    ).read_bytes()
    assert cal.axis.top[1] == pytest.approx(before["axis"]["top"][1] + BUMP[1], abs=0.5)
    # The new reference reads the bumped frame without a shift, and still reads 98%.
    after = client.post(
        "/api/calibration/check", json={**draft_of(saved(client)), "frames": [BUMPED]}
    ).json()
    assert (after["reading"]["frames"][0]["dx"], after["reading"]["frames"][0]["dy"]) == (
        pytest.approx((0, 0), abs=0.5)
    )
    assert after["reading"]["level"] == pytest.approx(98, abs=1)


def test_a_rebase_with_an_axis_edit_is_refused(client: TestClient, data: Path) -> None:
    before = saved(client)
    moved = client.post("/api/calibration/rebase", json={"frame": BUMPED}).json()
    draft = draft_of(moved)
    draft["axis"]["top"][1] -= 20
    r = client.put(
        "/api/calibration",
        json={**draft, "reference": BUMPED},
        headers={"If-Match": before["etag"]},
    )
    assert r.status_code == 422
    assert "on its own" in r.json()["detail"]
    assert calibration_etag(data / "calibration.yaml") == before["etag"]


def test_rebasing_onto_the_reference_is_refused(client: TestClient) -> None:
    r = client.post("/api/calibration/rebase", json={"frame": {"kind": "reference"}})
    assert r.status_code == 422


# ---- the camera -----------------------------------------------------------------------------


def test_grabbing_from_the_camera(
    data: Path, monkeypatch: pytest.MonkeyPatch, sample_frame_paths: list[Path]
) -> None:
    assert client_for(data).post("/api/frames/grab", json={"frames": 2}).status_code == 409
    with serve_go2rtc([p.read_bytes() for p in sample_frame_paths[:3]]) as go2rtc:
        monkeypatch.setenv("OILTANK_CAMERA_URL", go2rtc.url)
        monkeypatch.setenv("OILTANK_CAMERA_RETRY_BACKOFF_S", "0")
        c = client_for(data)
        assert c.get("/api/frames").json()["camera"] is True
        r = c.post("/api/frames/grab", json={"frames": 2})
        assert r.status_code == 200, r.text
        grabbed = r.json()["frames"]
        assert len(grabbed) == 2
    image = c.get(grabbed[0]["url"])
    assert image.content[:2] == b"\xff\xd8"
    body = c.post(
        "/api/calibration/check", json={**draft_of(saved(c)), "frames": [grabbed[0]["frame"]]}
    ).json()
    assert body["reading"]["level"] == pytest.approx(98, abs=1)
    rebase = c.post("/api/calibration/rebase", json={"frame": grabbed[1]["frame"]}).json()
    assert (rebase["shift"]["dx"], rebase["shift"]["dy"]) == pytest.approx((0, 0), abs=0.5)


def test_the_check_shows_the_strip_the_reader_gets(client: TestClient) -> None:
    body = client.post("/api/calibration/check", json=draft_of(saved(client))).json()
    strip = body["strip"]
    assert strip["png"].startswith("data:image/png;base64,")
    png = base64.b64decode(strip["png"].split(",", 1)[1])
    image = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE)
    assert image is not None and image.shape == (256, 128)
    assert strip["rows"] == 256 and strip["t_first"] > strip["t_last"]
    scores = strip["scores"]
    assert len(scores) == 256
    best = max((s, i) for i, s in enumerate(scores) if s is not None)[1]
    assert best == pytest.approx(strip["disc_row"], abs=1)
    rows = {tick["label"]: tick["row"] for tick in strip["ticks"]}
    assert rows["F"] < rows["98%"] < rows["E"]
    assert rows["98%"] == pytest.approx(strip["disc_row"], abs=2)  # the owner's point is the disc


def test_the_strip_follows_the_draft(client: TestClient) -> None:
    before = client.post("/api/calibration/check", json=draft_of(saved(client))).json()["strip"]
    draft = draft_of(saved(client))
    draft["axis"]["top"][1] -= 60  # a longer axis: the disc is further down its strip
    after = client.post("/api/calibration/check", json=draft).json()["strip"]
    assert after["t_first"] == before["t_first"]  # rows still run from 1 + margin to -margin
    assert after["disc_row"] > before["disc_row"] + 10
