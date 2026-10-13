"""``oiltank calibrate``: create, check and adjust a calibration.

Every command that changes the calibration prints the same report as ``show`` for the result, then
saves it (keeping the previous file as ``.bak``) unless ``--dry-run`` is given.
"""

from collections.abc import Sequence
from datetime import UTC, date
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError

from oiltank.annotate import render_diagnostic, render_grid
from oiltank.calibrate import (
    DiscScan,
    check_anchor,
    default_anchor,
    default_disc,
    default_half_width,
    find_shift,
    scan_disc_sizes,
    shifted,
    warnings,
    with_point,
    without_point,
)
from oiltank.calibration import (
    Anchor,
    Axis,
    Calibration,
    CalibrationError,
    CalibrationPoint,
    Disc,
)
from oiltank.cli.common import (
    CalibrationPath,
    calibration_file,
    fail,
    grab_frames,
    load_calibration,
    load_frames,
    tools,
    validation_message,
    write_bytes,
    write_image,
)
from oiltank.model import Frame, Measurement
from oiltank.pipeline import measure

app = typer.Typer(
    help="Create, check and adjust a calibration. See the README's Calibration section.",
    no_args_is_help=True,
)

XY = tuple[float, float]  # plain aliases: Typer can't read `type` statements
Rect = tuple[int, int, int, int]

Images = Annotated[
    list[Path] | None,
    typer.Option(
        "--image",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Frame to measure on; repeat for a burst. Defaults to the reference frame.",
    ),
]
Camera = Annotated[
    bool,
    typer.Option("--camera", help="Measure on a burst from the camera (OILTANK_CAMERA_URL)."),
]
OneImage = Annotated[
    Path | None,
    typer.Option(
        "--image",
        exists=True,
        dir_okay=False,
        readable=True,
        help="A full-size frame, or use --camera.",
    ),
]
OneCamera = Annotated[
    bool, typer.Option("--camera", help="Take the frame from the camera (OILTANK_CAMERA_URL).")
]
DryRun = Annotated[bool, typer.Option("--dry-run", help="Show the result without saving it.")]
Diagnostic = Annotated[
    Path | None,
    typer.Option("--diagnostic", help="Also write the diagnostic image here (.jpg or .png)."),
]


# ---- helpers -----------------------------------------------------------------------------------


def _invalid(e: ValueError) -> str:
    return validation_message(e) if isinstance(e, ValidationError) else str(e)


def _xy(p: Sequence[float]) -> str:
    return f"({p[0]:.1f}, {p[1]:.1f})"


def _frames(images: list[Path] | None, camera: bool, calibration: Calibration) -> list[Frame]:
    """A burst from the camera, the given image files, or else the reference frame."""
    if camera and images:
        fail("give --image or --camera, not both")
    if camera:
        return grab_frames()
    return load_frames(images or [calibration.reference_path])


def _one_frame(image: Path | None, camera: bool) -> Frame:
    if (image is None) == (not camera):
        fail("give either --image or --camera")
    return grab_frames(frames=1)[0] if camera else load_frames([image] if image else [])[0]


def _measure(calibration: Calibration, frames: Sequence[Frame]) -> Measurement:
    aligner, reader = tools(calibration)
    try:
        return measure(frames, aligner, reader, calibration)
    except CalibrationError as e:
        fail(str(e))


def _scan(
    calibration: Calibration, frames: Sequence[Frame], centre: Disc | None = None
) -> DiscScan:
    aligner, _ = tools(calibration)
    try:
        return scan_disc_sizes(frames, calibration, aligner, centre=centre)
    except CalibrationError as e:
        fail(str(e))
    except ValueError as e:
        fail(f"cannot scan disc sizes: {e}", 1)


def _reference_name(frame: Frame, image: Path | None, calibration_path: Path) -> Path:
    """Where a new reference frame goes, relative to the calibration: an image already next to
    the calibration is used in place; anything else is saved as ``reference-<time>.jpg``."""
    if image is not None and image.resolve().parent == calibration_path.parent.resolve():
        return Path(image.name)
    suffix = (image.suffix.lower() if image is not None else "") or ".jpg"
    return Path(f"reference-{frame.ts.astimezone(UTC):%Y%m%d-%H%M%S}{suffix}")


