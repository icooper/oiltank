"""Response helpers shared by the web UI's routes."""

import hashlib
import json
from typing import Any

from fastapi import Request, Response

IMMUTABLE = "public, max-age=31536000, immutable"


def json_response(request: Request, payload: Any) -> Response:
    """``payload`` as JSON, with an ETag: an unchanged payload is a bodiless 304."""
    body = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
    etag = f'"{hashlib.sha256(body).hexdigest()[:16]}"'
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(body, media_type="application/json", headers=headers)
