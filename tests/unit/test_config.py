import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from oiltank.config import (
    Settings,
    _Section,
    known_variables,
    settings_error,
    unknown_variables,
    unknown_variables_message,
)

REPO = Path(__file__).resolve().parents[2]


def test_defaults() -> None:
    s = Settings()
    assert s.camera.url is None
    assert s.camera.burst_frames == 5
    assert s.mqtt.port == 1883
    assert s.tank.capacity == 260
    assert s.calibration_path == Path("data/calibration.yaml")


def test_env_file_and_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "OILTANK_CAMERA_URL=http://go2rtc:1984/api/frame.jpeg?src=tank\n"
        "OILTANK_CAMERA_PASSWORD=\n"  # empty: not set
        "OILTANK_MQTT_CLIENT_ID=from-file\n"
        "OILTANK_TANK_CAPACITY=250\n"
        "TZ=Europe/London\n"  # other variables are fine
    )
    monkeypatch.setenv("OILTANK_MQTT_CLIENT_ID", "from-env")
    s = Settings(_env_file=env)
    assert s.camera.url == "http://go2rtc:1984/api/frame.jpeg?src=tank"
    assert s.camera.password is None
    assert s.mqtt.client_id == "from-env"  # the environment wins over .env
    assert s.tank.capacity == 250


def test_env_file_in_working_directory_is_read(tmp_path: Path) -> None:
    Path(".env").write_text("OILTANK_CAMERA_BURST_FRAMES=3\n")
    assert Settings().camera.burst_frames == 3


def test_secrets_are_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OILTANK_MQTT_PASSWORD", "hunter2")
    s = Settings()
    assert "hunter2" not in repr(s)
    assert s.mqtt.password is not None
    assert s.mqtt.password.get_secret_value() == "hunter2"


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("OILTANK_CAMERA_URLL", "x", "OILTANK_CAMERA_URLL: Extra inputs"),
        ("OILTANK_CAMERA_URL", "rtsp://cam/main", "must be an http"),
        ("OILTANK_CAMERA_URL", "http://u:p@go2rtc/api/frame.jpeg", "not in the URL"),
        ("OILTANK_CAMERA_BURST_FRAMES", "0", "OILTANK_CAMERA_BURST_FRAMES"),
        ("OILTANK_DEVICE_ID", "Oil Tank", "OILTANK_DEVICE_ID"),
        ("OILTANK_WEB_PORT", "70000", "OILTANK_WEB_PORT"),
        ("OILTANK_WEB_URL", "oiltank.lan", "must be an http"),
        ("OILTANK_WEB_EMBED_ORIGINS", "https://ha.lan https://ha.lan/lovelace", "isn't an origin"),
    ],
)
def test_invalid_settings_name_the_variable(
    name: str, value: str, message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError) as e:
        Settings()
    assert message in settings_error(e.value)


def test_unknown_variables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    env.write_text("OILTANK__CAMERA__URL=http://x/\nOILTANK_CAMERA_URL=http://x/\nTZ=UTC\n")
    monkeypatch.setenv("OILTANK_CAMRA_URL", "http://x/")  # misspelt section: settings ignore it
    unknown = unknown_variables(env)
    assert unknown == ["OILTANK_CAMRA_URL", "OILTANK__CAMERA__URL"]
    assert "single underscores" in unknown_variables_message(unknown)
    assert unknown_variables(tmp_path / "missing.env") == ["OILTANK_CAMRA_URL"]


def test_single_underscore_names_are_unambiguous() -> None:
    # OILTANK_<SECTION>_<KEY> splits at the first underscore after the prefix, so no top-level
    # setting may start with a section's name and an underscore.
    sections = {
        name
        for name, field in Settings.model_fields.items()
        if isinstance(field.annotation, type) and issubclass(field.annotation, _Section)
    }
    for name in Settings.model_fields:
        assert not any(name.startswith(f"{section}_") for section in sections), name


def test_env_example_lists_every_setting() -> None:
    text = (REPO / ".env.example").read_text()
    listed = {
        line.lstrip("# ").split("=", 1)[0]
        for line in text.splitlines()
        if line.lstrip("# ").startswith("OILTANK_") and "=" in line
    }
    assert listed == known_variables()
    # ...and parses as valid settings.
    Settings(_env_file=REPO / ".env.example")


# ---- the docs agree with the models -------------------------------------------------------------


def _sections() -> dict[str, type[_Section]]:
    return {
        name: field.annotation
        for name, field in Settings.model_fields.items()
        if isinstance(field.annotation, type) and issubclass(field.annotation, _Section)
    }


def _default(variable: str) -> object:
    """The model default behind an OILTANK_... variable."""
    rest = variable.removeprefix("OILTANK_").lower()
    for section, model in _sections().items():
        if rest.startswith(f"{section}_"):
            value = model.model_fields[rest.removeprefix(f"{section}_")].default
            return value.get_secret_value() if hasattr(value, "get_secret_value") else value
    return Settings.model_fields[rest].default


def _matches(documented: str, value: object) -> bool:
    text = documented.strip()
    if value is None:
        return text.lower() == "none"
    if isinstance(value, bool):
        return text.lower() == str(value).lower()
    if isinstance(value, int | float):
        try:
            return float(text) == float(value)
        except ValueError:
            return False
    return text == str(value)


def _readme_table() -> dict[str, str]:
    """Variable -> documented default, from the README's configuration table.

    Rows may list several variables of one section, later ones abbreviated to ``_KEY``, with one
    default each (or one default for all).
    """
    documented: dict[str, str] = {}
    for line in (REPO / "README.md").read_text().splitlines():
        if not line.startswith("| `OILTANK_"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        names = re.findall(r"`(OILTANK_[A-Z_]+|_[A-Z_]+)`", cells[0])
        full = names[0]
        rest = full.removeprefix("OILTANK_").lower()
        section = next((s for s in _sections() if rest.startswith(f"{s}_")), None)
        prefix = f"OILTANK_{section.upper()}" if section else "OILTANK"
        variables = [full] + [prefix + n for n in names[1:]]
        defaults = [d.strip() for d in cells[1].split(",")]
        if len(defaults) == 1:
            defaults *= len(variables)
        assert len(defaults) == len(variables), line
        documented |= dict(zip(variables, defaults, strict=True))
    return documented


def test_readme_configuration_table_matches_the_models() -> None:
    documented = _readme_table()
    assert set(documented) == known_variables()
    wrong = {v: d for v, d in documented.items() if not _matches(d, _default(v))}
    assert not wrong, f"README defaults differ from the code: {wrong}"


def test_env_example_defaults_match_the_models() -> None:
    commented = {}
    for line in (REPO / ".env.example").read_text().splitlines():
        m = re.fullmatch(r"#\s*(OILTANK_[A-Z_]+)=(.*)", line.strip())
        if m:
            commented[m.group(1)] = m.group(2)
    assert commented  # the commented-out lines are documented defaults
    wrong = {v: d for v, d in commented.items() if not _matches(d, _default(v))}
    assert not wrong, f".env.example defaults differ from the code: {wrong}"