def _save_reference(frame: Frame, name: Path, calibration_path: Path) -> None:
    """Write the frame's bytes, exactly as received, as the reference (unless it's there)."""
    target = calibration_path.parent / name
    if target.exists():
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        fail(f"cannot create {target.parent}: {e}")
    assert frame.data is not None
    write_bytes(target, frame.data)


def _report(
    calibration: Calibration,
    path: Path,
    frames: Sequence[Frame],
    scan: DiscScan | None = None,
    reference: Frame | None = None,
) -> Measurement:
    """Print what the calibration says, check it, and read ``frames`` with it.

    ``reference`` is the reference frame, if it isn't on disk yet.
    """
    echo = typer.echo
    ax, disc, anchor = calibration.axis, calibration.disc, calibration.anchor
    w, h = calibration.frame_size
    echo(f"calibration  {path}")
    echo(f"reference    {calibration.reference_image}  ({w}x{h})")
    echo(
        f"axis         top {_xy(ax.top)} to bottom {_xy(ax.bottom)}: {ax.length:.1f} px, "
        f"half-width {ax.half_width:g}, margin {ax.margin:g}"
    )

    scan = scan or _scan(calibration, frames)
    holds = scan.holds(disc)
    if holds is None:
        disc_note = "the reading isn't stable at this size"
    else:
        (w0, w1), (h0, h1) = holds
        disc_note = f"the reading holds for widths {w0:g}-{w1:g} and heights {h0:g}-{h1:g}"
    echo(f"disc         {disc.width:g} x {disc.height:g} px; {disc_note}")

    reference = reference or load_frames([calibration.reference_path])[0]
    check = check_anchor(reference.gray, calibration)
    echo(
        f"anchor       rect {list(anchor.rect)}, search {anchor.search} px; contrast "
        f"{check.contrast:.0f}, self-test error {check.max_error_px:.2f} px"
    )

    t_e, t_f = calibration.t_for(0.0), calibration.t_for(1.0)
    span = abs(t_f - t_e) * ax.length
    echo(
        f"scale        E (0%) at {_xy(ax.point(t_e))} t={t_e:.3f}; "
        f"F (100%) at {_xy(ax.point(t_f))} t={t_f:.3f}"
    )
    echo(f"             {span:.0f} px from E to F, {100 / span:.2f}% per px")
    label = "points      "
    for p in sorted(calibration.points, key=lambda p: p.t_on(ax)):
        t = p.t_on(ax)
        where = f"at {_xy(p.at)}" if p.at is not None else f"t={t:.3f} {_xy(ax.point(t))}"
        echo(f"{label} {p.fraction:.1%} {where}" + (f"  {p.note}" if p.note else ""))
        label = " " * 12
    match calibration.points:
        case []:
            echo(f"{label} none: E is the bottom of travel and F the top (defaults)")
        case [p] if p.fraction >= 0.5:
            echo(f"{label} E is the bottom of travel (default until there's a second point)")
        case [_]:
            echo(f"{label} F is the top of travel (default until there's a second point)")

    m = _measure(calibration, frames)
    if m.ok and m.fraction is not None and m.t is not None and m.position is not None:
        echo(
            f"reading      {m.fraction:.1%} (t={m.t:.3f}, confidence {m.confidence:.2f}), disc at "
            f"{_xy(m.position)}, {len(frames)} frame(s)"
        )
    else:
        echo(f"reading      none: {m.problem} (confidence {m.confidence:.2f})")

    problems = [*warnings(calibration), *check.problems]
    if scan.at_edge(disc):
        hint = (
            f"; try {scan.best.width:g} x {scan.best.height:g} (`calibrate disc --scan`)"
            if scan.best is not None and scan.best != disc
            else ""
        )
        problems.append(f"a slightly different disc size would read elsewhere{hint}")
    for problem in problems:
        echo(f"warning: {problem}", err=True)
    if scan.alternatives:
        others = " or ".join(
            f"y={ax.point(alt_t)[1]:.0f} ({count} sizes)" for alt_t, count in scan.alternatives
        )
        echo(
            f"note: other disc sizes find something at {others}; the green outline in the "
            "diagnostic image shows what this size finds",
            err=True,
        )
    return m


