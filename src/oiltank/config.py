"""Settings, from environment variables and an optional ``.env`` file.

Every setting is ``OILTANK_<SECTION>_<KEY>`` (``OILTANK_<KEY>`` at the top level), for example
``OILTANK_CAMERA_BURST_FRAMES``: the section is split off at the first underscore after the prefix
and the rest is the key. That only stays unambiguous while sections are one level deep and no
top-level setting starts with a section name and an underscore (a test checks this).

Real environment variables win over ``.env``, and ``.env`` over the defaults here. Docker Compose
passes ``.env`` to the container; from a checkout, ``.env`` in the current directory is read
(pydantic-settings loads it with python-dotenv). ``.env.example`` lists every setting.
"""

import os
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PREFIX = "OILTANK_"


class _Section(BaseModel):
    # Unknown keys are errors, so a misspelt OILTANK_CAMERA_... variable is caught.
    model_config = ConfigDict(extra="forbid", frozen=True)


class CameraSettings(_Section):
    """Where frames come from: an HTTP endpoint that returns a full-resolution JPEG."""

    url: str | None = None
    """For go2rtc: ``http://<host>:1984/api/frame.jpeg?src=<stream>``."""
    username: str | None = None
    password: SecretStr | None = None
    burst_frames: Annotated[int, Field(ge=1, le=30)] = 5
    burst_spacing_s: Annotated[float, Field(ge=0)] = 2.0
    timeout_s: Annotated[float, Field(gt=0)] = 15.0
    retries: Annotated[int, Field(ge=0, le=10)] = 3
    retry_backoff_s: Annotated[float, Field(ge=0)] = 2.0
    verify_tls: bool = True

    @field_validator("url")
    @classmethod
    def _http_url(cls, url: str | None) -> str | None:
        if url is not None:
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.netloc:
                raise ValueError("must be an http:// or https:// URL")
            if parts.username or parts.password:
                raise ValueError("put credentials in USERNAME and PASSWORD, not in the URL")
        return url


class MqttSettings(_Section):
    """The MQTT broker Home Assistant uses (milestone M4)."""

    host: str | None = None
    port: Annotated[int, Field(ge=1, le=65535)] = 1883
    username: str | None = None
    password: SecretStr | None = None
    tls: bool = False
    client_id: str = "oiltank"
    topic_prefix: str = "oiltank"
    discovery_prefix: str = "homeassistant"


class DeviceSettings(_Section):
    """How the tank appears in Home Assistant (milestone M4)."""

    id: Annotated[str, Field(pattern=r"^[a-z0-9_]+$")] = "oil_tank"
    name: str = "Oil Tank"


class TankSettings(_Section):
    """The tank itself (milestone M5)."""

    capacity: Annotated[float, Field(gt=0)] = 260.0
    """Usable capacity when the gauge reads F."""
    unit: Literal["gal", "L"] = "gal"
    reserve: Annotated[float, Field(ge=0)] = 0.0
    """"Days left" counts down to this volume rather than to empty."""


class ServiceSettings(_Section):
    """The long-running service: how often it reads, and how it filters and derives values."""

    interval_min: Annotated[float, Field(gt=0)] = 15.0
    min_confidence: Annotated[float, Field(ge=0, le=1)] = 0.5
    """Readings below this confidence are rejected; the last good level stays published."""
    failures_for_problem: Annotated[int, Field(ge=1)] = 3
    """Consecutive rejected readings before Problem turns on."""
    smoothing: Annotated[int, Field(ge=1, le=20)] = 3
    """The published level is the median of this many recent good readings."""
    refill_jump: Annotated[float, Field(gt=0, le=1)] = 0.15
    """A rise of more than this (fraction of the gauge), confirmed by the next reading, is a
    refill."""
    usage_window_days: Annotated[float, Field(gt=0)] = 7.0
    usage_min_hours: Annotated[float, Field(gt=0)] = 24.0
    usage_min_points: Annotated[int, Field(ge=3)] = 12


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter="_",
        env_nested_max_split=1,  # OILTANK_CAMERA_BURST_FRAMES -> camera.burst_frames
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,  # `OILTANK_CAMERA_PASSWORD=` means "not set"
        extra="ignore",  # .env may hold other variables, e.g. TZ for Compose
        frozen=True,
    )

    data_dir: Path = Path("data")
    """Calibration, history and archive. Relative to the working directory: ./data in a checkout,
    and /data in the container (which runs from /), where Compose mounts ./data."""
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    camera: CameraSettings = CameraSettings()
    mqtt: MqttSettings = MqttSettings()
    device: DeviceSettings = DeviceSettings()
    tank: TankSettings = TankSettings()
    service: ServiceSettings = ServiceSettings()

    @property
    def calibration_path(self) -> Path:
        return self.data_dir / "calibration.yaml"


def known_variables() -> set[str]:
    """Every ``OILTANK_...`` variable name that maps to a setting."""
    names = set()
    for name, field in Settings.model_fields.items():
        section = field.annotation
        if isinstance(section, type) and issubclass(section, _Section):
            names |= {env_name((name, key)) for key in section.model_fields}
        else:
            names.add(env_name((name,)))
    return names


def unknown_variables(env_file: Path | None = Path(".env")) -> list[str]:
    """``OILTANK_...`` variables, in the environment or ``env_file``, that match no setting.

    Settings ignore unknown top-level names (``.env`` may hold other variables), so without this
    a misspelt section, or the double-underscore form of a name, would silently do nothing.
    """
    names = {k.upper() for k in os.environ}
    if env_file is not None and env_file.is_file():
        names |= {k.upper() for k in dotenv_values(env_file)}
    known = known_variables()
    return sorted(n for n in names if n.startswith(ENV_PREFIX) and n not in known)


def unknown_variables_message(names: list[str]) -> str:
    hint = (
        "; settings use single underscores, e.g. OILTANK_CAMERA_URL"
        if any("__" in n for n in names)
        else ""
    )
    return f"unknown setting(s) {', '.join(names)}{hint} (see .env.example)"


def env_name(loc: tuple[int | str, ...]) -> str:
    """The environment variable for a settings location, e.g. ``OILTANK_CAMERA_URL``."""
    return ENV_PREFIX + "_".join(str(part).upper() for part in loc)


def settings_error(e: ValidationError) -> str:
    """A validation error in terms of the environment variables to fix."""
    lines = []
    for err in e.errors():
        msg = str(err["msg"]).removeprefix("Value error, ")
        lines.append(f"{env_name(err['loc'])}: {msg}")
    return "; ".join(lines)
