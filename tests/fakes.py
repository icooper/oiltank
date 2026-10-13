"""Test doubles shared by unit and integration tests."""

import base64
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


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
