"""The climate controller integration."""
import logging

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry

from .const import DOMAIN

PLATFORMS = ["climate"]

_LOGGER = logging.getLogger(__name__)


def _migrate_entry_data(entry_data: dict) -> tuple[dict, bool]:
    """Bring entry data up to current shape. Idempotent.

    - Ensures top-level ``temperature_sensor`` exists (default None).
    - Strips the obsolete per-device ``sensor`` key that an earlier
      iteration introduced before the design moved to a single
      controller-level sensor.
    """
    new_data = dict(entry_data)
    changed = False

    if "temperature_sensor" not in new_data:
        new_data["temperature_sensor"] = None
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
        _LOGGER.info(
            "Migrated entry %s: added 'sensor' field to per-device configs",
            entry.entry_id,
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _LOGGER.info("Climate controller setup completed for entry %s", entry.entry_id)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.info("Unloading climate controller for entry %s", entry.entry_id)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
