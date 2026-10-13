"""Serving the web UI from a thread of ``oiltank run``.

uvicorn only installs signal handlers on the main thread, so the service's own SIGTERM handling
stays in charge, and :meth:`WebServer.stop` ends the server when the service stops.
"""

import contextlib
import threading
import time

import uvicorn
from fastapi import FastAPI


class WebError(Exception):
    """The web UI couldn't be served (e.g. the port is taken)."""


class WebServer:
    def __init__(self, app: FastAPI, host: str, port: int) -> None:
        self.host, self.port = host, port
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_config=None,  # log through the service's logging setup
            access_log=False,
            server_header=False,
            lifespan="off",
            timeout_graceful_shutdown=5,
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._serve, name="web", daemon=True)

    def _serve(self) -> None:
        with contextlib.suppress(SystemExit):  # how uvicorn gives up at startup; it logged why
            self._server.run()

    def start(self, timeout: float = 10.0) -> None:
        """Start serving; raises WebError if it doesn't come up (uvicorn has logged why)."""
        self._thread.start()
        deadline = time.monotonic() + timeout
        while not self._server.started:
            if not self._thread.is_alive():  # e.g. it couldn't bind the port
                raise WebError(f"couldn't serve the web UI on {self.host}:{self.port}")
            if time.monotonic() > deadline:
                self.stop()
                raise WebError(f"the web UI didn't start within {timeout:g} s")
            time.sleep(0.02)

    def stop(self) -> None:
        if self._thread.ident is None:  # never started
            return
        self._server.should_exit = True
        self._thread.join(timeout=7)
        if self._thread.is_alive():
            self._server.force_exit = True
            self._thread.join(timeout=2)
