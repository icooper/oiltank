"""HttpSource against a mocked go2rtc (httpx.MockTransport): no network, no waiting."""

import base64
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from oiltank.config import CameraSettings
from oiltank.sources.base import CameraConfigError, CameraError
from oiltank.sources.http import HttpSource

URL = "http://go2rtc:1984/api/frame.jpeg?src=oil_tank"
Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture(scope="module")
def jpegs(sample_frame_paths: list[Path]) -> list[bytes]:
    return [p.read_bytes() for p in sample_frame_paths[:4]]


def jpeg(data: bytes) -> httpx.Response:
    return httpx.Response(200, content=data, headers={"content-type": "image/jpeg"})


def scripted(*responses: httpx.Response | Exception) -> tuple[Handler, list[httpx.Request]]:
    """A handler that plays ``responses`` in order (the last one repeats) and logs requests."""
    seen: list[httpx.Request] = []
    queue: Iterator[httpx.Response | Exception] = iter(responses)
    last: list[httpx.Response | Exception] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = next(queue, None)
        if item is None:
            item = last[0]
        last[:] = [item]
        if isinstance(item, Exception):
            raise item
        return item

    return handle, seen


def source(handler: Handler, sleeps: list[float] | None = None, **kwargs: object) -> HttpSource:
    log = sleeps if sleeps is not None else []
    return HttpSource(
        URL,
        transport=httpx.MockTransport(handler),
        sleep=log.append,
        retry_backoff_s=1.0,
        **kwargs,  # type: ignore[arg-type]
    )


def test_burst_of_distinct_frames(jpegs: list[bytes]) -> None:
    handler, seen = scripted(*(jpeg(d) for d in jpegs[:3]))
    sleeps: list[float] = []
    frames = source(handler, sleeps).grab(3, 2.0)
    assert [f.data for f in frames] == jpegs[:3]
    assert all(f.size == (3840, 2160) and f.source == URL for f in frames)
    assert sleeps == [2.0, 2.0]
    assert len(seen) == 3
    assert "authorization" not in seen[0].headers
    assert seen[0].headers["user-agent"].startswith("oiltank/")


def test_duplicates_are_dropped_and_refetched(jpegs: list[bytes]) -> None:
    a, b, c = (jpeg(d) for d in jpegs[:3])
    handler, seen = scripted(a, a, b, b, c)
    frames = source(handler).grab(3, 0.0)
    assert [f.data for f in frames] == jpegs[:3]
    assert len(seen) == 5


def test_a_frozen_stream_gives_one_frame(jpegs: list[bytes]) -> None:
    handler, seen = scripted(jpeg(jpegs[0]))
    frames = source(handler).grab(3, 0.0)
    assert len(frames) == 1
    assert len(seen) == 6  # gave up after 2 n requests


def test_server_errors_are_retried_with_backoff(jpegs: list[bytes]) -> None:
    handler, seen = scripted(
        httpx.Response(500, text="streams: dial tcp: i/o timeout"),
        httpx.Response(503),
        jpeg(jpegs[0]),
    )
    sleeps: list[float] = []
    frame = source(handler, sleeps).fetch()
    assert frame.data == jpegs[0]
    assert sleeps == [1.0, 2.0]
    assert len(seen) == 3


def test_timeouts_exhaust_retries() -> None:
    handler, seen = scripted(httpx.ReadTimeout("slow"))
    with pytest.raises(CameraError, match="didn't answer within 15 s"):
        source(handler, retries=2).fetch()
    assert len(seen) == 3


def test_unreachable_host() -> None:
    handler, _ = scripted(httpx.ConnectError("Name or service not known"))
    with pytest.raises(CameraError, match="cannot reach go2rtc:1984"):
        source(handler, retries=0).grab(5, 0.0)


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(401), "OILTANK_CAMERA_USERNAME"),
        (httpx.Response(404), "stream called 'oil_tank'"),
        (httpx.Response(200, text="<html>", headers={"content-type": "text/html"}), "text/html"),
        (httpx.Response(302, headers={"location": "/login"}), "HTTP 302"),
    ],
)
def test_configuration_problems_are_not_retried(response: httpx.Response, message: str) -> None:
    handler, seen = scripted(response)
    with pytest.raises(CameraConfigError, match=message):
        source(handler).grab(3, 0.0)
    assert len(seen) == 1


def test_corrupt_image_is_retried(jpegs: list[bytes]) -> None:
    handler, seen = scripted(jpeg(jpegs[0][:5000]), jpeg(jpegs[0]))
    assert source(handler).fetch().data == jpegs[0]
    assert len(seen) == 2


def test_burst_keeps_what_it_got_when_the_camera_drops(jpegs: list[bytes]) -> None:
    handler, _ = scripted(jpeg(jpegs[0]), jpeg(jpegs[1]), httpx.ConnectError("gone"))
    frames = source(handler, retries=1).grab(5, 0.0)
    assert [f.data for f in frames] == jpegs[:2]


def test_basic_auth(jpegs: list[bytes]) -> None:
    handler, seen = scripted(jpeg(jpegs[0]))
    source(handler, username="admin", password="s3cret").fetch()
    expected = "Basic " + base64.b64encode(b"admin:s3cret").decode()
    assert seen[0].headers["authorization"] == expected


def test_from_settings() -> None:
    camera = CameraSettings(url=URL, username="u", password="p", timeout_s=3, retries=1)
    with HttpSource.from_settings(camera) as src:
        assert src.url == URL
        assert src.host == "go2rtc:1984"
    with HttpSource.from_settings(camera, url="http://other:1984/api/frame.jpeg?src=x") as src:
        assert src.host == "other:1984"
    with pytest.raises(CameraConfigError, match="OILTANK_CAMERA_URL"):
        HttpSource.from_settings(CameraSettings())