def _finish(
    calibration: Calibration,
    path: Path,
    frames: Sequence[Frame],
    *,
    dry_run: bool,
    diagnostic: Path | None,
    scan: DiscScan | None = None,
    reference: Frame | None = None,
) -> None:
    m = _report(calibration, path, frames, scan, reference)
    if diagnostic is not None:
        write_image(diagnostic, render_diagnostic(frames[-1].gray, calibration, m))
    if dry_run:
        typer.echo("dry run: not saved")
        return
    try:
        calibration.save(path)
    except OSError as e:
        fail(f"cannot save {path}: {e}")
    typer.echo(f"saved {path}")


def _note(text: str) -> str:
    today = date.today().isoformat()
    return f"{today}: {text}" if text else today


# ---- commands ----------------------------------------------------------------------------------


@app.command()
def grid(
    out: Annotated[Path, typer.Option("--out", help="Where to write the image (.jpg or .png).")],
    region: Annotated[
        Rect | None,
        typer.Option(metavar="X Y W H", help="Show only this part of the frame, enlarged."),
    ] = None,
    step: Annotated[
        int | None, typer.Option(min=1, help="Grid spacing in frame px. Default: about 16 lines.")
    ] = None,
    width: Annotated[int, typer.Option(min=100, help="Output width in px.")] = 1600,
    image: OneImage = None,
    camera: OneCamera = False,
) -> None:
    """Write a frame with labelled coordinate gridlines, for reading off pixel positions."""
    frame = _one_frame(image, camera)
    try:
        img = render_grid(frame.gray, region, step=step, width=width)
    except ValueError as e:
        fail(str(e))
    write_image(out, img)
    typer.echo(f"wrote {out}")


@app.command()
def init(
    top: Annotated[
        XY, typer.Option(metavar="X Y", help="Disc centre at the top of its travel (frame px).")
    ],
    bottom: Annotated[
        XY, typer.Option(metavar="X Y", help="Disc centre at the bottom of its travel.")
    ],
    image: OneImage = None,
    camera: OneCamera = False,
    calibration_path: CalibrationPath = None,
    half_width: Annotated[
        float | None,
        typer.Option(help="Strip half-width across the axis (px). Default: from the travel."),
    ] = None,
    disc: Annotated[
        XY | None, typer.Option(metavar="W H", help="Disc size (px). Default: scanned for.")
    ] = None,
    anchor: Annotated[
        Rect | None,
        typer.Option(metavar="X Y W H", help="Alignment anchor. Default: just below the gauge."),
    ] = None,
    force: Annotated[
        bool, typer.Option(help="Replace an existing calibration (kept as .bak).")
    ] = False,
    dry_run: DryRun = False,
    diagnostic: Diagnostic = None,
) -> None:
    """Create a calibration from one frame and the two ends of the disc's travel.

    The frame (--image, or --camera) becomes the reference frame, saved next to the calibration.
    """
    calibration_path = calibration_file(calibration_path)
    if calibration_path.exists() and not (force or dry_run):
        fail(f"{calibration_path} exists; use --force to replace it (the old one is kept as .bak)")
    frame = _one_frame(image, camera)
    reference = _reference_name(frame, image, calibration_path)

    def build(travel: Axis, disc_size: Disc) -> Calibration:
        axis = travel.updated(half_width=half_width or default_half_width(travel, disc_size))
        a = (
            Anchor(rect=anchor, search=max(8, round(0.3 * axis.length)))
            if anchor
            else default_anchor(axis, disc_size, frame.size)
        )
        cal = Calibration.build(
            calibration_path.parent,
            reference_image=reference,
            frame_size=frame.size,
            axis=axis,
            disc=disc_size,
            anchor=a,
        )
        cal.use_reference(frame.gray)  # not saved yet
        return cal

    try:
        travel = Axis(top=top, bottom=bottom, half_width=1.0)  # half-width is chosen in build()
        guess = Disc(width=disc[0], height=disc[1]) if disc else default_disc(travel)
        calibration = build(travel, guess)
        if disc is None:
            scan = _scan(calibration, [frame], centre=guess)
            if scan.best is None:
                typer.echo(
                    "warning: no disc size gave a consistent reading; keeping the starting guess. "
                    "Check the axis in the diagnostic image.",
                    err=True,
                )
            else:
                typer.echo(f"scanned for the disc size: {scan.best.width:g} x {scan.best.height:g}")
                calibration = build(travel, scan.best)
    except ValueError as e:
        fail(_invalid(e))

    if not dry_run:
        _save_reference(frame, reference, calibration_path)
    _finish(
        calibration,
        calibration_path,
        [frame],
        dry_run=dry_run,
        diagnostic=diagnostic,
        reference=frame,
    )


