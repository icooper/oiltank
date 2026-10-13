"""The web UI's server side: pages, the status API and the request checks."""

import json
import os
import re
import socket
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Mount

from oiltank.calibration import Calibration
from oiltank.config import Settings, WebSettings
from oiltank.events import EventKind
from oiltank.model import LevelState
from oiltank.web.app import STATIC, create_app
from oiltank.web.control import CalibrationFile, Current, FileControl, ServiceControl
from oiltank.web.schemas import ServiceInfo
from oiltank.web.security import Policy, host_allowed, refusal
from oiltank.web.server import WebError, WebServer
from tests.fakes import Rig

T0 = datetime(2026, 10, 13, 3, 59, 20, tzinfo=UTC)
JSON = {"Content-Type": "application/json"}


@dataclass
class FakeControl:
    level: LevelState | None = None
    image: bytes | None = None
    here: bool = True
    read: list[bool] = field(default_factory=list)
    now: Current | None = None

    def current(self) -> Current | None:
        return self.now

    def calibration_file(self) -> CalibrationFile:
        return CalibrationFile(Path("missing.yaml"))

    def save_calibration(self, calibration: Calibration, *, if_match: str, read_now: bool) -> str:
        raise AssertionError("not expected")

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(here=self.here, alive=True, reading=False, mqtt_connected=True)

    def state(self) -> LevelState | None:
        return self.level

    def gauge_image(self) -> bytes | None:
        return self.image

    def archive_size(self) -> int | None:
        return 1234

    def read_now(self) -> bool:
        self.read.append(True)
        return self.here


def state(fraction: float = 0.98) -> LevelState:
    return LevelState(
        ts=T0,
        fraction=fraction,
        volume=fraction * 250,
        confidence=0.97,
        t=0.8,
        problem=None,
        usage_rate=2.0,
        days_left=110.0,
        last_fill=None,
        last_fill_volume=None,
    )


def client(
    control: FakeControl | ServiceControl | FileControl,
    settings: Settings | None = None,
    host: str = "oiltank.local",
) -> TestClient:
    app = create_app(control, settings or Settings(), read_only=False)
    return TestClient(app, base_url=f"http://{host}")


def asset_paths(html: str) -> list[str]:
    """The page's files (not its in-page links, like ``#view=history``)."""
    return [p for p in re.findall(r'(?:src|href)="([^"]+)"', html) if not p.startswith("#")]


# ---- pages ----------------------------------------------------------------------------------


def test_the_page_and_its_assets() -> None:
    c = client(FakeControl())
    page = c.get("/")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-cache"
    paths = asset_paths(page.text)
    assert paths and all(re.fullmatch(r"static/[0-9a-f]{12}/.+", p) for p in paths)
    for path in paths:
        asset = c.get(f"/{path}")
        assert asset.status_code == 200, path
        assert "immutable" in asset.headers["cache-control"]
    assert c.get("/static/000000000000/app.css").status_code == 404


def test_static_files_only_use_relative_urls() -> None:
    """So the UI works under a path prefix (a reverse proxy, or a Home Assistant add-on)."""
    for f in STATIC.rglob("*"):
        if f.suffix in (".html", ".css", ".js"):
            text = f.read_text()
            assert not re.search(r"""(?:src|href)=["']/""", text), f
            assert not re.search(r"""["'`]/(?:api|static)\b""", text), f
            assert not re.search(r"url\(\s*['\"]?/", text), f


def test_every_module_import_exists() -> None:
    for f in (STATIC / "js").rglob("*.js"):
        for target in re.findall(r"""from\s+["'](\.[^"']+)["']""", f.read_text()):
            assert (f.parent / target).resolve().is_file(), f"{f.name} imports {target}"


def test_served_under_a_path_prefix() -> None:
    app = create_app(FakeControl(), Settings(), read_only=False)
    outer = Starlette(routes=[Mount("/oiltank-ui", app=app)])
    c = TestClient(outer, base_url="http://ha.local")
    page = c.get("/oiltank-ui/")
    assert page.status_code == 200
    for path in asset_paths(page.text):  # relative to /oiltank-ui/
        assert c.get(f"/oiltank-ui/{path}").status_code == 200, path
    assert c.get("/oiltank-ui/api/status").status_code == 200


# ---- the API --------------------------------------------------------------------------------


def test_status() -> None:
    c = client(FakeControl(level=state()))
    r = c.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["state"]["level"] == 98.0
    assert body["state"]["volume"] == 245.0
    assert body["unit"] == "gal"
    assert body["service"]["here"] is True
    assert body["archive"] == {"enabled": True, "size": 1234, "max_size": 2000 * 1024 * 1024}
    again = c.get("/api/status", headers={"If-None-Match": r.headers["etag"]})
    assert again.status_code == 304


def test_status_before_any_reading() -> None:
    assert client(FakeControl()).get("/api/status").json()["state"] is None


def test_gauge_image() -> None:
    assert client(FakeControl()).get("/api/gauge.jpg").status_code == 404
    r = client(FakeControl(image=b"\xff\xd8jpeg")).get("/api/gauge.jpg")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content == b"\xff\xd8jpeg"


def test_read_now() -> None:
    control = FakeControl()
    r = client(control).post("/api/read-now", json={})
    assert r.status_code == 202
    assert control.read == [True]
    standalone = client(FakeControl(here=False)).post("/api/read-now", json={})
    assert standalone.status_code == 409


