"""Editing the calibration's geometry from the web UI: frames to edit it on, what a draft makes of
them, the disc-size scan, and rebasing onto a new reference frame after the camera moved.
"""

import threading
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, ValidationError

from oiltank.archive import ArchiveReader
from oiltank.calibrate import find_shift, shifted
from oiltank.calibration import CalibrationError, ResolutionMismatch, reference_name
from oiltank.sources.base import CameraError
from oiltank.web.calibration_api import Draft, Workspace, error_text, points_of
from oiltank.web.drafts import evaluate, scan
from oiltank.web.frames import FrameRef, FrameStore, NoSuchFrame
from oiltank.web.responses import json_response

RECENT_FRAMES = 30
RECENT_DAYS = 14
REFERENCE = FrameRef(kind="reference")


class Check(Draft):
    frames: list[FrameRef] = Field(default_factory=lambda: [REFERENCE], min_length=1, max_length=10)


class Grab(BaseModel):
    frames: Annotated[int, Field(ge=1, le=5)] = 3


class Rebase(BaseModel):
    frame: FrameRef
    search: Annotated[int, Field(gt=0)] | None = None
    """How far (px) to look for the anchor; by default 10% of the frame's longer side."""


def geometry_router(workspace: Workspace, frames: FrameStore, archive: ArchiveReader) -> APIRouter:
    router = APIRouter()
    scanning = threading.Semaphore(1)

    def frames_for(refs: list[FrameRef]) -> list[Any]:
        try:
            return frames.all(refs)
        except NoSuchFrame as e:
            raise HTTPException(404, str(e)) from e

    def reference_gray() -> Any:
        try:
            return frames.get(REFERENCE).gray
        except NoSuchFrame as e:
            raise HTTPException(409, str(e)) from e

    @router.get("/api/frames")
    def listing(request: Request) -> Response:
        """Frames to edit on: the reference, recent full frames from the archive, and whether
        the camera can be asked for new ones."""
        loaded = workspace.control.calibration_file().load()
        name = None if loaded is None else loaded[0].reference_image.as_posix()
        recent = []
        today = datetime.now(UTC)
        for k in range(RECENT_DAYS):
            day = (today - timedelta(days=k)).strftime("%Y-%m-%d")
            for r in reversed(archive.records(day)):
                if r.frame_path is not None:
                    recent.append(
                        {
                            "day": r.day,
                            "key": r.key,
                            "ts": r.meta.ts.isoformat(),
                            "url": f"archive/{r.day}/{r.meta.frame}",
                            "problem": r.meta.problem,
                        }
                    )
            if len(recent) >= RECENT_FRAMES:
                break
        return json_response(
            request,
            {
                "reference": {"name": name, "url": "api/frames/reference.jpg"} if name else None,
                "archive": recent[:RECENT_FRAMES],
                "camera": frames.can_grab,
            },
        )

    @router.get("/api/frames/reference.jpg", include_in_schema=False)
    def reference_image() -> Response:
        try:
            name, data = frames.reference_bytes()
        except NoSuchFrame as e:
            raise HTTPException(404, str(e)) from e
        media = "image/png" if name.lower().endswith(".png") else "image/jpeg"
        return Response(data, media_type=media, headers={"Cache-Control": "no-cache"})

    @router.post("/api/frames/grab")
    def grab(body: Grab) -> dict[str, Any]:
        """New frames from the camera, kept for half an hour to edit on or rebase onto."""
        try:
            grab_id, grabbed = frames.grab(body.frames)
        except NoSuchFrame as e:
            raise HTTPException(409, str(e)) from e
        except CameraError as e:
            raise HTTPException(502, f"the camera didn't give a frame: {e}") from e
        return {
            "grab": grab_id,
            "frames": [
                {
                    "index": i,
                    "ts": f.ts.isoformat(),
                    "url": f"api/frames/grab/{grab_id}/{i}.jpg",
                    "frame": {"kind": "grab", "grab": grab_id, "index": i},
                }
                for i, f in enumerate(grabbed)
            ],
        }

    @router.get("/api/frames/grab/{grab_id}/{index}.jpg", include_in_schema=False)
    def grabbed(grab_id: str, index: int) -> Response:
        try:
            frame = frames.get(FrameRef(kind="grab", grab=grab_id, index=index))
        except NoSuchFrame as e:
            raise HTTPException(404, str(e)) from e
        if frame.data is None:
            raise HTTPException(404, "that frame has no image")
        return Response(frame.data, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})

    @router.post("/api/calibration/check")
    def check(body: Check) -> dict[str, Any]:
        """What the reader makes of ``frames`` with the draft, the anchor's self-test, and the
        warnings. A draft that isn't a valid calibration comes back as ``errors``."""
        try:
            draft = workspace.draft(body)
        except ValidationError as e:
            return {"valid": False, "errors": [error_text(e)]}
        reference = reference_gray()
        try:
            result = evaluate(draft, frames_for(body.frames), reference)
        except ResolutionMismatch as e:
            raise HTTPException(422, str(e)) from e
        except CalibrationError as e:
            return {"valid": False, "errors": [str(e)]}
        return {"valid": True, "errors": [], **result}

    @router.post("/api/calibration/scan")
    def disc_scan(body: Check) -> dict[str, Any]:
        """Where the reader puts the disc for disc sizes around the draft's (takes a second or
        two; one at a time)."""
        draft = workspace.drafted(body)
        if not scanning.acquire(blocking=False):
            raise HTTPException(409, "a disc-size scan is already running")
        try:
            return scan(draft, frames_for(body.frames), reference_gray())
        except (ValueError, CalibrationError) as e:
            raise HTTPException(422, f"can't scan disc sizes: {e}") from e
        finally:
            scanning.release()

    @router.post("/api/calibration/rebase")
    def rebase(body: Rebase) -> dict[str, Any]:
        """How far the camera has moved since the reference frame, judged by the anchor, and the
        calibration moved to match. Save it with ``reference`` set to the frame."""
        if body.frame.kind == "reference":
            raise HTTPException(422, "rebase onto an archived or newly grabbed frame")
        saved, _ = workspace.loaded()
        try:
            frame = frames.get(body.frame)
        except NoSuchFrame as e:
            raise HTTPException(404, str(e)) from e
        try:
            saved.check_frame_size(frame.size)
            search = body.search or round(0.1 * max(saved.frame_size))
            found = find_shift(saved, frame.gray, search)
        except CalibrationError as e:
            raise HTTPException(422, str(e)) from e
        shift = {"dx": round(found.dx, 2), "dy": round(found.dy, 2), "score": round(found.score, 3)}
        if not found.ok:
            raise HTTPException(
                422,
                f"the anchor wasn't found clearly in that frame (best match {found.score:.2f}); "
                "if the camera moved a long way, set up the geometry again",
            )
        moved = shifted(saved, found.dx, found.dy, reference_name(frame.ts))
        return {
            "shift": shift,
            "axis": moved.axis.model_dump(mode="json"),
            "disc": moved.disc.model_dump(mode="json"),
            "anchor": moved.anchor.model_dump(mode="json"),
            **points_of(moved),
            "frame": body.frame.model_dump(exclude_none=True),
        }

    return router