@app.command()
def show(
    calibration_path: CalibrationPath = None,
    images: Images = None,
    camera: Camera = False,
    diagnostic: Diagnostic = None,
) -> None:
    """Summarize and check a calibration, and read the gauge with it."""
    calibration_path = calibration_file(calibration_path)
    calibration = load_calibration(calibration_path)
    frames = _frames(images, camera, calibration)
    m = _report(calibration, calibration_path, frames)
    if diagnostic is not None:
        write_image(diagnostic, render_diagnostic(frames[-1].gray, calibration, m))


@app.command("axis")
def set_axis(
    calibration_path: CalibrationPath = None,
    top: Annotated[XY | None, typer.Option(metavar="X Y", help="New top of travel.")] = None,
    bottom: Annotated[XY | None, typer.Option(metavar="X Y", help="New bottom of travel.")] = None,
    half_width: Annotated[float | None, typer.Option(help="Strip half-width (px).")] = None,
    margin: Annotated[float | None, typer.Option(help="Strip extension past each end.")] = None,
    images: Images = None,
    camera: Camera = False,
    dry_run: DryRun = False,
    diagnostic: Diagnostic = None,
) -> None:
    """Move the ends of the disc's travel, or change the strip around it.

    Points recorded with `calibrate point` stay where the disc was; points given as `t` move
    with the axis.
    """
    calibration_path = calibration_file(calibration_path)
    calibration = load_calibration(calibration_path)
    given = {"top": top, "bottom": bottom, "half_width": half_width, "margin": margin}
    changes: dict[str, Any] = {k: v for k, v in given.items() if v is not None}
    if not changes:
        fail("nothing to change: give --top, --bottom, --half-width or --margin")
    try:
        updated = calibration.updated(axis=calibration.axis.updated(**changes))
    except ValueError as e:
        fail(_invalid(e))
    moving = [p for p in calibration.points if p.t is not None]
    if moving and ("top" in changes or "bottom" in changes):
        typer.echo(f"note: {len(moving)} point(s) given as t move with the axis")
    frames = _frames(images, camera, updated)
    _finish(updated, calibration_path, frames, dry_run=dry_run, diagnostic=diagnostic)


@app.command("disc")
def set_disc(
    calibration_path: CalibrationPath = None,
    size: Annotated[XY | None, typer.Option(metavar="W H", help="Disc size (px).")] = None,
    scan: Annotated[
        bool, typer.Option("--scan", help="Choose the size that reads most consistently.")
    ] = False,
    images: Images = None,
    camera: Camera = False,
    dry_run: DryRun = False,
    diagnostic: Diagnostic = None,
) -> None:
    """Set the disc size, or scan for the size that reads most consistently.

    A wrong size can lock onto other features in the dome with high confidence. The scan reads
    the frames with sizes from 0.6x to 1.6x the current one and picks the middle of the largest
    range of sizes that agree on where the disc is.
    """
    if (size is None) == (not scan):
        fail("give either --size W H or --scan")
    calibration_path = calibration_file(calibration_path)
    calibration = load_calibration(calibration_path)
    frames = _frames(images, camera, calibration)
    result = None
    try:
        if size is not None:
            updated = calibration.updated(disc=Disc(width=size[0], height=size[1]))
        else:
            result = _scan(calibration, frames)
            _print_scan(result, calibration, len(frames))
            if result.best is None:
                fail("no disc size gave a consistent reading; check the axis first", 1)
            updated = calibration.updated(disc=result.best)
    except ValueError as e:
        fail(_invalid(e))
    _finish(updated, calibration_path, frames, dry_run=dry_run, diagnostic=diagnostic)