# ---- request checks -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("192.168.1.20:8780", True),
        ("100.101.102.103", True),  # a Tailscale address
        ("[::1]:8780", True),
        ("localhost:8780", True),
        ("oiltank:8780", True),
        ("oiltank.local", True),
        ("nas.lan:8780", True),
        ("oiltank.home.arpa", True),
        ("oiltank.example.com", False),
        ("evil.example", False),
        ("", False),
    ],
)
def test_private_host_names(host: str, allowed: bool) -> None:
    assert host_allowed(host, Policy()) is allowed


def test_the_web_url_host_is_trusted() -> None:
    policy = Policy.from_settings(WebSettings(url="https://oiltank.example.com/"))
    assert host_allowed("oiltank.example.com", policy)
    assert not host_allowed("other.example.com", policy)


@pytest.mark.parametrize(
    ("method", "headers", "status"),
    [
        ("GET", {"host": "evil.example"}, 400),
        ("POST", {"host": "oiltank.lan"}, 415),  # not JSON
        ("POST", {"host": "oiltank.lan", "content-type": "text/plain"}, 415),
        ("POST", {"host": "oiltank.lan", "content-type": "application/json"}, None),  # curl
        (
            "POST",
            {
                "host": "oiltank.lan",
                "content-type": "application/json; charset=utf-8",
                "origin": "http://oiltank.lan",
            },
            None,
        ),
        (
            "POST",
            {"host": "oiltank.lan", "content-type": "application/json", "origin": "https://x.io"},
            403,
        ),
        (
            # TLS ends at a reverse proxy, which passes the Host header on
            "PUT",
            {
                "host": "oiltank.example.com",
                "content-type": "application/json",
                "origin": "https://oiltank.example.com",
            },
            None,
        ),
    ],
)
def test_refusals(method: str, headers: dict[str, str], status: int | None) -> None:
    policy = Policy.from_settings(WebSettings(url="https://oiltank.example.com/"))
    result = refusal(method, headers, policy)
    assert (None if result is None else result[0]) == status


def test_requests_are_checked_end_to_end() -> None:
    c = client(FakeControl())
    assert client(FakeControl(), host="evil.example").get("/").status_code == 400
    assert c.post("/api/read-now", content="{}").status_code == 415
    cross = c.post("/api/read-now", json={}, headers={"Origin": "https://evil.example"})
    assert cross.status_code == 403
    same = c.post("/api/read-now", json={}, headers={"Origin": "http://oiltank.local"})
    assert same.status_code == 202


def test_security_headers_and_framing(monkeypatch: pytest.MonkeyPatch) -> None:
    csp = client(FakeControl()).get("/").headers["content-security-policy"]
    assert "frame-ancestors 'self'" in csp
    assert "default-src 'self'" in csp
    monkeypatch.setenv("OILTANK_WEB_EMBED_ORIGINS", "https://ha.example.com https://ha.lan:8123")
    page = client(FakeControl(), Settings()).get("/")
    assert (
        "frame-ancestors 'self' https://ha.example.com https://ha.lan:8123"
        in (page.headers["content-security-policy"])
    )
    assert page.headers["x-content-type-options"] == "nosniff"
    assert "x-frame-options" not in page.headers  # it can't allow a list of sites


# ---- controls -------------------------------------------------------------------------------


def test_service_control_reads_the_snapshot(rig: Rig, settings: Settings) -> None:
    control = ServiceControl(rig.service, rig.events)
    c = client(control, settings)
    assert c.get("/api/status").json()["state"] is None
    rig.service.cycle()
    body = c.get("/api/status").json()
    assert body["state"]["level"] == pytest.approx(98, abs=1)
    assert body["service"]["here"] and body["service"]["mqtt_connected"]
    assert c.get("/api/gauge.jpg").content == rig.publisher.images[-1]
    assert c.post("/api/read-now", json={}).status_code == 202
    assert rig.events.get_nowait().kind is EventKind.READ_NOW


def test_file_control_reads_the_heartbeat(tmp_path: Path) -> None:
    control = FileControl(tmp_path, tmp_path / "calibration.yaml")
    assert control.service_info() == ServiceInfo(here=False, alive=False)
    beat = tmp_path / "heartbeat"
    beat.write_text(json.dumps({"ts": "x", "last_reading": T0.isoformat(), "mqtt_connected": True}))
    info = control.service_info()
    assert info.alive and info.mqtt_connected and info.last_reading == T0
    old = time.time() - 600
    os.utime(beat, (old, old))
    assert not control.service_info().alive
    assert control.read_now() is False


# ---- the server thread ----------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_web_server_starts_and_stops() -> None:
    app: FastAPI = create_app(FakeControl(level=state()), Settings(), read_only=False)
    port = free_port()
    server = WebServer(app, "127.0.0.1", port)
    server.start()
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/api/status", timeout=5)
        assert r.json()["state"]["level"] == 98.0
    finally:
        server.stop()
    with pytest.raises(httpx.ConnectError):
        httpx.get(f"http://127.0.0.1:{port}/api/status", timeout=2)


def test_web_server_reports_a_taken_port() -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        app = create_app(FakeControl(), Settings(), read_only=False)
        with pytest.raises(WebError, match=str(port)):
            WebServer(app, "127.0.0.1", port).start()


def test_history_inside_the_service(rig: Rig, settings: Settings) -> None:
    """Served by `oiltank run`: the readings come from the history the service is writing, with
    the service's own calibration."""
    c = client(ServiceControl(rig.service, rig.events), settings)
    rig.service.cycle()
    since = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    r = c.get("/api/readings", params={"start": since})
    assert r.status_code == 200
    [level] = r.json()["readings"]["level"]
    assert level == pytest.approx(98, abs=1)
    current = ServiceControl(rig.service, rig.events).current()
    assert current.epoch == rig.service.snapshot.epoch
