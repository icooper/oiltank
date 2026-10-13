"""Discovery and state payloads. The discovery JSON is golden: tests/data/discovery.json.

After a deliberate entity change, regenerate it with ``UPDATE_GOLDEN=1 uv run pytest
tests/unit/test_discovery.py`` and review the diff.
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from oiltank.config import Settings
from oiltank.model import LevelState, Measurement, Problem
from oiltank.publish.discovery import (
    Topics,
    discovery_payload,
    state_from_measurement,
    state_payload,
)

GOLDEN = Path(__file__).resolve().parents[1] / "data" / "discovery.json"
TS = datetime(2026, 10, 13, 3, 25, 7, tzinfo=UTC)


def _versionless(payload: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(json.dumps(payload))
    payload["device"]["sw_version"] = payload["origin"]["sw_version"] = "<version>"
    return payload


def test_discovery_matches_golden() -> None:
    payload = _versionless(discovery_payload(Settings()))
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.write_text(json.dumps(payload, indent=2) + "\n")
    assert payload == json.loads(GOLDEN.read_text()), (
        "discovery changed: if deliberate, rerun with UPDATE_GOLDEN=1 and review the diff"
    )


def test_every_component_is_unique_and_has_a_platform() -> None:
    components = discovery_payload(Settings())["components"]
    ids = [c["unique_id"] for c in components.values()]
    assert len(set(ids)) == len(ids)
    assert all(
        c["platform"] in {"sensor", "binary_sensor", "image", "button"} for c in components.values()
    )


def test_settings_flow_into_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OILTANK_DEVICE_ID", "garage_tank")
    monkeypatch.setenv("OILTANK_DEVICE_NAME", "Garage Tank")
    monkeypatch.setenv("OILTANK_TANK_UNIT", "L")
    monkeypatch.setenv("OILTANK_MQTT_TOPIC_PREFIX", "home/oil")
    monkeypatch.setenv("OILTANK_MQTT_DISCOVERY_PREFIX", "ha")
    settings = Settings()
    topics = Topics.from_settings(settings)
    assert topics.discovery == "ha/device/garage_tank/config"
    assert topics.state == "home/oil/garage_tank/state"
    assert topics.command == "home/oil/garage_tank/cmd/read"
    assert topics.ha_status == "ha/status"
    payload = discovery_payload(settings)
    assert payload["device"]["identifiers"] == ["garage_tank"]
    assert payload["device"]["name"] == "Garage Tank"
    components = payload["components"]
    assert components["volume"]["unit_of_measurement"] == "L"
    assert components["usage_rate"]["unit_of_measurement"] == "L/d"
    assert components["level"]["unique_id"] == "garage_tank_level"


def test_state_payload_with_everything() -> None:
    state = LevelState(
        ts=TS,
        fraction=0.97812,
        volume=244.53,
        confidence=0.98765,
        t=0.81549,
        problem=None,
        usage_rate=1.2345,
        days_left=171.66,
        last_fill=datetime(2026, 10, 5, 14, 0, tzinfo=UTC),
        last_fill_volume=180.04,
    )
    assert state_payload(state) == {
        "level": 97.8,
        "volume": 244.5,
        "confidence": 0.988,
        "t": 0.8155,
        "usage_rate": 1.23,
        "days_left": 171.7,
        "last_fill": "2026-10-05T14:00:00+00:00",
        "last_fill_volume": 180.0,
        "problem": False,
        "reason": None,
        "ts": "2026-10-13T03:25:07+00:00",
    }


def _measurement(fraction: float | None, problem: Problem | None) -> Measurement:
    return Measurement(
        ts=TS,
        t=None if fraction is None else 0.8,
        fraction=fraction,
        confidence=0.0 if problem else 0.95,
        indicator=None,
        position=None,
        problem=problem,
        notices=(),
        frames=(),
        reader="classical",
    )


def test_state_from_a_good_measurement() -> None:
    payload = state_payload(state_from_measurement(_measurement(0.5, None), capacity=250))
    assert payload["level"] == 50.0
    assert payload["volume"] == 125.0
    assert payload["problem"] is False
    assert payload["usage_rate"] is None  # needs history (M5)


def test_state_from_a_failed_measurement() -> None:
    m = _measurement(None, Problem.CAMERA_MOVED)
    payload = state_payload(state_from_measurement(m, capacity=250))
    assert payload["level"] is None
    assert payload["volume"] is None
    assert payload["problem"] is True
    assert payload["reason"] == "camera_moved"
    json.dumps(payload)  # serializable as-is
