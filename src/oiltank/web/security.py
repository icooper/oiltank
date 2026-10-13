"""Request checks for a web UI with no login, on a private network.

- **Host.** Only names a browser uses on a private network are accepted: IP addresses,
  ``localhost``, single-label names, local suffixes (``.local``, ``.lan``, ``.home.arpa``,
  ``.internal``) and the host of ``OILTANK_WEB_URL``. Otherwise a public site could reach the UI by
  pointing its own name at a private address (DNS rebinding).
- **Writes** (anything but GET, HEAD and OPTIONS) must be JSON. A cross-site page can't send JSON
  without a CORS preflight, which this app never grants. If the browser says where a write came
  from (``Origin``), that must be this site.
- **Responses** carry a Content-Security-Policy: everything from this site only, and only this
  site and ``OILTANK_WEB_EMBED_ORIGINS`` may show it in a frame.
"""

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from starlette.datastructures import MutableHeaders
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from oiltank.config import WebSettings

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
LOCAL_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal")


@dataclass(frozen=True, slots=True)
class Policy:
    trusted_hosts: frozenset[str] = frozenset()
    """Host names accepted besides private ones: ``OILTANK_WEB_URL``'s."""
    trusted_origins: frozenset[str] = frozenset()
    """Origins writes may come from besides the request's own host: ``OILTANK_WEB_URL``'s."""
    frame_ancestors: tuple[str, ...] = ()

    @classmethod
    def from_settings(cls, web: WebSettings) -> Policy:
        hosts, origins = set(), set()
        if web.url is not None:
            parts = urlsplit(web.url)
            if parts.hostname:
                hosts.add(parts.hostname.lower())
                origins.add(f"{parts.scheme}://{parts.netloc}".lower())
        return cls(
            frozenset(hosts),
            frozenset(origins),
            tuple(o.rstrip("/") for o in web.embed_origins.split()),
        )

    @property
    def headers(self) -> dict[str, str]:
        ancestors = " ".join(("'self'", *self.frame_ancestors))
        return {
            "Content-Security-Policy": (
                "default-src 'self'; img-src 'self' blob: data:; base-uri 'none'; "
                f"form-action 'self'; frame-ancestors {ancestors}"
            ),
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "same-origin",
        }


def host_name(host: str) -> str:
    """The name in a Host header, without the port (and IPv6 brackets)."""
    host = host.strip().lower()
    if host.startswith("["):
        return host[1 : host.find("]")] if "]" in host else host
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def host_allowed(host: str, policy: Policy) -> bool:
    name = host_name(host)
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    return (
        name == "localhost"
        or "." not in name
        or name.endswith(LOCAL_SUFFIXES)
        or name in policy.trusted_hosts
    )


def refusal(method: str, headers: Mapping[str, str], policy: Policy) -> tuple[int, str] | None:
    """Why a request is refused, as ``(status, reason)``, or None if it may go ahead.

    ``headers`` has lower-case names.
    """
    host = headers.get("host", "")
    if not host_allowed(host, policy):
        return 400, (
            f"{host_name(host) or 'that host name'} isn't a private-network name; "
            "if that's how you reach the web UI, set OILTANK_WEB_URL to it"
        )
    if method.upper() in SAFE_METHODS:
        return None
    content_type = headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        return 415, "changes must be sent as JSON"
    origin = headers.get("origin")
    if origin is not None:
        origin = origin.lower()
        own = {f"http://{host.lower()}", f"https://{host.lower()}"}
        if origin not in own and origin not in policy.trusted_origins:
            return 403, "cross-site requests aren't accepted"
    return None


class SecurityMiddleware:
    """Applies :func:`refusal` to every request and :attr:`Policy.headers` to every response."""

    def __init__(self, app: ASGIApp, policy: Policy) -> None:
        self._app = app
        self._policy = policy

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers: dict[str, str] = {}
        for key, value in scope["headers"]:
            headers.setdefault(key.decode("latin-1").lower(), value.decode("latin-1"))
        refused = refusal(scope["method"], headers, self._policy)
        if refused is not None:
            status, reason = refused
            await PlainTextResponse(reason, status, self._policy.headers)(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                for key, value in self._policy.headers.items():
                    response_headers.setdefault(key, value)
            await send(message)

        await self._app(scope, receive, send_with_headers)
