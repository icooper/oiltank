"""Home Assistant MQTT discovery and state payloads. Pure: no I/O.

One device-based discovery message (``<discovery_prefix>/device/<id>/config``) declares every
entity. They all read one retained JSON state message, except the gauge image (its own retained
topic) and the Read now button (a command topic). Values that aren't known yet are ``null`` in
the state; templates render them as ``None``, which Home Assistant shows as "unknown".
"""

from dataclasses import dataclass
from typing import Any, Self

from oiltank import __version__
from oiltank.config import Settings
from oiltank.model import LevelState, Measurement

PRESS = "PRESS"
ONLINE = "online"
OFFLINE = "offline"


@dataclass(frozen=True, slots=True)
class Topics:
    base: str
    availability: str
    state: str
    image: str
    command: str
    discovery: str
    ha_status: str
    """Home Assistant's birth/last-will topic: ``online`` after it (re)starts."""

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        base = f"{settings.mqtt.topic_prefix}/{settings.device.id}"
        prefix = settings.mqtt.discovery_prefix
        return cls(
            base=base,
            availability=f"{base}/availability",
            state=f"{base}/state",
            image=f"{base}/image",
            command=f"{base}/cmd/read",
            discovery=f"{prefix}/device/{settings.device.id}/config",
            ha_status=f"{prefix}/status",
        )


def discovery_payload(settings: Settings) -> dict[str, Any]:
    """The device-based discovery message for every entity."""
    topics = Topics.from_settings(settings)
    device_id = settings.device.id
    unit = settings.tank.unit

    def entity(platform: str, key: str, name: str, **options: Any) -> dict[str, Any]:
        return {"platform": platform, "unique_id": f"{device_id}_{key}", "name": name, **options}

    def sensor(key: str, name: str, field: str, **options: Any) -> dict[str, Any]:
        template = options.pop("value_template", f"{{{{ value_json.{field} }}}}")
        return entity(
            "sensor", key, name, state_topic=topics.state, value_template=template, **options
        )

    components = {
        "level": sensor(
            "level",
            "Level",
            "level",
            unit_of_measurement="%",
            state_class="measurement",
            suggested_display_precision=0,
            icon="mdi:storage-tank",
        ),
        "volume": sensor(
            "volume",
            "Volume",
            "volume",
            device_class="volume_storage",
            unit_of_measurement=unit,
            state_class="measurement",
            suggested_display_precision=0,
        ),
        "usage_rate": sensor(
            "usage_rate",
            "Usage rate",
            "usage_rate",
            unit_of_measurement=f"{unit}/d",
            state_class="measurement",
            suggested_display_precision=1,
            icon="mdi:fire",
        ),
        "days_left": sensor(
            "days_left",
            "Days left",
            "days_left",
            device_class="duration",
            unit_of_measurement="d",
            suggested_display_precision=0,
            icon="mdi:calendar-clock",
        ),
        "last_fill": sensor(
            "last_fill", "Last fill", "last_fill", device_class="timestamp", icon="mdi:tanker-truck"
        ),
        "last_fill_volume": sensor(
            "last_fill_volume",
            "Last fill amount",
            "last_fill_volume",
            device_class="volume",
            unit_of_measurement=unit,
            suggested_display_precision=0,
        ),
        "gauge": entity(
            "image", "gauge", "Gauge", image_topic=topics.image, content_type="image/jpeg"
        ),
        "read_now": entity(
            "button",
            "read_now",
            "Read now",
            command_topic=topics.command,
            payload_press=PRESS,
            icon="mdi:refresh",
        ),
        "problem": entity(
            "binary_sensor",
            "problem",
            "Problem",
            device_class="problem",
            state_topic=topics.state,
            value_template="{{ 'ON' if value_json.problem else 'OFF' }}",
            json_attributes_topic=topics.state,
            json_attributes_template="{{ {'reason': value_json.reason} | tojson }}",
        ),
        "confidence": sensor(
            "confidence",
            "Confidence",
            "confidence",
            value_template="{{ (value_json.confidence * 100) | round(0) }}",
            unit_of_measurement="%",
            state_class="measurement",
            suggested_display_precision=0,
            entity_category="diagnostic",
        ),
        "last_reading": sensor(
            "last_reading",
            "Last reading",
            "ts",
            device_class="timestamp",
            entity_category="diagnostic",
        ),
        "position": sensor(
            "position",
            "Gauge position",
            "t",
            suggested_display_precision=3,
            entity_category="diagnostic",
            enabled_by_default=False,
        ),
    }
    return {
        "device": {
            "identifiers": [device_id],
            "name": settings.device.name,
            "manufacturer": "oiltank",
            "model": "Float gauge camera reader",
            "sw_version": __version__,
        },
        "origin": {"name": "oiltank", "sw_version": __version__},
        "availability_topic": topics.availability,
        "qos": 1,
        "components": components,
    }


def state_from_measurement(m: Measurement, capacity: float) -> LevelState:
    """A state with just the current reading: nothing derived from history yet."""
    return LevelState(
        ts=m.ts,
        fraction=m.fraction if m.ok else None,
        volume=m.fraction * capacity if m.ok and m.fraction is not None else None,
        confidence=m.confidence,
        t=m.t,
        problem=m.problem,
    )


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def state_payload(state: LevelState) -> dict[str, Any]:
    """The retained JSON state message every entity reads."""
    return {
        "level": _round(None if state.fraction is None else 100 * state.fraction, 1),
        "volume": _round(state.volume, 1),
        "confidence": round(state.confidence, 3),
        "t": _round(state.t, 4),
        "usage_rate": _round(state.usage_rate, 2),
        "days_left": _round(state.days_left, 1),
        "last_fill": None if state.last_fill is None else state.last_fill.isoformat(),
        "last_fill_volume": _round(state.last_fill_volume, 1),
        "problem": state.problem is not None,
        "reason": None if state.problem is None else str(state.problem),
        "ts": state.ts.isoformat(timespec="seconds"),
    }
