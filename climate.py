"""Climate controller climate device."""
import logging
from typing import Any, Dict, Optional
from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature
from homeassistant.components.climate.const import (
    HVACMode,
)
from homeassistant.const import UnitOfTemperature, ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.config_entries import ConfigEntry

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Режимы HVAC
HVAC_MODES = [HVACMode.OFF, HVACMode.AUTO]

# Режимы preset
PRESET_MODES = ["work", "chill", "sleep"]

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the climate controller climate device."""
    _LOGGER.info("Setting up climate device for entry %s", config_entry.entry_id)
    # Создаем устройство с именем из конфига
    name = config_entry.data.get("name", "Climate Controller")
    device = ClimateControllerDevice(name, config_entry)
    async_add_entities([device], True)
    _LOGGER.info("Climate device setup completed for entry %s", config_entry.entry_id)

class ClimateControllerDevice(ClimateEntity):
    """Representation of a climate controller device."""

    def __init__(self, name: str, config_entry: ConfigEntry) -> None:
        """Initialize the device."""
        self._name = name
        self._config_entry = config_entry
        self._hvac_mode = HVACMode.OFF
        self._preset_mode = "work"
        
        # Получаем температуры для режимов из конфига
        self._preset_temperatures = config_entry.data.get("preset_temperatures", {
            "sleep": 18.0,
            "work": 23.0,
            "chill": 24.0
        })
        self._target_temperature = self._preset_temperatures.get("work", 23.0)
        self._current_temperature = self._target_temperature
        self._available = True

    @property
    def name(self) -> str:
        """Return the name of the device."""
        return self._name

    @property
    def unique_id(self) -> str:
        """Return a unique ID."""
        return f"{self._config_entry.entry_id}_climate"

    @property
    def hvac_mode(self) -> str:
        """Return hvac operation ie. heat, cool mode."""
        return self._hvac_mode

    @property
    def hvac_modes(self) -> list[str]:
        """Return the list of available hvac operation modes."""
        return HVAC_MODES

    @property
    def preset_mode(self) -> str:
        """Return the current preset mode."""
        return self._preset_mode

    @property
    def preset_modes(self) -> list[str]:
        """Return a list of available preset modes."""
        return PRESET_MODES

    @property
    def temperature_unit(self) -> str:
        """Return the unit of measurement."""
        return UnitOfTemperature.CELSIUS

    @property
    def target_temperature(self) -> float:
        """Return the temperature we try to reach."""
        return self._target_temperature

    @property
    def current_temperature(self) -> float:
        """Return the current temperature."""
        return self._current_temperature

    @property
    def supported_features(self) -> int:
        """Return the list of supported features."""
        return ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self._available

    async def async_set_hvac_mode(self, hvac_mode: str) -> None:
        """Set new target hvac mode."""
        self._hvac_mode = hvac_mode
        self.async_write_ha_state()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set new preset mode."""
        if preset_mode in PRESET_MODES:
            self._preset_mode = preset_mode
            # Устанавливаем температуру для выбранного режима
            if preset_mode in self._preset_temperatures:
                self._target_temperature = self._preset_temperatures[preset_mode]
            self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is not None:
            self._target_temperature = temperature
            self.async_write_ha_state()