def _print_scan(scan: DiscScan, calibration: Calibration, n_frames: int) -> None:
    typer.echo(
        f"disc size scan on {n_frames} frame(s): disc centre y (frame px) for each width x "
        "height.\n* agrees with the starting size, # is the chosen size, ? is low confidence, "
        "-- found nothing"
    )
    typer.echo("  w \\ h " + "".join(f"{h:>9g}" for h in scan.heights))
    for i, w in enumerate(scan.widths):
        cells = []
        for j in range(len(scan.heights)):
            t = scan.t[i, j]
            if t != t:  # NaN
                cells.append(f"{'--':>9}")
                continue
            y = calibration.axis.point(float(t))[1]
            cells.append(f"{y:8.1f}{_mark(scan, i, j)}")
        typer.echo(f"{w:>7g} " + "".join(cells))


def _mark(scan: DiscScan, i: int, j: int) -> str:
    best = scan.best
    if best is not None and (scan.widths[i], scan.heights[j]) == (best.width, best.height):
        return "#"
    if scan.stable[i, j]:
        return "*"
    return "?" if scan.confidence[i, j] < 0.8 else " "


@app.command("anchor")
def set_anchor(
    calibration_path: CalibrationPath = None,
    rect: Annotated[
        Rect | None, typer.Option(metavar="X Y W H", help="Static area to align on.")
    ] = None,
    search: Annotated[
        int | None, typer.Option(min=1, help="How far (px) the camera may shift.")
    ] = None,
    min_score: Annotated[
        float | None, typer.Option(min=0, max=1, help="Weakest acceptable anchor match.")
    ] = None,
    recalibrate_shift: Annotated[
        float | None, typer.Option(help="Shift (px) beyond which to suggest `rebase`.")
    ] = None,
    images: Images = None,
    camera: Camera = False,
    dry_run: DryRun = False,
    diagnostic: Diagnostic = None,
) -> None:
    """Change the alignment anchor: a static, textured patch away from the disc."""
    calibration_path = calibration_file(calibration_path)
    calibration = load_calibration(calibration_path)
    given = {
        "rect": rect,
        "search": search,
        "min_score": min_score,
        "recalibrate_shift": recalibrate_shift,
    }
    changes: dict[str, Any] = {k: v for k, v in given.items() if v is not None}
    if not changes:
        fail("nothing to change: give --rect, --search, --min-score or --recalibrate-shift")
    try:
        updated = calibration.updated(anchor=calibration.anchor.updated(**changes))
    except ValueError as e:
        fail(_invalid(e))
    frames = _frames(images, camera, updated)
    _finish(updated, calibration_path, frames, dry_run=dry_run, diagnostic=diagnostic)


