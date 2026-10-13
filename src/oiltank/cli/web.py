"""``oiltank web``: the web UI on its own, without the service."""

from typing import Annotated

import typer
import uvicorn

from oiltank.cli.common import load_settings
from oiltank.web.app import create_app
from oiltank.web.control import FileControl

DEFAULT_PORT = 8780


def web(
    host: Annotated[str, typer.Option(help="Address to listen on.")] = "127.0.0.1",
    port: Annotated[
        int | None,
        typer.Option(
            min=1, max=65535, help=f"Default: OILTANK_WEB_PORT, or {DEFAULT_PORT} if that's 0."
        ),
    ] = None,
    read_only: Annotated[
        bool, typer.Option("--read-only", help="Refuse calibration changes.")
    ] = False,
) -> None:
    """Serve the web UI for OILTANK_DATA_DIR without running the service (Ctrl-C stops it).

    For browsing readings on another machine, or a copy of the data. `oiltank run` serves the
    same UI itself when OILTANK_WEB_PORT is set.
    """
    settings = load_settings()
    app = create_app(
        FileControl(settings.data_dir, settings.calibration_path),
        settings,
        read_only=read_only or settings.web.read_only,
    )
    port = port or settings.web.port or DEFAULT_PORT
    typer.echo(f"web UI on http://{host}:{port}/ (Ctrl-C stops it)")
    uvicorn.run(app, host=host, port=port, log_config=None, server_header=False, access_log=False)
