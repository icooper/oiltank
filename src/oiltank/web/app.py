"""The web UI's routes.

Every URL the pages use is relative (``api/status``, ``static/<hash>/app.css``), so the UI also
works under a path prefix, behind a reverse proxy. Static files are served under a hash of their
contents and cached for good; ``index.html`` is never cached, so a new version shows up at once.
"""

import hashlib
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from oiltank.archive import ArchiveReader
from oiltank.config import Settings
from oiltank.publish.discovery import state_payload
from oiltank.sources.http import HttpSource
from oiltank.web.calibration_api import Workspace, calibration_router
from oiltank.web.control import Control
from oiltank.web.frames import FrameStore
from oiltank.web.geometry_api import geometry_router
from oiltank.web.history_api import history_router
from oiltank.web.responses import IMMUTABLE, json_response
from oiltank.web.schemas import ArchiveInfo, Status
from oiltank.web.security import Policy, SecurityMiddleware

STATIC = Path(__file__).parent / "static"


def static_hash(root: Path = STATIC) -> str:
    """A short hash of every static file's name and contents."""
    h = hashlib.sha256()
    for f in sorted(p for p in root.rglob("*") if p.is_file()):
        h.update(f.relative_to(root).as_posix().encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:12]


class _ImmutableFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = IMMUTABLE
        return response


def create_app(control: Control, settings: Settings, *, read_only: bool) -> FastAPI:
    app = FastAPI(title="oiltank", docs_url=None, redoc_url=None, openapi_url="/api/openapi.json")
    app.add_middleware(SecurityMiddleware, policy=Policy.from_settings(settings.web))
    assets = f"static/{static_hash()}"
    app.mount(f"/{assets}", _ImmutableFiles(directory=STATIC), name="static")
    index = (STATIC / "index.html").read_text().replace("{{static}}", assets)
    archive = settings.archive

    @app.get("/", include_in_schema=False)
    def page() -> HTMLResponse:
        return HTMLResponse(index, headers={"Cache-Control": "no-cache"})

    @app.get("/api/status", response_model=Status)
    def status(request: Request) -> Response:
        state = control.state()
        body = Status(
            read_only=read_only,
            unit=settings.tank.unit,
            capacity=settings.tank.capacity,
            state=None if state is None else state_payload(state),
            service=control.service_info(),
            archive=ArchiveInfo(
                enabled=archive.enabled,
                size=control.archive_size(),
                max_size=int(archive.max_mb * 1024 * 1024) if archive.enabled else None,
            ),
        )
        return json_response(request, body.model_dump(mode="json"))

    @app.get("/api/gauge.jpg", include_in_schema=False)
    def gauge() -> Response:
        image = control.gauge_image()
        if image is None:
            return Response(status_code=404)
        return Response(image, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})

    @app.post("/api/read-now", status_code=202)
    def read_now() -> JSONResponse:
        if not control.read_now():
            return JSONResponse(
                {"detail": "no service here to take a reading (this is `oiltank web`)"}, 409
            )
        return JSONResponse({"detail": "reading"}, 202)

    history = settings.data_dir / "oiltank.db"
    archive_reader = ArchiveReader(settings.data_dir / "archive")
    camera = settings.camera
    frames = FrameStore(
        archive_reader,
        control.calibration_file(),
        (lambda: HttpSource.from_settings(camera)) if camera.url else None,
    )
    workspace = Workspace(settings, control, history)
    app.include_router(calibration_router(workspace, frames, read_only=read_only))
    app.include_router(geometry_router(workspace, frames, archive_reader))
    app.include_router(history_router(settings, control, archive_reader, history))
    return app
