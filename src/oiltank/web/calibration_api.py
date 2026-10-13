"""Changing the calibration from the web UI: its points here, and its geometry with
:mod:`oiltank.web.geometry_api`.

The browser keeps a draft: the points, and the axis, disc and anchor. The server works out new
points (from a reading you trust, or from a delivery and its ticket) and adds them to the draft
the way ``oiltank calibrate point`` does, so a point with the same reading is replaced and the
points must still rise together. A draft can be previewed against the history, then saved; the
save is refused if the calibration file has changed since the draft started (``If-Match``).
"""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field, ValidationError

from oiltank.calibrate import SAME_POINT, dated_note, warnings, with_point
from oiltank.calibration import (
    Calibration,
    CalibrationError,
    CalibrationPoint,
    reference_name,
)
from oiltank.config import Settings
from oiltank.epochs import AxisEpoch, Change, successor
from oiltank.filter import FilterParams
from oiltank.service import CalibrationConflictError, ServiceBusyError, ServiceStoppedError
from oiltank.web.control import Control, Current
from oiltank.web.frames import FrameRef, FrameStore, NoSuchFrame
from oiltank.web.history_api import read_history, series_payload
from oiltank.web.responses import json_response
from oiltank.web.timeline import build_series, scale

FILL_SEARCH = timedelta(hours=2)
"""How far from the time asked for a refill may be."""
RECENT_FILLS = 12


class Draft(BaseModel):
    """A changed calibration: its points, and any geometry that changed (the rest as saved).

    The geometry is checked when it's applied to the saved calibration (:meth:`Workspace.draft`),
    not here, so a check of a draft that doesn't work can say why.
    """

    points: list[dict[str, Any]]
    axis: dict[str, Any] | None = None
    disc: dict[str, Any] | None = None
    anchor: dict[str, Any] | None = None


class FromReading(Draft):
    ts: datetime
    fraction: Annotated[float, Field(ge=0, le=1)]
    note: str = ""


class FromFill(Draft):
    fill_ts: datetime
    delivered: Annotated[float, Field(gt=0)]
    """The amount on the delivery ticket, in the tank's unit."""
    note: str = ""


class Preview(Draft):
    start: datetime | None = None
    end: datetime | None = None


class Save(Draft):
    read_now: bool = False
    reference: FrameRef | None = None
    """A rebase: this frame becomes the reference, and the geometry moved with the camera."""


def error_text(e: ValidationError) -> str:
    """A validation error, in words (its first one, and where)."""
    err = e.errors()[0]
    msg = str(err["msg"]).removeprefix("Value error, ")
    where = ".".join(str(part) for part in err["loc"])
    return f"{where}: {msg}" if where else msg


class Workspace:
    """What the calibration and geometry routes share: the calibration file, the calibration
    readings are shown with, and drafts made against them."""

    def __init__(self, settings: Settings, control: Control, history_path: Path) -> None:
        self.settings = settings
        self.control = control
        self.history_path = history_path

    def loaded(self) -> tuple[Calibration, str]:
        """The calibration file as it is now, and its tag."""
        found = self.control.calibration_file().load()
        if found is None:
            raise HTTPException(409, "there's no usable calibration to change")
        return found

    def current(self) -> Current:
        cur = self.control.current()
        if cur is None:
            raise HTTPException(409, "there's no usable calibration")
        return cur

    def draft(self, draft: Draft) -> Calibration:
        """The saved calibration with the draft's changes; raises ValidationError if they don't
        make a calibration."""
        changes: dict[str, Any] = {"points": draft.points}
        for name in ("axis", "disc", "anchor"):
            if getattr(draft, name) is not None:
                changes[name] = getattr(draft, name)
        return self.loaded()[0].updated(**changes)

    def drafted(self, draft: Draft) -> Calibration:
        """:meth:`draft`, as a 422 if it doesn't make a calibration."""
        try:
            return self.draft(draft)
        except ValidationError as e:
            raise HTTPException(422, f"that doesn't make a calibration: {error_text(e)}") from e

    def current_for(self, calibration: Calibration) -> Current:
        """``calibration``, with the epoch its axis would have: stored ``t`` carried onto it."""
        cur = self.current()
        if calibration.axis == cur.calibration.axis:
            return Current(calibration, cur.epoch)
        reference = calibration.reference_image.as_posix()
        with read_history(self.history_path) as h:
            epoch = (
                h.epoch_for(reference, calibration.axis)[0]
                if h is not None
                else successor(None, reference, calibration.axis)
            )
        return Current(calibration, epoch)


