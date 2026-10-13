"""Frames from an HTTP endpoint that returns one JPEG per request, such as go2rtc's frame API.

go2rtc serves ``/api/frame.jpeg?src=<stream>`` from the camera's live stream, at its native
resolution. A request made sooner than the camera's keyframe interval can return the same frame
again, so bursts drop exact duplicates.
"""

import hashlib
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from types import TracebackType
from typing import Self
from urllib.parse import parse_qs, urlsplit

import httpx

from oiltank import __version__
from oiltank.config import CameraSettings
from oiltank.model import Frame
from oiltank.sources.base import CameraConfigError, CameraError
from oiltank.sources.files import FrameDecodeError, decode_gray

log = logging.getLogger(__name__)


class HttpSource:
    """Fetches frames from ``url``, retrying transient failures with exponential backoff.

    ``transport``, ``sleep`` and ``clock`` exist for tests.
    """

    def __init__(
        self,
        url: str,
        *,
        username: str | None = None,
        password: str | None = None,
        timeout_s: float = 15.0,
        retries: int = 3,
        retry_backoff_s: float = 2.0,
        verify_tls: bool = True,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.url = url
        parts = urlsplit(url)
        self.host = parts.netloc
        self._stream = parse_qs(parts.query).get("src", [None])[0]
        self._timeout_s = timeout_s
        self._retries = retries
        self._backoff_s = retry_backoff_s
        self._sleep = sleep
        self._clock = clock
        self._client = httpx.Client(
            auth=httpx.BasicAuth(username, password or "") if username else None,
            timeout=timeout_s,
            verify=verify_tls,
            transport=transport,
            headers={"User-Agent": f"oiltank/{__version__}"},
        )

    @classmethod
    def from_settings(cls, camera: CameraSettings, *, url: str | None = None) -> Self:
        """A source for the configured camera (``url`` overrides the configured URL)."""
        target = url or camera.url
        if not target:
            raise CameraConfigError("no camera URL: set OILTANK_CAMERA_URL (see .env.example)")
        return cls(
            target,
            username=camera.username,
            password=camera.password.get_secret_value() if camera.password else None,
            timeout_s=camera.timeout_s,
            retries=camera.retries,
            retry_backoff_s=camera.retry_backoff_s,
            verify_tls=camera.verify_tls,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # ---- fetching ------------------------------------------------------------------------------

    def _attempt(self) -> Frame:
        """One request. Raises CameraConfigError (don't retry) or CameraError (may retry)."""
        try:
            response = self._client.get(self.url)
        except httpx.TimeoutException as e:
            raise CameraError(f"{self.host} didn't answer within {self._timeout_s:g} s") from e
        except httpx.TransportError as e:
            raise CameraError(f"cannot reach {self.host}: {e}") from e

        status = response.status_code
        if status in (401, 403):
            raise CameraConfigError(
                f"{self.host} refused the request (HTTP {status}); set OILTANK_CAMERA_USERNAME "
                "and OILTANK_CAMERA_PASSWORD to match go2rtc's api credentials"
            )
        if status == 404:
            where = f" (is there a go2rtc stream called {self._stream!r}?)" if self._stream else ""
            raise CameraConfigError(f"{self.url} not found (HTTP 404){where}")
        if status >= 500 or status == 429:
            detail = response.text.strip().splitlines()[0][:200] if response.text.strip() else ""
            raise CameraError(
                f"{self.host} answered HTTP {status}" + (f": {detail}" if detail else "")
            )
        if status != 200:
            raise CameraConfigError(f"unexpected answer from {self.host}: HTTP {status}")

        content_type = response.headers.get("content-type", "")
        if not content_type.startswith("image/"):
            raise CameraConfigError(
                f"expected an image from {self.url}, got {content_type or 'no content type'}; "
                "is this go2rtc's /api/frame.jpeg endpoint?"
            )
        try:
            return decode_gray(response.content, source=self.url, ts=self._clock())
        except FrameDecodeError as e:  # truncated or corrupt: worth another try
            raise CameraError(f"{self.host} sent an image that couldn't be decoded") from e

    def fetch(self) -> Frame:
        """One frame, retrying transient failures ``retries`` times with exponential backoff."""
        for attempt in range(self._retries + 1):
            if attempt:
                delay = self._backoff_s * 2 ** (attempt - 1)
                log.warning(
                    "retrying in %.1f s (attempt %d of %d)", delay, attempt + 1, self._retries + 1
                )
                self._sleep(delay)
            try:
                return self._attempt()
            except CameraConfigError:
                raise
            except CameraError as e:
                log.warning("%s", e)
                last = e
        raise last

    def grab(self, n: int, spacing_s: float) -> list[Frame]:
        """Up to ``n`` distinct frames, ``spacing_s`` apart.

        Exact duplicates (go2rtc re-serving a frame) are dropped and refetched, up to ``2 n``
        requests in all. If a fetch still fails after its retries, the burst stops there and
        returns what it has. Raises :class:`CameraError` if that's nothing.
        """
        frames: list[Frame] = []
        seen: set[bytes] = set()
        for request in range(2 * n):
            if len(frames) == n:
                break
            if request:
                self._sleep(spacing_s)
            try:
                frame = self.fetch()
            except CameraConfigError:
                raise
            except CameraError:
                if not frames:
                    raise
                log.warning("burst cut short: %d of %d frames", len(frames), n)
                break
            assert frame.data is not None
            digest = hashlib.sha256(frame.data).digest()
            if digest in seen:
                log.info("go2rtc returned the same frame again; fetching another")
                continue
            seen.add(digest)
            frames.append(frame)
            log.debug(
                "frame %d of %d: %d bytes, %dx%d", len(frames), n, len(frame.data), *frame.size
            )
        if len(frames) < n:
            log.warning("got %d distinct frames of the %d asked for", len(frames), n)
        return frames
