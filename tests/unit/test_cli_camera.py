"""CLI commands that fetch frames, against a fake go2rtc on localhost (real sockets)."""

import base64
import shutil
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from typer.testing import CliRunner, Result

from oiltank.calibration import Calibration
from oiltank.cli import app
from tests.conftest import SAMPLE_CALIBRATION

runner = CliRunner()


@dataclass
class FakeGo2rtc:
    """Serves the sample frames in turn at /api/frame.jpeg?src=oil_tank."""

    frames: list[bytes]
    auth: tuple[str, str] | None = None
    fail_first: int = 0
    delay_s: float = 0.0
    requests: int = 0
    url: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def respond(self, handler: BaseHTTPRequestHandler) -> None:
        with self._lock:
            self.requests += 1
            n = self.requests
        time.sleep(self.delay_s)
        parts = urlsplit(handler.path)
        if self.auth is not None:
            token = base64.b64encode(":".join(self.auth).encode()).decode()
            if handler.headers.get("Authorization") != f"Basic {token}":
                handler.send_response(401)
                handler.end_headers()
                return
        if parts.path != "/api/frame.jpeg" or parse_qs(parts.query).get("src") != ["oil_tank"]:
            handler.send_response(404)
            handler.end_headers()
            return
        if n <= self.fail_first:
            body = b"streams: dial tcp: i/o timeout"
            handler.send_response(500)
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
            return
        data = self.frames[(n - self.fail_first - 1) % len(self.frames)]
        handler.send_response(200)
        handler.send_header("Content-Type", "image/jpeg")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)


@pytest.fixture
def go2rtc(sample_frame_paths: list[Path], monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGo2rtc]:
    fake = FakeGo2rtc(frames=[p.read_bytes() for p in sample_frame_paths[:4]])

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            fake.respond(self)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    fake.url = f"http://{host!s}:{port}/api/frame.jpeg?src=oil_tank"
    monkeypatch.setenv("OILTANK_CAMERA_URL", fake.url)
    monkeypatch.setenv("OILTANK_CAMERA_BURST_SPACING_S", "0")
    monkeypatch.setenv("OILTANK_CAMERA_RETRY_BACKOFF_S", "0")
    yield fake
    server.shutdown()
    server.server_close()


def _run(*args: str) -> Result:
    return runner.invoke(app, list(args))


def test_read_from_the_camera(go2rtc: FakeGo2rtc) -> None:
    result = _run("read", "-c", str(SAMPLE_CALIBRATION), "--frames", "3")
    assert result.exit_code == 0, result.output
    assert f"fetching 3 frame(s) from {urlsplit(go2rtc.url).netloc}" in result.stderr
    assert result.stdout.startswith("level 9")
    assert "3 frame(s)" in result.stdout
    assert go2rtc.requests == 3


