"""Config flow for climate controller."""
from __future__ import annotations

import datetime

from homeassistant import config_entries
from homeassistant.core import callback
import voluptuous as vol
from homeassistant.helpers import selector

from .const import DOMAIN

DEFAULT_PRESET_TEMPS = {"sleep": 18.0, "work": 23.0, "chill": 24.0}
PRESETS = ("sleep", "work", "chill")


def _validate_time(s) -> str | None:
    """Return canonical ``HH:MM`` for a valid time string, or ``None``.

    Accepts ``HH:MM`` and ``HH:MM:SS`` (the latter is what
    :class:`selector.TimeSelector` returns) and normalizes both to
    ``HH:MM`` so chip-text / TimeSelector defaults agree across renders.
    """
    if s is None:
        return None
    try:
        if isinstance(s, datetime.time):
            t = s
        else:
            t = datetime.time.fromisoformat(str(s).strip())
    except (ValueError, TypeError):
        return None
    return t.strftime("%H:%M")


def _coerce_points(raw) -> list[dict]:
    """Defensive coercion of stored ``preset_schedules[preset]``.

    The legacy string format (``"21:00=20,..."``) is intentionally NOT
    migrated (PRD option 4B): if storage holds anything other than a list
    of well-formed ``{"time": "HH:MM", "temp": float}`` dicts, return
    ``[]``.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        t = _validate_time(p.get("time"))
        if t is None:
            continue
        try:
            v = float(p.get("temp"))
        except (TypeError, ValueError):
            continue
        out.append({"time": t, "temp": v})
    return out


def _temp_selector() -> selector.NumberSelector:
    """Single source of truth for the temperature input widget."""
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=5,
            max=35,
            step=0.1,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement="°C",
        )
    )


@config_entries.HANDLERS.register(DOMAIN)
class ClimateControllerFlowHandler(config_entries.ConfigFlow):
    """Handle a config flow."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        return self.async_create_entry(
            title="Climate Controller",
            data={
                "name": "Climate Controller",
                "temp_threshold": 1,
                "temperature_sensor": None,
                "cooling_devices": [],
                "heating_devices": [],
                "cooling_config": {},
                "heating_config": {},
                "preset_temperatures": dict(DEFAULT_PRESET_TEMPS),
                "preset_schedules": {p: [] for p in PRESETS},
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return ClimateControllerOptionsFlowHandler()


def _build_device_config(
    new_devices: list[str],
    prev_devices: list[str],
    prev_config: dict,
    user_input: dict,
    field_prefix: str,
) -> dict:
    """Build per-device config from the submitted form.

    Per-device fields in the form are indexed against the *previous* device
    list (the schema was built before submission), so for each newly submitted
    device we look up its index in the previous list to pull form values.
    Devices added in the same submission have no form rows yet and get
    defaults; they can be configured on the next reopen.
    """
    new_config: dict = {}
    for new_i, device in enumerate(new_devices):
        if device in prev_devices:
            prev_i = prev_devices.index(device)
            new_config[device] = {
                "enable": user_input.get(f"{field_prefix}_dev_{prev_i}_enable", True),
                "passive": user_input.get(f"{field_prefix}_dev_{prev_i}_passive", False),
                "order": new_i,
            }
        else:
            existing = prev_config.get(device, {})
            new_config[device] = {
                "enable": existing.get("enable", True),
                "passive": existing.get("passive", False),
                "order": new_i,
            }
    return new_config


def _add_device_rows(
    schema: dict,
    devices: list[str],
    config: dict,
    field_prefix: str,
) -> None:
    """Add per-device enable/passive fields to the schema."""
    for i, device in enumerate(devices):
        device_config = config.get(device, {})
        schema[
            vol.Required(
                f"{field_prefix}_dev_{i}_enable",
                default=device_config.get("enable", True),
            )
        ] = bool
        schema[
            vol.Required(
                f"{field_prefix}_dev_{i}_passive",
                default=device_config.get("passive", False),
            )
        ] = bool


class ClimateControllerOptionsFlowHandler(config_entries.OptionsFlow):
    """Menu-driven OptionsFlow.

    Layout (PRD choice 1A / 2C / 3A):

    - ``init``  — root menu: Devices & sensors / Sleep schedule / Work
      schedule / Chill schedule / Save & exit.
    - ``devices`` — single big form for sensor, preset_temperatures,
      cooling/heating devices and their per-device toggles. Submit
      returns to ``init``.
    - ``schedule_<preset>`` — sub-menu per preset with: Add point,
      Bulk edit (only when ≥1 point exists), Back. Submit/back returns
      to ``init``.
    - ``add_<preset>`` — form with one ``TimeSelector`` + one
      ``NumberSelector`` to append a single point. Returns to the
      preset's schedule menu.
    - ``bulk_<preset>`` — form with one row per existing point: time +
      temp + delete-checkbox. Submit replaces the point list with the
      surviving rows.
    - ``finish`` — commits ``self.data`` to the config entry,
      ``async_reload``s, and exits the flow.

    Changes accumulate in ``self.data`` across sub-steps; nothing is
    written to storage until the user picks **Save & exit**. Closing
    the dialog mid-flow discards everything (acceptable v1 behaviour).
    """

    async def async_step_init(self, user_input=None):
        """Root menu."""
        if not hasattr(self, "data"):
            self.data = dict(self.config_entry.data)
            self.data.setdefault(
                "preset_schedules", {p: [] for p in PRESETS}
            )
            # Defensive: if storage contains a non-list (legacy string
            # format from earlier PRD draft), drop it. Option 4B.
            for p in PRESETS:
                self.data["preset_schedules"][p] = _coerce_points(
                    self.data["preset_schedules"].get(p)
                )

        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "devices",
                "schedule_sleep",
                "schedule_work",
                "schedule_chill",
                "finish",
            ],
        )

    async def async_step_finish(self, user_input=None):
        """Commit accumulated draft to the config entry and exit."""
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            title=self.data.get("name", "Climate Controller"),
            data=self.data,
        )
        await self.hass.config_entries.async_reload(self.config_entry.entry_id)
        return self.async_create_entry(title="", data=self.data)

    async def async_step_devices(self, user_input=None):
        """Sensor + preset_temperatures + cooling/heating devices."""
        if user_input is not None:
            if user_input.get("_back"):
                return await self.async_step_init()
            prev_cooling_devices = list(self.data.get("cooling_devices", []))
            prev_heating_devices = list(self.data.get("heating_devices", []))
            prev_cooling_config = dict(self.data.get("cooling_config", {}))
            prev_heating_config = dict(self.data.get("heating_config", {}))

            self.data["name"] = user_input.get("name", "Climate Controller")
            self.data["temp_threshold"] = user_input.get("temp_threshold", 1)
            self.data["temperature_sensor"] = (
                user_input.get("temperature_sensor") or None
            )
            self.data["preset_temperatures"] = {
                "sleep": user_input.get("sleep_temp", DEFAULT_PRESET_TEMPS["sleep"]),
                "work": user_input.get("work_temp", DEFAULT_PRESET_TEMPS["work"]),
                "chill": user_input.get("chill_temp", DEFAULT_PRESET_TEMPS["chill"]),
            }

            new_cooling_devices = user_input.get("cooling_devices", [])
            self.data["cooling_devices"] = new_cooling_devices
            self.data["cooling_config"] = _build_device_config(
                new_cooling_devices,
                prev_cooling_devices,
                prev_cooling_config,
                user_input,
                "cooling",
            )

            new_heating_devices = user_input.get("heating_devices", [])
            self.data["heating_devices"] = new_heating_devices
            self.data["heating_config"] = _build_device_config(
                new_heating_devices,
                prev_heating_devices,
                prev_heating_config,
                user_input,
                "heating",
            )
            return await self.async_step_init()

        schema: dict = {}
        schema[vol.Required("name", default=self.data.get("name", "Climate Controller"))] = str
        schema[vol.Required("temp_threshold", default=self.data.get("temp_threshold", 1))] = vol.Coerce(float)

        sensor_default = self.data.get("temperature_sensor")
        sensor_selector = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
        )
        sensor_key = (
            vol.Optional("temperature_sensor", default=sensor_default)
            if sensor_default
            else vol.Optional("temperature_sensor")
        )
        schema[sensor_key] = sensor_selector

        preset_temps = self.data.get("preset_temperatures", DEFAULT_PRESET_TEMPS)
        schema[vol.Required("sleep_temp", default=preset_temps.get("sleep", 18.0))] = vol.Coerce(float)
        schema[vol.Required("work_temp", default=preset_temps.get("work", 23.0))] = vol.Coerce(float)
        schema[vol.Required("chill_temp", default=preset_temps.get("chill", 24.0))] = vol.Coerce(float)

        device_selector = selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=["climate", "switch", "input_boolean"],
                multiple=True,
            )
        )
        cooling_devices = list(self.data.get("cooling_devices", []))
        schema[vol.Required("cooling_devices", default=cooling_devices)] = device_selector
        _add_device_rows(schema, cooling_devices, self.data.get("cooling_config", {}), "cooling")

        heating_devices = list(self.data.get("heating_devices", []))
        schema[vol.Required("heating_devices", default=heating_devices)] = device_selector
        _add_device_rows(schema, heating_devices, self.data.get("heating_config", {}), "heating")

        schema[vol.Required("_back", default=False)] = bool

        return self.async_show_form(step_id="devices", data_schema=vol.Schema(schema))

    # ------------------------------------------------------------------
    # Schedule sub-menus per preset
    # ------------------------------------------------------------------

    def _schedule_menu(self, preset: str):
        """Common renderer for the per-preset schedule menu."""
        points = self.data.get("preset_schedules", {}).get(preset, [])
        options = [f"add_{preset}"]
        if points:
            options.append(f"bulk_{preset}")
        options.append("init")
        summary = (
            "\n".join(f"  {p['time']} → {p['temp']}°C" for p in points)
            if points
            else "(no points)"
        )
        return self.async_show_menu(
            step_id=f"schedule_{preset}",
            menu_options=options,
            description_placeholders={"summary": summary},
        )

    async def async_step_schedule_sleep(self, user_input=None):
        return self._schedule_menu("sleep")

    async def async_step_schedule_work(self, user_input=None):
        return self._schedule_menu("work")

    async def async_step_schedule_chill(self, user_input=None):
        return self._schedule_menu("chill")

    # ------------------------------------------------------------------
    # Add a single point
    # ------------------------------------------------------------------

    def _add_point_schema(self, preset: str) -> vol.Schema:
        default_temps = self.data.get("preset_temperatures", DEFAULT_PRESET_TEMPS)
        return vol.Schema(
            {
                vol.Required("time", default="00:00"): selector.TimeSelector(),
                vol.Required(
                    "temp",
                    default=float(default_temps.get(preset, 22.0)),
                ): _temp_selector(),
                vol.Required("_back", default=False): bool,
            }
        )

    async def _handle_add(self, preset: str, user_input):
        if user_input is not None:
            if user_input.get("_back"):
                return self._schedule_menu(preset)
            errors: dict[str, str] = {}
            canonical = _validate_time(user_input.get("time"))
            if canonical is None:
                errors["time"] = "invalid_schedule"
            try:
                temp = float(user_input.get("temp"))
            except (TypeError, ValueError):
                errors["temp"] = "invalid_schedule"
                temp = None  # type: ignore

            if errors:
                return self.async_show_form(
                    step_id=f"add_{preset}",
                    data_schema=self._add_point_schema(preset),
                    errors=errors,
                )

            schedules = self.data.setdefault(
                "preset_schedules", {p: [] for p in PRESETS}
            )
            existing = [
                p
                for p in schedules.get(preset, [])
                if p.get("time") != canonical
            ]
            existing.append({"time": canonical, "temp": float(temp)})
            existing.sort(key=lambda p: p["time"])
            schedules[preset] = existing
            return self._schedule_menu(preset)

        return self.async_show_form(
            step_id=f"add_{preset}",
            data_schema=self._add_point_schema(preset),
        )

    async def async_step_add_sleep(self, user_input=None):
        return await self._handle_add("sleep", user_input)

    async def async_step_add_work(self, user_input=None):
        return await self._handle_add("work", user_input)

    async def async_step_add_chill(self, user_input=None):
        return await self._handle_add("chill", user_input)

    # ------------------------------------------------------------------
    # Bulk edit: one row per existing point + delete checkbox
    # ------------------------------------------------------------------

    def _bulk_schema(self, points: list[dict]) -> vol.Schema:
        schema: dict = {}
        for i, p in enumerate(points):
            schema[
                vol.Required(f"row_{i}_time", default=p["time"])
            ] = selector.TimeSelector()
            schema[
                vol.Required(f"row_{i}_temp", default=float(p["temp"]))
            ] = _temp_selector()
            schema[vol.Required(f"row_{i}_delete", default=False)] = bool
        schema[vol.Required("_back", default=False)] = bool
        return vol.Schema(schema)

    async def _handle_bulk(self, preset: str, user_input):
        points = list(self.data.get("preset_schedules", {}).get(preset, []))
        if not points:
            # Nothing to bulk-edit — bounce back to the preset menu.
            return self._schedule_menu(preset)

        if user_input is not None:
            if user_input.get("_back"):
                return self._schedule_menu(preset)
            errors: dict[str, str] = {}
            new_points: list[dict] = []
            seen: set[str] = set()
            for i, _orig in enumerate(points):
                if user_input.get(f"row_{i}_delete", False):
                    continue
                canonical = _validate_time(user_input.get(f"row_{i}_time"))
                if canonical is None:
                    errors[f"row_{i}_time"] = "invalid_schedule"
                    continue
                try:
                    temp = float(user_input.get(f"row_{i}_temp"))
                except (TypeError, ValueError):
                    errors[f"row_{i}_temp"] = "invalid_schedule"
                    continue
                if canonical in seen:
                    continue
                seen.add(canonical)
                new_points.append({"time": canonical, "temp": temp})

            if errors:
                return self.async_show_form(
                    step_id=f"bulk_{preset}",
                    data_schema=self._bulk_schema(points),
                    errors=errors,
                )

            new_points.sort(key=lambda p: p["time"])
            self.data.setdefault("preset_schedules", {})[preset] = new_points
            return self._schedule_menu(preset)

        return self.async_show_form(
            step_id=f"bulk_{preset}",
            data_schema=self._bulk_schema(points),
        )

    async def async_step_bulk_sleep(self, user_input=None):
        return await self._handle_bulk("sleep", user_input)

    async def async_step_bulk_work(self, user_input=None):
        return await self._handle_bulk("work", user_input)

    async def async_step_bulk_chill(self, user_input=None):
        return await self._handle_bulk("chill", user_input)
