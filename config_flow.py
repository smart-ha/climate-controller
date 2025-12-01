"""Config flow for climate controller."""
from homeassistant import config_entries
from homeassistant.core import callback
import voluptuous as vol
from homeassistant.helpers import selector

from .const import DOMAIN

@config_entries.HANDLERS.register(DOMAIN)
class ClimateControllerFlowHandler(config_entries.ConfigFlow):
    """Handle a config flow."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        # Создаем запись с пустыми данными
        return self.async_create_entry(
            title="Climate Controller",
            data={
                "name": "Climate Controller",
                "temp_threshold": 1,  # Значение по умолчанию
                "cooling_devices": [],
                "heating_devices": [],
                "cooling_config": {},
                "heating_config": {},
                "preset_temperatures": {
                    "sleep": 18.0,
                    "work": 23.0,
                    "chill": 24.0
                }
            }
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return ClimateControllerOptionsFlowHandler(config_entry)


class ClimateControllerOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        self.config_entry = config_entry
        self.data = dict(config_entry.data)

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        errors = {}
        
        if user_input is not None:
            # Сохраняем все данные
            self.data["name"] = user_input.get("name", "Climate Controller")
            self.data["temp_threshold"] = user_input.get("temp_threshold", 1)
            
            # Сохраняем температуры для пресетов
            self.data["preset_temperatures"] = {
                "sleep": user_input.get("sleep_temp", 18.0),
                "work": user_input.get("work_temp", 23.0),
                "chill": user_input.get("chill_temp", 24.0)
            }
            
            # Обрабатываем устройства охлаждения
            self.data["cooling_devices"] = user_input.get("cooling_devices", [])
            
            # Инициализируем конфигурацию для устройств охлаждения
            self.data["cooling_config"] = {}
            for i, device in enumerate(self.data["cooling_devices"]):
                self.data["cooling_config"][device] = {
                    "enable": user_input.get(f"{device} enable", True),
                    "passive": user_input.get(f"{device} passive", False),
                    "order": i  # Порядок определяется позицией в списке
                }
            
            # Обрабатываем устройства нагрева
            self.data["heating_devices"] = user_input.get("heating_devices", [])
            
            # Инициализируем конфигурацию для устройств нагрева
            self.data["heating_config"] = {}
            for i, device in enumerate(self.data["heating_devices"]):
                self.data["heating_config"][device] = {
                    "enable": user_input.get(f"{device} enable", True),
                    "passive": user_input.get(f"{device} passive", False),
                    "order": i  # Порядок определяется позицией в списке
                }
            
            # Обновляем заголовок записи
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                title=self.data["name"],
                data=self.data
            )
            
            # Вызываем перезагрузку интеграции после сохранения настроек
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            
            return self.async_create_entry(title="", data=self.data)
        
        # Подготавливаем схему данных
        schema = {}
        
        # Имя конфигурации
        schema[vol.Required("name", default=self.data.get("name", "Climate Controller"))] = str
        
        # Порог температуры
        schema[vol.Required("temp_threshold", default=self.data.get("temp_threshold", 1))] = vol.Coerce(float)
        
        # Температуры для пресетов
        preset_temps = self.data.get("preset_temperatures", {
            "sleep": 18.0,
            "work": 23.0,
            "chill": 24.0
        })
        schema[vol.Required("sleep_temp", default=preset_temps.get("sleep", 18.0))] = vol.Coerce(float)
        schema[vol.Required("work_temp", default=preset_temps.get("work", 23.0))] = vol.Coerce(float)
        schema[vol.Required("chill_temp", default=preset_temps.get("chill", 24.0))] = vol.Coerce(float)
        
        # Устройства охлаждения
        schema[vol.Required("cooling_devices", default=self.data.get("cooling_devices", []))] = selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=["climate", "switch", "input_boolean"],
                multiple=True
            )
        )
        
        # Активность для устройств охлаждения
        for device in self.data.get("cooling_devices", []):
            device_config = self.data.get("cooling_config", {}).get(device, {})
            schema[vol.Required(f"{device} enable", default=device_config.get("enable", True))] = bool
            schema[vol.Required(f"{device} passive", default=device_config.get("passive", False))] = bool

        # Устройства нагрева
        schema[vol.Required("heating_devices", default=self.data.get("heating_devices", []))] = selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=["climate", "switch", "input_boolean"],
                multiple=True
            )
        )
        
        # Активность для устройств нагрева
        for device in self.data.get("heating_devices", []):
            device_config = self.data.get("heating_config", {}).get(device, {})
            schema[vol.Required(f"{device} enable", default=device_config.get("enable", True))] = bool
            schema[vol.Required(f"{device} passive", default=device_config.get("passive", False))] = bool

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema),
            errors=errors,
        )