def test_read_with_an_explicit_url(go2rtc: FakeGo2rtc, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OILTANK_CAMERA_URL")
    result = _run("read", "-c", str(SAMPLE_CALIBRATION), "--url", go2rtc.url, "--frames", "2")
    assert result.exit_code == 0, result.output
    assert "2 frame(s)" in result.stdout


def test_read_without_a_camera_or_image() -> None:
    result = _run("read", "-c", str(SAMPLE_CALIBRATION))
    assert result.exit_code == 2
    assert "OILTANK_CAMERA_URL" in result.output


def test_camera_options_need_the_camera(sample_frame_paths: list[Path]) -> None:
    image = str(sample_frame_paths[0])
    result = _run("read", "-c", str(SAMPLE_CALIBRATION), "--image", image, "--frames", "2")
    assert result.exit_code == 2


def test_wrong_stream_name(go2rtc: FakeGo2rtc) -> None:
    url = go2rtc.url.replace("oil_tank", "oil_tanks")
    result = _run("read", "-c", str(SAMPLE_CALIBRATION), "--url", url)
    assert result.exit_code == 2
    assert "stream called 'oil_tanks'" in result.output


def test_basic_auth(go2rtc: FakeGo2rtc, monkeypatch: pytest.MonkeyPatch) -> None:
    go2rtc.auth = ("admin", "s3cret")
    refused = _run("read", "-c", str(SAMPLE_CALIBRATION), "--frames", "1")
    assert refused.exit_code == 2
    assert "OILTANK_CAMERA_USERNAME" in refused.output
    monkeypatch.setenv("OILTANK_CAMERA_USERNAME", "admin")
    monkeypatch.setenv("OILTANK_CAMERA_PASSWORD", "s3cret")
    allowed = _run("read", "-c", str(SAMPLE_CALIBRATION), "--frames", "1")
    assert allowed.exit_code == 0, allowed.output


def test_transient_errors_are_retried(go2rtc: FakeGo2rtc) -> None:
    go2rtc.fail_first = 2
    result = _run("read", "-c", str(SAMPLE_CALIBRATION), "--frames", "2")
    assert result.exit_code == 0, result.output
    assert "HTTP 500" in result.stderr  # logged as retries
    assert go2rtc.requests == 4


def test_slow_camera_times_out(go2rtc: FakeGo2rtc, monkeypatch: pytest.MonkeyPatch) -> None:
    go2rtc.delay_s = 1.0
    monkeypatch.setenv("OILTANK_CAMERA_TIMEOUT_S", "0.2")
    monkeypatch.setenv("OILTANK_CAMERA_RETRIES", "0")
    result = _run("read", "-c", str(SAMPLE_CALIBRATION))
    assert result.exit_code == 1
    assert "camera unreachable" in result.output
    assert "within 0.2 s" in result.output


def test_unknown_setting_is_an_error(go2rtc: FakeGo2rtc, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OILTANK__CAMERA__URL", go2rtc.url)
    result = _run("read", "-c", str(SAMPLE_CALIBRATION))
    assert result.exit_code == 2
    assert "single underscores" in result.output


def test_grab(go2rtc: FakeGo2rtc, tmp_path: Path) -> None:
    one = tmp_path / "frame.jpg"
    assert _run("grab", "--out", str(one)).exit_code == 0
    assert one.read_bytes() == go2rtc.frames[0]
    many = tmp_path / "burst"
    many.mkdir()
    result = _run("grab", "--out", str(many), "--frames", "3")
    assert result.exit_code == 0, result.output
    assert sorted(p.read_bytes() for p in many.iterdir()) == sorted(go2rtc.frames[1:4])
    named = _run("grab", "--out", str(tmp_path / "n.jpg"), "--frames", "2")
    assert named.exit_code == 0
    assert (tmp_path / "n-1.jpg").exists() and (tmp_path / "n-2.jpg").exists()


def test_calibrate_from_the_camera(go2rtc: FakeGo2rtc, tmp_path: Path) -> None:
    path = tmp_path / "data" / "calibration.yaml"
    init = ["calibrate", "init", "-c", str(path), "--camera"]
    init += ["--top", "2166", "1020", "--bottom", "2166", "1172"]

    dry = _run(*init, "--dry-run")
    assert dry.exit_code == 0, dry.output
    assert not (tmp_path / "data").exists()

    result = _run(*init)
    assert result.exit_code == 0, result.output
    cal = Calibration.load(path)
    assert cal.reference_image.name.startswith("reference-")
    assert cal.reference_path.read_bytes() in go2rtc.frames  # stored exactly as received

    point = _run("calibrate", "point", "-c", str(path), "--camera", "--fraction", "0.98")
    assert point.exit_code == 0, point.output
    assert "reading      98.0%" in point.stdout

    rebase = _run("calibrate", "rebase", "-c", str(path), "--camera")
    assert rebase.exit_code == 0, rebase.output
    assert "the camera moved" in rebase.stdout


def test_default_calibration_comes_from_the_data_dir(
    go2rtc: FakeGo2rtc, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    shutil.copyfile(SAMPLE_CALIBRATION, data / "calibration.yaml")
    shutil.copyfile(SAMPLE_CALIBRATION.parent / "frame0.jpg", data / "frame0.jpg")
    monkeypatch.setenv("OILTANK_DATA_DIR", str(data))
    result = _run("read", "--frames", "1")
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("level 9")
    shown = _run("calibrate", "show")
    assert shown.exit_code == 0, shown.output
    assert f"calibration  {data / 'calibration.yaml'}" in shown.stdout