def calibration_router(workspace: Workspace, frames: FrameStore, *, read_only: bool) -> APIRouter:
    router = APIRouter()
    settings, control, history_path = workspace.settings, workspace.control, workspace.history_path
    tank = settings.tank
    loaded, current = workspace.loaded, workspace.current

    def drafted(points: list[dict[str, Any]]) -> Calibration:
        return workspace.drafted(Draft(points=points))

    def added(points: list[dict[str, Any]], point: CalibrationPoint) -> dict[str, Any]:
        draft = drafted(points)
        try:
            updated, replaced = with_point(draft, point)
        except ValidationError as e:
            raise HTTPException(422, _conflict(draft, point) or error_text(e)) from e
        return {
            **points_of(updated),
            "point": point.model_dump(mode="json", exclude_none=True),
            "replaced": [p.model_dump(mode="json", exclude_none=True) for p in replaced],
        }

    @router.get("/api/calibration")
    def get(request: Request) -> Response:
        calibration, etag = loaded()
        return json_response(request, describe(calibration, etag))

    def describe(calibration: Calibration, etag: str) -> dict[str, Any]:
        with read_history(history_path) as h:
            fills = [] if h is None else h.fills()[-RECENT_FILLS:]
        return {
            "etag": etag,
            "read_only": read_only,
            "unit": tank.unit,
            "capacity": tank.capacity,
            "reference_image": calibration.reference_image.as_posix(),
            "frame_size": list(calibration.frame_size),
            "axis": calibration.axis.model_dump(mode="json"),
            "disc": calibration.disc.model_dump(mode="json"),
            "anchor": calibration.anchor.model_dump(mode="json"),
            **points_of(calibration),
            "scale": scale(calibration),
            "warnings": warnings(calibration),
            "fills": [
                {
                    "ts": f.ts.isoformat(),
                    "before": round(100 * f.before, 1),
                    "after": round(100 * f.after, 1),
                    "volume": round(f.volume(tank.capacity), 1),
                }
                for f in reversed(fills)
            ],
        }

    @router.post("/api/calibration/preview")
    def preview(request: Request, body: Preview) -> Response:
        """The readings as they would be with the draft (a changed axis carries them onto it)."""
        draft = workspace.current_for(workspace.drafted(body))
        payload = series_payload(
            settings, history_path, draft, draft.calibration, body.start, body.end
        )
        return json_response(request, payload)

    @router.post("/api/calibration/points/from-reading")
    def from_reading(body: FromReading) -> dict[str, Any]:
        """A point where the disc was at a reading, which the gauge showed as ``fraction``."""
        cur = current()
        ts = body.ts if body.ts.tzinfo else body.ts.replace(tzinfo=UTC)
        with read_history(history_path) as h:
            rows = (
                []
                if h is None
                else h.readings(
                    ts - timedelta(seconds=1), ts + timedelta(seconds=1), onto=cur.epoch
                )
            )
        row = min(rows, key=lambda r: abs(r.ts - ts), default=None)
        if row is None:
            raise HTTPException(404, "there's no reading at that time")
        if row.t is None or row.fraction is None:
            raise HTTPException(422, "that reading didn't find the disc, so it has no position")
        x, y = cur.calibration.axis.point(row.t)
        when = row.ts.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
        note = dated_note(body.note or f"the reading at {when}", date.today())
        point = CalibrationPoint(at=(round(x, 1), round(y, 1)), fraction=body.fraction, note=note)
        return added(body.points, point)

    @router.post("/api/calibration/points/from-fill")
    def from_fill(body: FromFill) -> dict[str, Any]:
        """A point after a refill: the level before it plus what the ticket says was delivered,
        where the disc was once the refill was confirmed."""
        cur = current()
        fill_ts = body.fill_ts if body.fill_ts.tzinfo else body.fill_ts.replace(tzinfo=UTC)
        params = FilterParams.from_settings(settings.service)
        start = fill_ts - timedelta(days=1)
        warm_up = start - timedelta(days=params.usage_window_days + 1)
        with read_history(history_path) as h:
            rows = (
                []
                if h is None
                else h.readings(warm_up, fill_ts + timedelta(days=2), onto=cur.epoch)
            )
            last_fill = None if h is None else h.last_fill(warm_up)
        series = build_series(
            rows, start, cur.calibration, params, tank.capacity, tank.reserve, last_fill
        )
        target = fill_ts.timestamp()
        fill = min(series["fills"], key=lambda f: abs(f["ts"] - target), default=None)
        if fill is None or abs(fill["ts"] - target) > FILL_SEARCH.total_seconds():
            raise HTTPException(404, "the history shows no refill then")
        before = fill["before"] / 100
        after = before + body.delivered / tank.capacity
        if after > 1:
            raise HTTPException(
                422,
                f"{100 * before:.0f}% plus {body.delivered:g} {tank.unit} is more than the tank "
                f"holds ({tank.capacity:g} {tank.unit}); check the amount, or the level before",
            )
        cols = series["readings"]
        i = cols["ts"].index(fill["ts"])
        after_fill = [
            cols["t"][j] for j in range(i, len(cols["ts"])) if cols["level"][j] is not None
        ]
        if not after_fill:
            raise HTTPException(422, "no reading after the refill found the disc")
        x, y = cur.calibration.axis.point(median(after_fill[:2]))
        day = datetime.fromtimestamp(fill["ts"], UTC).strftime("%Y-%m-%d")
        note = dated_note(
            body.note or f"delivery on {day}: {body.delivered:g} {tank.unit} on the ticket",
            date.today(),
        )
        point = CalibrationPoint(at=(round(x, 1), round(y, 1)), fraction=round(after, 4), note=note)
        return {
            **added(body.points, point),
            "before": round(100 * before, 2),
            "after": round(100 * after, 2),
        }

    @router.put("/api/calibration")
    def save(
        request: Request,
        body: Save,
        if_match: Annotated[str | None, Header()] = None,
    ) -> Response:
        """Save the draft as the calibration, and use it at once."""
        if read_only:
            raise HTTPException(403, "the web UI is read-only (OILTANK_WEB_READ_ONLY)")
        if if_match is None:
            raise HTTPException(428, "say which calibration the changes were made to (If-Match)")
        draft = workspace.drafted(body)
        reference = None
        if body.reference is not None:
            draft, reference = rebased(draft, body.reference)
        try:
            etag = control.save_calibration(
                draft, if_match=if_match.strip('"'), read_now=body.read_now, reference=reference
            )
        except CalibrationConflictError as e:
            raise HTTPException(
                412,
                "the calibration has changed since you started (perhaps `oiltank calibrate`); "
                "reload it and make your changes again",
            ) from e
        except CalibrationError as e:
            raise HTTPException(422, str(e)) from e
        except (ServiceBusyError, ServiceStoppedError) as e:
            raise HTTPException(503, f"not saved: {e}") from e
        saved, _ = loaded()
        return json_response(request, describe(saved, etag))

    def rebased(draft: Calibration, ref: FrameRef) -> tuple[Calibration, bytes]:
        """``draft`` on a new reference frame, which must only have moved the geometry with the
        camera: readings can't be carried across a rebase and an axis edit at once."""
        try:
            frame = frames.get(ref)
        except NoSuchFrame as e:
            raise HTTPException(404, str(e)) from e
        if ref.kind == "reference" or frame.data is None:
            raise HTTPException(422, "rebase onto an archived or newly grabbed frame")
        name = reference_name(frame.ts)
        saved, _ = loaded()
        before = AxisEpoch(saved.reference_image.as_posix(), saved.axis.top, saved.axis.bottom)
        if successor(before, name.as_posix(), draft.axis).change is not Change.REBASE:
            raise HTTPException(
                422,
                "a rebase only moves the gauge with the camera; save it on its own, then change "
                "the axis",
            )
        return draft.updated(reference_image=name), frame.data

    return router


def points_of(calibration: Calibration) -> dict[str, Any]:
    """``points`` as the draft holds them, in order of position, and each one's ``t`` on the
    axis (``positions``, for showing; not part of a point)."""
    ordered = sorted(calibration.points, key=lambda p: p.t_on(calibration.axis))
    return {
        "points": [p.model_dump(mode="json", exclude_none=True) for p in ordered],
        "positions": [round(p.t_on(calibration.axis), 5) for p in ordered],
    }


def _conflict(calibration: Calibration, point: CalibrationPoint) -> str | None:
    """Which existing points a new one contradicts: readings must rise up the gauge."""
    axis = calibration.axis
    t = point.t_on(axis)
    clashes = [
        p
        for p in calibration.points
        if (p.t_on(axis) - t) * (p.fraction - point.fraction) <= 0
        and abs(p.fraction - point.fraction) >= SAME_POINT
    ]
    if not clashes:
        return None
    names = ", ".join(
        f"{100 * p.fraction:g}%" + (f" ({p.note})" if p.note else "") for p in clashes
    )
    return (
        f"a {100 * point.fraction:g}% point there contradicts the {names} point: readings must "
        "rise up the gauge. Remove that point first if this one is right"
    )
