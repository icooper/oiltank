"""Test doubles shared by unit and integration tests."""

import base64
import queue
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from oiltank.events import Event, EventKind
from oiltank.history import History
from oiltank.model import Frame, LevelState
from oiltank.publish.discovery import Topics
from oiltank.publish.mqtt import MqttError
from oiltank.service import Service
from oiltank.sources.base import CameraError


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


@contextmanager
def serve_go2rtc(frames: list[bytes]) -> Iterator[FakeGo2rtc]:
    """A fake go2rtc on localhost, serving ``frames`` in turn; ``.url`` is its frame URL."""
    fake = FakeGo2rtc(frames=frames)

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
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()


@dataclass
class ScriptedCamera:
    """Hands out the sample frames (distinct each time), or raises when told to."""

    frames: list[Frame]
    fail: bool = False
    calls: int = 0
    delay: float = 0.0

    def grab(self, n: int, spacing_s: float) -> list[Frame]:
        self.calls += 1
        threading.Event().wait(self.delay)
        if self.fail:
            raise CameraError("go2rtc:1984 didn't answer within 15 s")
        start = (self.calls - 1) * n
        return [self.frames[(start + k) % len(self.frames)] for k in range(n)]


@dataclass
class RecordingPublisher:
    topics: Topics
    connected: bool = True
    broken: bool = False
    states: list[LevelState] = field(default_factory=list)
    images: list[bytes] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    def start(self, timeout_s: float = 15.0, *, keep_trying: bool = False) -> bool:
        self.calls.append("start")
        return True

    def stop(self, *, offline: bool = True) -> None:
        self.calls.append("stop")

    def publish_discovery(self) -> None:
        self.calls.append("discovery")

    def publish_availability(self, online: bool) -> None:
        self.calls.append(f"availability:{online}")

    def publish_state(self, state: LevelState) -> None:
        if self.broken:
            raise MqttError("publishing to oiltank/oil_tank/state failed: not connected")
        self.states.append(state)

    def publish_image(self, jpeg: bytes) -> None:
        self.images.append(jpeg)


@dataclass
class Rig:
    service: Service
    publisher: RecordingPublisher
    events: queue.Queue[Event]
    history: History


@contextmanager
def running(rig: Rig) -> Iterator[threading.Thread]:
    """The service loop, on its own thread."""
    stop = threading.Event()
    thread = threading.Thread(target=rig.service.run, args=(stop,), daemon=True)
    thread.start()
    try:
        yield thread
    finally:
        stop.set()
        rig.events.put(Event(EventKind.STOP))
        thread.join(timeout=10)
    assert not thread.is_alive()