@app.command()
def point(
    fraction: Annotated[
        float,
        typer.Option(min=0, max=1, help="What the gauge reads, as a fraction: 0.98 for 98%."),
    ],
    calibration_path: CalibrationPath = None,
    images: Images = None,
    camera: Camera = False,
    at: Annotated[
        XY | None,
        typer.Option(metavar="X Y", help="Disc centre (reference-frame px), instead of measuring."),
    ] = None,
    t: Annotated[
        float | None, typer.Option(help="Position along the axis, instead of measuring.")
    ] = None,
    note: Annotated[str, typer.Option(help="Where the reading came from.")] = "",
    remove: Annotated[
        bool, typer.Option("--remove", help="Remove the point for this reading instead.")
    ] = False,
    min_confidence: Annotated[
        float, typer.Option(min=0, max=1, help="Refuse to record a less confident reading.")
    ] = 0.8,
    dry_run: DryRun = False,
    diagnostic: Diagnostic = None,
) -> None:
    """Record what the gauge reads now, from a reading you trust.

    Measures where the disc is in the frames and stores that position with the reading, replacing
    any point with the same reading. The position is fixed in the frame, so later axis edits don't
    move it. With --camera, it measures the camera's view now.
    """
    calibration_path = calibration_file(calibration_path)
    calibration = load_calibration(calibration_path)
    frames = _frames(images, camera, calibration)
    try:
        if remove:
            updated, removed = without_point(calibration, fraction)
            if not removed:
                fail(f"there is no {fraction:.1%} point to remove")
            for p in removed:
                typer.echo(
                    f"removed the {p.fraction:.1%} point" + (f" ({p.note})" if p.note else "")
                )
        else:
            if at is not None and t is not None:
                fail("give --at or --t, not both")
            if at is not None:
                new = CalibrationPoint(at=at, fraction=fraction, note=_note(note))
            elif t is not None:
                new = CalibrationPoint(t=t, fraction=fraction, note=_note(note))
            else:
                m = _measure(calibration, frames)
                if not m.ok or m.position is None or m.fraction is None:
                    fail(f"no reading to record: {m.problem}", 1)
                if m.confidence < min_confidence:
                    fail(
                        f"the reading's confidence is {m.confidence:.2f}, below "
                        f"{min_confidence:g}; check it with `calibrate show --diagnostic`",
                        1,
                    )
                x, y = m.position
                new = CalibrationPoint(
                    at=(round(x, 1), round(y, 1)), fraction=fraction, note=_note(note)
                )
                typer.echo(
                    f"measured the disc at {_xy(m.position)} on {len(frames)} frame(s); "
                    f"it read {m.fraction:.1%} before this point"
                )
            updated, replaced = with_point(calibration, new)
            for p in replaced:
                typer.echo(
                    f"replaces the {p.fraction:.1%} point" + (f" ({p.note})" if p.note else "")
                )
    except ValidationError as e:
        fail(
            f"that point doesn't fit the others ({validation_message(e)}); remove the "
            "conflicting point with --remove first"
        )
    _finish(updated, calibration_path, frames, dry_run=dry_run, diagnostic=diagnostic)


@app.command()
def rebase(
    image: OneImage = None,
    camera: OneCamera = False,
    calibration_path: CalibrationPath = None,
    search: Annotated[
        int | None,
        typer.Option(min=1, help="How far (px) to look for the anchor. Default: 10% of the frame."),
    ] = None,
    dry_run: DryRun = False,
    diagnostic: Diagnostic = None,
) -> None:
    """Follow the camera after a bump, using a new frame as the reference.

    Finds how far the view moved in the new frame and moves the calibration by the same amount.
    Handles shifts, not a changed zoom or angle; those need `calibrate init`.
    """
    calibration_path = calibration_file(calibration_path)
    calibration = load_calibration(calibration_path)
    frame = _one_frame(image, camera)
    try:
        calibration.check_frame_size(frame.size)
    except CalibrationError as e:
        fail(f"{e}. A new resolution needs `calibrate init`")
    radius = search or round(0.1 * max(calibration.frame_size))
    shift = find_shift(calibration, frame.gray, radius)
    if not shift.ok:
        fail(
            f"couldn't find the anchor within {radius} px (best match {shift.score:.2f}); the "
            "view may have changed too much. Use `calibrate init`",
            1,
        )
    typer.echo(
        f"the camera moved {shift.dx:+.1f}, {shift.dy:+.1f} px (anchor match {shift.score:.2f})"
    )
    reference = _reference_name(frame, image, calibration_path)
    try:
        updated = shifted(calibration, shift.dx, shift.dy, reference)
    except ValueError as e:
        fail(f"can't move the calibration that far: {_invalid(e)}")
    updated.use_reference(frame.gray)  # not saved yet
    if not dry_run:
        _save_reference(frame, reference, calibration_path)
    _finish(
        updated,
        calibration_path,
        [frame],
        dry_run=dry_run,
        diagnostic=diagnostic,
        reference=frame,
    )
