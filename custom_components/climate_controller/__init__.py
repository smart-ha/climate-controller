"""The climate controller integration."""
import logging

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry

from .const import (
    DEFAULT_OCCUPANCY_CONFIG,
    DEFAULT_TEMPERATURE_AGGREGATION,
    DOMAIN,
)
from .occupancy import OccupancyStore

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
    - Fills in the ``occupancy`` block key by key, so an entry written before
      a given occupancy setting existed picks up its default instead of
      making every reader guard for a missing key.
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

    occupancy = dict(new_data.get("occupancy") or {})
    for key, default in DEFAULT_OCCUPANCY_CONFIG.items():
        if key not in occupancy:
            occupancy[key] = list(default) if isinstance(default, list) else default
            changed = True
    new_data["occupancy"] = occupancy

    return new_data, changed


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up climate controller from a config entry."""
    _LOGGER.info("Setting up climate controller for entry %s", entry.entry_id)

    migrated, changed = _migrate_entry_data(entry.data)
    if changed:
        hass.config_entries.async_update_entry(entry, data=migrated)
        _LOGGER.info("Migrated entry %s to the current data shape", entry.entry_id)

    # Learned occupancy history is runtime data, not configuration: it is
    # rewritten by every card click and every hourly slot rollover, so it gets
    # its own Store instead of riding along in the config entry.
    occupancy_store = OccupancyStore(hass, entry.entry_id)
    await occupancy_store.async_load()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "occupancy": occupancy_store,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _LOGGER.info("Climate controller setup completed for entry %s", entry.entry_id)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.info("Unloading climate controller for entry %s", entry.entry_id)
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unloaded
