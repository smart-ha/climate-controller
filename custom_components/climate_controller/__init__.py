"""The climate controller integration."""
import logging

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry

from .const import DEFAULT_TEMPERATURE_AGGREGATION, DOMAIN

PLATFORMS = ["climate"]

_LOGGER = logging.getLogger(__name__)


def _migrate_entry_data(entry_data: dict) -> tuple[dict, bool]:
    """Bring entry data up to current shape. Idempotent.

    - Folds the single ``temperature_sensor`` string into the
      ``temperature_sensors`` list and picks a default aggregation method,
      so setups made before multi-sensor support keep working untouched.
    - Strips the obsolete per-device ``sensor`` key that an earlier
      iteration introduced before the design moved to a single
      controller-level sensor.
    """
    new_data = dict(entry_data)
    changed = False

    if "temperature_sensors" not in new_data:
        legacy = new_data.get("temperature_sensor")
        new_data["temperature_sensors"] = [legacy] if legacy else []
        changed = True
    # The old single-sensor key is dropped rather than kept in sync: two
    # sources of truth for the same setting is how they drift apart.
    if "temperature_sensor" in new_data:
        del new_data["temperature_sensor"]
        changed = True
    if "temperature_aggregation" not in new_data:
        new_data["temperature_aggregation"] = DEFAULT_TEMPERATURE_AGGREGATION
        changed = True

    for key in ("cooling_config", "heating_config"):
        config = new_data.get(key) or {}
        new_config: dict = {}
        for device, device_config in config.items():
            if isinstance(device_config, dict) and "sensor" in device_config:
                stripped = {k: v for k, v in device_config.items() if k != "sensor"}
                new_config[device] = stripped
                changed = True
            else:
                new_config[device] = device_config
        if changed and key in new_data:
            new_data[key] = new_config
    return new_data, changed


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up climate controller from a config entry."""
    _LOGGER.info("Setting up climate controller for entry %s", entry.entry_id)

    migrated, changed = _migrate_entry_data(entry.data)
    if changed:
        hass.config_entries.async_update_entry(entry, data=migrated)
        _LOGGER.info("Migrated entry %s to the current data shape", entry.entry_id)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _LOGGER.info("Climate controller setup completed for entry %s", entry.entry_id)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.info("Unloading climate controller for entry %s", entry.entry_id)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
