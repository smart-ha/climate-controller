"""Reading temperature sensors and collapsing several of them into one.

Split out of ``climate.py`` once the controller grew from a single room
sensor to a set of them: both the climate entity and the config flow need to
know which sensors are configured, and neither should own that knowledge.
"""
from __future__ import annotations

import statistics

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_conversion import TemperatureConverter

from .const import (
    AGGREGATION_MAX,
    AGGREGATION_MEDIAN,
    AGGREGATION_MIN,
    DEFAULT_TEMPERATURE_AGGREGATION,
)


def read_sensor_celsius(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """Return the sensor's value converted to Celsius, or None if unusable."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None, ""):
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    unit = state.attributes.get("unit_of_measurement")
    if unit in (UnitOfTemperature.CELSIUS, "°C", "C", None):
        return value
    if unit in (UnitOfTemperature.FAHRENHEIT, "°F", "F"):
        return TemperatureConverter.convert(
            value, UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.CELSIUS
        )
    if unit in (UnitOfTemperature.KELVIN, "K"):
        return TemperatureConverter.convert(
            value, UnitOfTemperature.KELVIN, UnitOfTemperature.CELSIUS
        )
    return None


def configured_sensors(entry_data: dict) -> list[str]:
    """Sensors the controller measures from.

    Tolerates the pre-multi-sensor entry shape (a single ``temperature_sensor``
    string) so the entity keeps working if it is ever read before
    ``_migrate_entry_data`` has rewritten the entry.
    """
    sensors = entry_data.get("temperature_sensors")
    if sensors is None:
        legacy = entry_data.get("temperature_sensor")
        sensors = [legacy] if legacy else []
    return [entity_id for entity_id in sensors if entity_id]


def aggregation_method(entry_data: dict) -> str:
    """Configured aggregation method, falling back to the default."""
    return entry_data.get("temperature_aggregation") or DEFAULT_TEMPERATURE_AGGREGATION


def aggregate(values: list[float], method: str) -> float | None:
    """Collapse readings into one value. ``None`` when there is nothing to use."""
    if not values:
        return None
    if method == AGGREGATION_MIN:
        return min(values)
    if method == AGGREGATION_MAX:
        return max(values)
    if method == AGGREGATION_MEDIAN:
        return statistics.median(values)
    return statistics.fmean(values)


def read_temperature_celsius(hass: HomeAssistant, entry_data: dict) -> float | None:
    """The controller's measured temperature across all configured sensors.

    Unavailable sensors are skipped rather than poisoning the result: with a
    houseful of battery-powered sensors, one dead cell must not stall the
    boiler. Only when *every* sensor is unusable does this return ``None``,
    which the caller treats as "skip this evaluation".
    """
    readings = [
        value
        for value in (
            read_sensor_celsius(hass, entity_id)
            for entity_id in configured_sensors(entry_data)
        )
        if value is not None
    ]
    return aggregate(readings, aggregation_method(entry_data))
