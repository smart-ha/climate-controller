"""Climate controller climate device."""
from __future__ import annotations

import logging
import time
from typing import Any, Literal

from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature
from homeassistant.components.climate.const import HVACAction, HVACMode, PRESET_NONE
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_TEMPERATURE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity
import voluptuous as vol
import datetime as dt

from .const import (
    ACTION_NONE,
    ACTION_OFF,
    ACTION_ON,
    ACTION_PRESET_PREFIX,
    ACTUATION_DEADBAND,
    DAYS_PER_WEEK,
    DEFAULT_KD,
    DEFAULT_KI,
    DEFAULT_KP,
    DOMAIN,
    DEFAULT_MAX_DEVICE_DELTA_C,
    HOURS_PER_DAY,
    OUTPUT_MAX,
    OUTPUT_MIN,
    PWM_WINDOW_SECONDS,
    SLOT_MODES,
    TICK_INTERVAL_SECONDS,
)
from .occupancy import OccupancyController
from .pid import PidController
from .sensors import (
    aggregation_method,
    configured_sensors,
    read_sensor_celsius,
    read_temperature_celsius,
)
from .schedule import normalize_points, target_at

_LOGGER = logging.getLogger(__name__)

HVAC_MODES = [HVACMode.OFF, HVACMode.AUTO]
# PRESET_NONE ("none") is HA's idiom for "no preset active" — selecting it
# leaves the current target temperature untouched and disables the per-preset
# time-of-day schedule. The three named presets carry fixed defaults + schedules.
PRESET_MODES = [PRESET_NONE, "work", "chill", "sleep"]

# Occupancy grid editing is entity-scoped (each controller learns its own
# week), so these go through the entity service mechanism — that is what lets
# the Lovelace card address a slot with nothing but `entity_id`.
SERVICE_SET_OCCUPANCY_SLOT = "set_occupancy_slot"
SERVICE_CLEAR_OCCUPANCY_OVERRIDES = "clear_occupancy_overrides"
SERVICE_RESET_OCCUPANCY_LEARNING = "reset_occupancy_learning"

# ``day`` and ``hour`` accept a scalar or a list and are applied as a
# rectangle (every listed day × every listed hour). The card's drag-select is
# exactly a rectangle, so painting 20 cells stays one service call.
SET_OCCUPANCY_SLOT_SCHEMA = {
    vol.Required("day"): vol.All(
        cv.ensure_list, [vol.All(vol.Coerce(int), vol.Range(min=0, max=DAYS_PER_WEEK - 1))]
    ),
    vol.Required("hour"): vol.All(
        cv.ensure_list,
        [vol.All(vol.Coerce(int), vol.Range(min=0, max=HOURS_PER_DAY - 1))],
    ),
    vol.Required("mode"): vol.In(SLOT_MODES),
}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the climate controller climate device."""
    _LOGGER.info("Setting up climate device for entry %s", config_entry.entry_id)
    name = config_entry.data.get("name", "Climate Controller")
    device = ClimateControllerDevice(name, config_entry)
    async_add_entities([device], True)

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_SET_OCCUPANCY_SLOT,
        SET_OCCUPANCY_SLOT_SCHEMA,
        "async_set_occupancy_slot",
    )
    platform.async_register_entity_service(
        SERVICE_CLEAR_OCCUPANCY_OVERRIDES, {}, "async_clear_occupancy_overrides"
    )
    platform.async_register_entity_service(
        SERVICE_RESET_OCCUPANCY_LEARNING, {}, "async_reset_occupancy_learning"
    )


class ClimateControllerDevice(ClimateEntity, RestoreEntity):
    """Representation of a climate controller device."""

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = HVAC_MODES
    _attr_preset_modes = PRESET_MODES
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, name: str, config_entry: ConfigEntry) -> None:
        """Initialize the device."""
        self._name = name
        self._config_entry = config_entry
        self._hvac_mode: HVACMode = HVACMode.OFF
        self._hvac_action: HVACAction = HVACAction.OFF
        self._preset_mode = "work"
        self._preset_temperatures = config_entry.data.get(
            "preset_temperatures",
            {"sleep": 18.0, "work": 23.0, "chill": 24.0},
        )
        self._target_temperature = self._preset_temperatures.get("work", 23.0)
        self._available = True

        # PID per device (cooling + heating) — keyed by entity_id with a side
        # prefix so the same entity could appear on both sides without
        # colliding (unlikely but cheap to disambiguate).
        self._pids: dict[str, PidController] = {}
        # Listener / interval handles, populated in async_added_to_hass.
        self._unsub_callbacks: list = []
        # Wall-clock of the last evaluation, for dt calculation.
        self._last_eval_ts: float | None = None
        # PWM duty fraction per on/off device (key = "side:entity_id").
        self._pwm_fractions: dict[str, float] = {}
        # Pending off-pulse async_call_later handles per device.
        self._pwm_off_handles: dict[str, Any] = {}
        # Last-warned monotonic timestamp per preset for invalid schedules
        # (rate-limit so a corrupted storage entry doesn't flood the log).
        self._scheduled_warning_ts: dict[str, float] = {}
        # Runtime-only zone state machine: 'idle' | 'heating' | 'cooling'.
        # Asymmetric hysteresis: width = `temp_threshold` on entry into
        # active zones, 0 on exit (active zones drive until measured hits
        # the exact target). Not persisted across HA restarts — the next
        # _evaluate tick re-evaluates from current measured/setpoint.
        self._zone: Literal["idle", "heating", "cooling"] = "idle"
        # On the very first _evaluate after entity startup, devices may be
        # in stale pre-restart state (heater on, AC in heat) even if our
        # state machine lands in idle. Force _enter_idle_zone once on
        # first tick so the known-idle starting condition is restored.
        self._first_evaluate: bool = True
        # Occupancy schedule. The controller is built in async_added_to_hass,
        # once hass (and the loaded store) is available.
        self._occupancy: OccupancyController | None = None
        # Last occupancy state we acted on. Restored from the previous session
        # so an HA restart mid-evening doesn't re-fire the expected action —
        # which would stomp a manual change the user made after the transition.
        self._occupancy_state: str | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def unique_id(self) -> str:
        return f"{self._config_entry.entry_id}_climate"

    @property
    def hvac_mode(self) -> HVACMode:
        return self._hvac_mode

    @property
    def hvac_action(self) -> HVACAction:
        return self._hvac_action

    @property
    def preset_mode(self) -> str:
        return self._preset_mode

    @property
    def target_temperature(self) -> float:
        return self._target_temperature

    @property
    def current_temperature(self) -> float | None:
        if not self.hass:
            return None
        return read_temperature_celsius(self.hass, self._config_entry.data)

    @property
    def available(self) -> bool:
        return self._available

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the individual readings behind ``current_temperature``.

        With several sensors feeding one loop, "why is it heating?" is only
        answerable if you can see each reading and which of them dropped out.
        """
        if not self.hass:
            return {}
        entry_data = self._config_entry.data
        sensor_ids = configured_sensors(entry_data)
        readings = {
            entity_id: read_sensor_celsius(self.hass, entity_id)
            for entity_id in sensor_ids
        }
        attributes: dict[str, Any] = {
            "temperature_aggregation": aggregation_method(entry_data),
            "temperature_sensors": sensor_ids,
            "temperature_readings": readings,
            "temperature_sensors_unavailable": [
                entity_id for entity_id, value in readings.items() if value is None
            ],
        }
        attributes.update(self._occupancy_attributes())
        return attributes

    def _occupancy_attributes(self) -> dict[str, Any]:
        """Everything the Lovelace card needs to draw the 24×7 grid.

        The grid is published as 7 rows of 24 cell codes (see ``const.py``)
        plus the matching per-cell scores, so the card can colour a cell and
        explain it ("2 of the last 4 weeks") without a second round trip.
        """
        occupancy = self._occupancy
        if occupancy is None:
            return {"occupancy_enabled": False}

        last_motion = occupancy.last_motion
        return {
            "occupancy_enabled": occupancy.enabled,
            "occupancy_state": self._occupancy_state,
            "occupancy_grid": occupancy.grid_by_day(),
            "occupancy_scores": occupancy.scores_by_day(),
            "occupancy_threshold": occupancy.learning_threshold,
            "occupancy_learning_weeks": occupancy.learning_weeks,
            "occupancy_learning_days": occupancy.store.days_observed,
            "occupancy_learning_min_days": occupancy.learning_min_days,
            "occupancy_preheat_minutes": occupancy.preheat_minutes,
            "occupancy_motion_sensors": occupancy.motion_sensors,
            "occupancy_last_motion": (
                last_motion.isoformat() if last_motion is not None else None
            ),
            "occupancy_overrides": occupancy.store.override_count,
            "occupancy_expected_action": occupancy.expected_action,
            "occupancy_away_action": occupancy.away_action,
        }

    async def async_added_to_hass(self) -> None:
        """Wire up sensor listener + periodic tick."""
        await super().async_added_to_hass()

        # Восстанавливаем последнее пользовательское состояние через RestoreEntity,
        # чтобы перезапуск HA не сбрасывал hvac_mode / preset / температуру.
        last_state = await self.async_get_last_state()
        if last_state is not None:
            restored: dict[str, Any] = {}
            try:
                mode_candidate = HVACMode(last_state.state)
            except ValueError:
                mode_candidate = None
            if mode_candidate in HVAC_MODES:
                self._hvac_mode = mode_candidate
                restored["hvac_mode"] = mode_candidate

            preset_candidate = last_state.attributes.get("preset_mode")
            if preset_candidate in PRESET_MODES:
                self._preset_mode = preset_candidate
                restored["preset_mode"] = preset_candidate

            temp_candidate = last_state.attributes.get("temperature")
            if temp_candidate is not None:
                try:
                    self._target_temperature = float(temp_candidate)
                    restored["target_temperature"] = self._target_temperature
                except (TypeError, ValueError):
                    pass

            occupancy_candidate = last_state.attributes.get("occupancy_state")
            if isinstance(occupancy_candidate, str):
                self._occupancy_state = occupancy_candidate
                restored["occupancy_state"] = occupancy_candidate

            if restored:
                _LOGGER.info(
                    "climate_controller[%s]: restored state from last session: %s",
                    self._name,
                    restored,
                )
            else:
                _LOGGER.debug(
                    "climate_controller[%s]: last_state present but no valid fields to restore",
                    self._name,
                )
        else:
            _LOGGER.debug(
                "climate_controller[%s]: no previous state to restore",
                self._name,
            )

        # Build a PID per configured device. Disabled, non-passive devices
        # are ignored — they would just waste cycles.
        # A climate.* device present in BOTH cooling_config AND heating_config
        # (with enable=true on both sides) collapses into a single "bidir" PID
        # whose output sign drives hvac_mode (positive=heat, negative=cool).
        # Non-climate domains in both sides fall back to the legacy two-PID
        # behaviour.
        self._pids = {}
        cooling_cfg = self._config_entry.data.get("cooling_config", {})
        heating_cfg = self._config_entry.data.get("heating_config", {})

        bidir_entities: set[str] = set()
        for entity_id in set(cooling_cfg) & set(heating_cfg):
            if not entity_id.startswith("climate."):
                continue
            c = cooling_cfg[entity_id]
            h = heating_cfg[entity_id]
            if c.get("enable", True) and h.get("enable", True):
                bidir_entities.add(entity_id)
                self._pids[f"bidir:{entity_id}"] = PidController(
                    DEFAULT_KP, DEFAULT_KI, DEFAULT_KD, OUTPUT_MIN, OUTPUT_MAX
                )

        for side, cfg_dict in (("cooling", cooling_cfg), ("heating", heating_cfg)):
            for entity_id, cfg in cfg_dict.items():
                if entity_id in bidir_entities:
                    continue  # already registered as bidir
                if not cfg.get("enable", True) and not cfg.get("passive", False):
                    continue
                self._pids[f"{side}:{entity_id}"] = PidController(
                    DEFAULT_KP, DEFAULT_KI, DEFAULT_KD, OUTPUT_MIN, OUTPUT_MAX
                )

        sensor_ids = configured_sensors(self._config_entry.data)
        if sensor_ids:
            self._unsub_callbacks.append(
                async_track_state_change_event(
                    self.hass, sensor_ids, self._handle_sensor_event
                )
            )

        # Occupancy: the learned week lives in a Store set up by
        # async_setup_entry, shared by the entity and the OptionsFlow.
        store = (
            self.hass.data.get(DOMAIN, {})
            .get(self._config_entry.entry_id, {})
            .get("occupancy")
        )
        if store is not None:
            self._occupancy = OccupancyController(
                self.hass, self._config_entry, store, self._name
            )
            motion_sensors = self._occupancy.motion_sensors
            if motion_sensors:
                self._unsub_callbacks.append(
                    async_track_state_change_event(
                        self.hass, motion_sensors, self._handle_motion_event
                    )
                )
        else:
            _LOGGER.warning(
                "climate_controller[%s]: occupancy store missing, schedule disabled",
                self._name,
            )

        # Следим за управляемыми устройствами: вернувшееся из unavailable
        # устройство приходит в своём собственном (часто устаревшем) состоянии,
        # и его нужно сразу привести к тому, что требует контроллер.
        device_ids = sorted({key.split(":", 1)[1] for key in self._pids})
        if device_ids:
            self._unsub_callbacks.append(
                async_track_state_change_event(
                    self.hass, device_ids, self._handle_device_event
                )
            )

        self._unsub_callbacks.append(
            async_track_time_interval(
                self.hass,
                self._handle_tick,
                dt.timedelta(seconds=TICK_INTERVAL_SECONDS),
            )
        )

        self._unsub_callbacks.append(
            async_track_time_interval(
                self.hass,
                self._handle_pwm_window,
                dt.timedelta(seconds=PWM_WINDOW_SECONDS),
            )
        )
        # Kick off the first PWM window immediately so we don't wait
        # PWM_WINDOW_SECONDS before the first actuation.
        self.hass.async_create_task(self._run_pwm_window())

    async def async_will_remove_from_hass(self) -> None:
        """Tear down listeners on entity removal / entry reload."""
        for unsub in self._unsub_callbacks:
            try:
                unsub()
            except Exception:  # noqa: BLE001 — defensive, listeners are foreign
                _LOGGER.exception("Failed to unsubscribe a listener")
        self._unsub_callbacks.clear()
        for handle in list(self._pwm_off_handles.values()):
            try:
                handle()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Failed to cancel PWM off-handle")
        self._pwm_off_handles.clear()
        await super().async_will_remove_from_hass()

    @callback
    def _handle_sensor_event(self, event: Event) -> None:
        """Sensor state changed — re-evaluate the loop."""
        self._evaluate()
        self.async_write_ha_state()

    @callback
    def _handle_device_event(self, event: Event) -> None:
        """Managed device changed state — reconcile it when it comes back.

        Only the unavailable/unknown/missing → available edge matters. While
        a device is unavailable every service call to it is skipped (see
        ``_set_climate_idle`` & co.), so whatever the controller wanted in the
        meantime was never applied — e.g. a boiler that dropped off during
        idle-zone entry and then returned in its own ``heat`` mode.
        """
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        if old_state is not None and old_state.state not in (
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        ):
            return
        entity_id = event.data.get("entity_id") or new_state.entity_id
        _LOGGER.info(
            "climate_controller[%s]: %s became available (state=%s), reconciling",
            self._name,
            entity_id,
            new_state.state,
        )
        self.hass.async_create_task(self._reconcile_device(entity_id))

    async def _reconcile_device(self, entity_id: str) -> None:
        """Bring a just-returned device in line with the controller state.

        * passive — nothing to do, ``_drive_passive_devices`` steers it every tick;
        * controller OFF — suspend (same as the AUTO→OFF transition);
        * idle zone, or active zone of the opposite side — park in idle;
        * active zone of its own side (or bidir) — re-evaluate right away
          instead of waiting for the next tick.
        """
        if self._first_evaluate:
            # Зона ещё не вычислена (старт HA / reload): первый _evaluate сам
            # разберётся с устройствами, не дёргаем их по дефолтной idle-зоне.
            return
        needs_evaluate = False
        for key in list(self._pids):
            side, key_entity_id = key.split(":", 1)
            if key_entity_id != entity_id or self._is_passive(side, entity_id):
                continue
            if self._hvac_mode == HVACMode.OFF:
                await self._release_device(key, suspend=True)
            elif self._zone == "idle" or (
                side != "bidir" and side != self._zone
            ):
                await self._release_device(key, suspend=False)
            else:
                # Свежий PID-цикл: накопленный за время недоступности
                # интеграл не относится к реальному поведению устройства.
                self._pids[key].reset()
                needs_evaluate = True
        if needs_evaluate:
            self._evaluate()
            self.async_write_ha_state()

    @callback
    def _handle_tick(self, _now) -> None:
        """Periodic tick — re-evaluate even if the sensor didn't change."""
        self.hass.async_create_task(self._async_tick())

    async def _async_tick(self) -> None:
        """One controller tick: occupancy first, then the PID loop.

        Occupancy goes first because an expected→away transition may switch
        the preset or turn the controller off, and the PID pass that follows
        should already work against the new target rather than actuating once
        against the old one.
        """
        await self._process_occupancy()
        self._evaluate()
        self.async_write_ha_state()

    @callback
    def _handle_motion_event(self, event: Event) -> None:
        """A motion sensor fired — react now, don't wait for the next tick.

        Only the off→on edge matters: `on` is what counts both as a learning
        observation for the current slot and as the live trigger that
        outranks the forecast.
        """
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state != "on":
            return
        if self._occupancy is None:
            return
        self._occupancy.note_motion()
        self.hass.async_create_task(self._async_tick())

    async def _process_occupancy(self) -> None:
        """Advance the occupancy schedule and act on a state transition.

        The configured action is applied only *on* a transition, so a manual
        change (different preset, turned off by hand) survives until the next
        expected↔away boundary — and is then overridden, which is what makes
        a schedule a schedule.
        """
        occupancy = self._occupancy
        if occupancy is None or not occupancy.motion_sensors:
            return

        # Learning runs as soon as sensors are configured, whether or not the
        # schedule is allowed to act: a week of watching the grid fill in is
        # exactly how you decide on a threshold before handing it the room.
        now = occupancy.tick()
        if not occupancy.enabled:
            if self._occupancy_state is not None:
                # Switched off mid-session: forget the state so re-enabling it
                # applies the action instead of assuming the entity is already
                # where the schedule wants it.
                self._occupancy_state = None
            return

        state = occupancy.evaluate(now)
        previous = self._occupancy_state
        if state == previous:
            return

        self._occupancy_state = state
        action = occupancy.action_for(
            state, occupancy.expected_action, occupancy.away_action
        )
        _LOGGER.info(
            "climate_controller[%s]: occupancy %s → %s, applying action '%s'",
            self._name,
            previous,
            state,
            action,
        )
        await self._apply_occupancy_action(action)

    async def _apply_occupancy_action(self, action: str) -> None:
        """Run one configured occupancy action against this entity."""
        if action == ACTION_NONE:
            return
        if action == ACTION_OFF:
            await self.async_set_hvac_mode(HVACMode.OFF)
            return
        if action == ACTION_ON:
            await self.async_set_hvac_mode(HVACMode.AUTO)
            return
        if action.startswith(ACTION_PRESET_PREFIX):
            preset = action[len(ACTION_PRESET_PREFIX) :]
            if preset not in PRESET_MODES:
                _LOGGER.warning(
                    "climate_controller[%s]: unknown preset '%s' in occupancy action",
                    self._name,
                    preset,
                )
                return
            # A preset is only meaningful while the controller runs, so the
            # action implies turning it on — otherwise "expected → work" would
            # silently do nothing after an away period that turned it off.
            if self._hvac_mode == HVACMode.OFF:
                await self.async_set_hvac_mode(HVACMode.AUTO)
            await self.async_set_preset_mode(preset)
            return
        _LOGGER.warning(
            "climate_controller[%s]: unknown occupancy action '%s'",
            self._name,
            action,
        )

    def _scheduled_target_for(self, preset_mode: str) -> float | None:
        """If the active preset has at least one schedule point, return the
        interpolated target temperature for the current time. ``None`` otherwise.

        If storage has been corrupted by manual edit (e.g. all entries fail
        to normalize), emit a single ``_LOGGER.warning`` at most once per 60
        seconds per preset and fall back to ``None`` (caller keeps the fixed
        ``preset_temperatures[preset]``).
        """
        schedules = self._config_entry.data.get("preset_schedules", {}) or {}
        raw = schedules.get(preset_mode)
        # Defensive: a non-list (e.g. legacy string from an older PRD draft,
        # which we explicitly do not migrate — option 4B) is treated as empty.
        if not isinstance(raw, list) or not raw:
            return None
        runtime = normalize_points(raw)
        if not runtime:
            now = time.monotonic()
            last = self._scheduled_warning_ts.get(preset_mode, 0.0)
            if now - last >= 60.0:
                self._scheduled_warning_ts[preset_mode] = now
                _LOGGER.warning(
                    "climate_controller[%s]: schedule for preset '%s' has no valid points",
                    self._name,
                    preset_mode,
                )
            return None
        return target_at(runtime, dt.datetime.now().time())

    def _is_passive(self, side: str, entity_id: str) -> bool:
        if side == "bidir":
            cooling_cfg = self._config_entry.data.get("cooling_config", {})
            heating_cfg = self._config_entry.data.get("heating_config", {})
            return bool(
                cooling_cfg.get(entity_id, {}).get("passive", False)
                or heating_cfg.get(entity_id, {}).get("passive", False)
            )
        cfg = self._config_entry.data.get(f"{side}_config", {})
        return bool(cfg.get(entity_id, {}).get("passive", False))

    def _has_active_managed_devices(self) -> bool:
        """Return True if any non-passive registered device is currently in
        an active state (switch on, climate in heat/cool/auto/dry/etc.).

        Used while the controller sits in the idle zone to recover from
        races: if a device was unavailable during _enter_idle_zone and
        later came back online in stale active state, this returns True so
        the caller fires another _enter_idle_zone pass.
        """
        for key in self._pids:
            side, entity_id = key.split(":", 1)
            if self._is_passive(side, entity_id):
                continue
            state = self.hass.states.get(entity_id)
            if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                continue
            domain = entity_id.split(".", 1)[0]
            if domain in ("switch", "input_boolean"):
                if state.state == "on":
                    return True
            elif domain == "climate":
                if state.state not in (HVACMode.OFF, HVACMode.FAN_ONLY):
                    return True
        return False

    def _evaluate(self) -> None:
        """Compute PID output for every active device and dispatch actuation."""
        measured = read_temperature_celsius(self.hass, self._config_entry.data)
        if measured is None:
            _LOGGER.debug(
                "climate_controller[%s]: no usable temperature sensor, skipping eval",
                self._name,
            )
            return

        now = time.monotonic()
        if self._last_eval_ts is None:
            dt_seconds = float(TICK_INTERVAL_SECONDS)
        else:
            dt_seconds = max(now - self._last_eval_ts, 0.001)
        self._last_eval_ts = now

        # Apply per-preset time-of-day schedule if configured. The 0.05°C floor
        # avoids touching state for sub-tick interpolation noise.
        scheduled = self._scheduled_target_for(self._preset_mode)
        if (
            scheduled is not None
            and abs(scheduled - self._target_temperature) >= 0.05
        ):
            self._target_temperature = round(scheduled, 2)

        setpoint = self._target_temperature
        is_off = self._hvac_mode == HVACMode.OFF

        # Passive devices are steered toward the current target on every tick —
        # in AUTO (both active and idle zones) AND while the controller is OFF —
        # and are never physically suspended. This is for always-on equipment
        # (e.g. heating that must keep the room warm even when nobody uses it).
        # Done before the zone machine's early idle-return so idle-zone and OFF
        # ticks still regulate them.
        self._drive_passive_devices(setpoint, measured, dt_seconds)

        # Three-zone state machine with asymmetric hysteresis. Width is
        # `temp_threshold` on entry into active zones (idle → heating /
        # cooling) and 0 on exit (active zones drive until measured hits
        # the exact target, then drop into idle).
        if not is_off:
            threshold = float(self._config_entry.data.get("temp_threshold", 1.0))
            diff = measured - setpoint
            prev_zone = self._zone
            if prev_zone == "heating":
                if diff >= 0:
                    self._zone = "idle"
            elif prev_zone == "cooling":
                if diff <= 0:
                    self._zone = "idle"
            else:  # idle
                if diff < -threshold:
                    self._zone = "heating"
                elif diff > threshold:
                    self._zone = "cooling"
            if self._zone != prev_zone:
                _LOGGER.info(
                    "climate_controller[%s]: zone transition %s → %s (diff=%.2f, threshold=%.2f)",
                    self._name,
                    prev_zone,
                    self._zone,
                    diff,
                    threshold,
                )
            if self._zone == "idle":
                self._hvac_action = HVACAction.IDLE
                # Fire _enter_idle_zone on:
                # - real transition into idle
                # - first tick after entity startup (covers post-restart
                #   stale device state)
                # - any tick where some non-passive device is still in
                #   active state (covers race where a device was
                #   unavailable on a previous _enter_idle_zone pass and
                #   then came back online in stale state, OR a manual
                #   user override that flipped a switch on).
                if (
                    prev_zone != "idle"
                    or self._first_evaluate
                    or self._has_active_managed_devices()
                ):
                    self.hass.async_create_task(self._enter_idle_zone())
                self._first_evaluate = False
                return
            # Active zone — fall through to the PID loop. Set hvac_action
            # immediately so the UI reflects the transition before the
            # actuation tasks complete.
            self._hvac_action = (
                HVACAction.HEATING
                if self._zone == "heating"
                else HVACAction.COOLING
            )
            self._first_evaluate = False

        for key, pid in self._pids.items():
            side, entity_id = key.split(":", 1)
            if self._is_passive(side, entity_id):
                # Passive devices are already regulated in
                # _drive_passive_devices() (unconditionally, incl. OFF). Skip
                # here so their PID isn't advanced twice per tick.
                continue
            if is_off:
                # Non-passive device, controller OFF — stays suspended
                # (physically turned off on the AUTO→OFF transition).
                continue
            # In an active zone we only drive same-side and bidir devices;
            # opposite-side keys were parked when the zone last entered idle
            # (via _enter_idle_zone), and a device that was unavailable at
            # that moment is parked on its return by _reconcile_device.
            if self._zone == "heating" and side == "cooling":
                continue
            if self._zone == "cooling" and side == "heating":
                continue
            output = pid.update(setpoint, measured, dt_seconds)
            _LOGGER.debug(
                "climate_controller[%s]: side=%s device=%s setpoint=%.2f measured=%.2f dt=%.1fs output=%.3f passive=%s",
                self._name,
                side,
                entity_id,
                setpoint,
                measured,
                dt_seconds,
                output,
                self._is_passive(side, entity_id),
            )
            # Deadband: for climate.* targets, skip actuation when |output|
            # is below the configured threshold. switch/input_boolean still
            # go through _update_pwm_fraction so the off-pulse cancellation
            # path keeps working.
            if (
                entity_id.startswith("climate.")
                and abs(output) < ACTUATION_DEADBAND
            ):
                _LOGGER.debug(
                    "climate_controller[%s]: deadband skip side=%s entity=%s output=%.3f",
                    self._name,
                    side,
                    entity_id,
                    output,
                )
                continue
            self.hass.async_create_task(
                self._actuate(side, entity_id, setpoint, output, measured)
            )

    def _drive_passive_devices(
        self, setpoint: float, measured: float, dt_seconds: float
    ) -> None:
        """Regulate every *passive* device toward ``setpoint`` unconditionally.

        Passive devices (``cfg["passive"] = True``) model always-on equipment —
        e.g. heating that must keep the room warm even when the room is unused.
        Unlike normal devices they are:

        * driven by their PID on every tick regardless of ``hvac_mode`` (AUTO
          active zone, AUTO idle zone, and OFF alike), toward the same current
          controller target; and
        * never physically turned off — ``_suspend_active_devices`` and
          ``_enter_idle_zone`` already skip them.

        Side handling is the same as for normal devices: the actuation layer
        maps a heating-side output's negative half to no-op, a cooling-side
        output's positive half to no-op, and a bidir device's sign to hvac_mode.
        """
        for key, pid in self._pids.items():
            side, entity_id = key.split(":", 1)
            if not self._is_passive(side, entity_id):
                continue
            output = pid.update(setpoint, measured, dt_seconds)
            _LOGGER.debug(
                "climate_controller[%s]: passive drive side=%s device=%s "
                "setpoint=%.2f measured=%.2f dt=%.1fs output=%.3f",
                self._name,
                side,
                entity_id,
                setpoint,
                measured,
                dt_seconds,
                output,
            )
            # Same deadband as the main loop: don't nudge climate.* targets for
            # sub-threshold demand (the room is close enough).
            if (
                entity_id.startswith("climate.")
                and abs(output) < ACTUATION_DEADBAND
            ):
                continue
            self.hass.async_create_task(
                self._actuate(side, entity_id, setpoint, output, measured)
            )

    async def _actuate(
        self,
        side: str,
        entity_id: str,
        setpoint: float,
        output: float,
        measured: float,
    ) -> None:
        """Apply the PID output to the configured device."""
        domain = entity_id.split(".", 1)[0]
        if side == "bidir":
            # bidir is only ever registered for climate.* in async_added_to_hass.
            # Defensive bail out for anything else — should not happen.
            if domain == "climate":
                await self._actuate_climate_bidir(entity_id, setpoint, output, measured)
            return
        if domain == "climate":
            await self._actuate_climate(side, entity_id, setpoint, output, measured)
        elif domain in ("switch", "input_boolean"):
            self._update_pwm_fraction(side, entity_id, output)

    @callback
    def _handle_pwm_window(self, _now) -> None:
        """Periodic PWM window tick — start the next on/off cycle for each device."""
        self.hass.async_create_task(self._run_pwm_window())

    async def _run_pwm_window(self) -> None:
        """Start a fresh PWM window for every on/off device.

        For each device we look at the latest computed duty fraction:
        * fraction <= 0 → ensure the device is off; nothing scheduled.
        * fraction >= 1 → ensure the device is on; nothing scheduled.
        * 0 < fraction < 1 → turn on, schedule turn-off at fraction*window.
        """
        for key, fraction in list(self._pwm_fractions.items()):
            await self._start_device_window(key, fraction)

    async def _start_device_window(self, key: str, fraction: float) -> None:
        """Apply one PWM window for a single device."""
        side, entity_id = key.split(":", 1)
        domain = entity_id.split(".", 1)[0]
        # Cancel any pending off-handle from the previous window.
        prev_handle = self._pwm_off_handles.pop(key, None)
        if prev_handle is not None:
            try:
                prev_handle()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Failed to cancel PWM off-handle for %s", key)

        if fraction <= 0.0:
            await self._set_onoff_device(domain, entity_id, on=False)
            return
        if fraction >= 1.0:
            await self._set_onoff_device(domain, entity_id, on=True)
            return

        await self._set_onoff_device(domain, entity_id, on=True)
        on_seconds = max(1.0, fraction * PWM_WINDOW_SECONDS)

        @callback
        def _off_pulse(_now, _key=key, _domain=domain, _entity_id=entity_id):
            self._pwm_off_handles.pop(_key, None)
            self.hass.async_create_task(
                self._set_onoff_device(_domain, _entity_id, on=False)
            )

        self._pwm_off_handles[key] = async_call_later(
            self.hass, on_seconds, _off_pulse
        )

    async def _set_onoff_device(
        self, domain: str, entity_id: str, on: bool
    ) -> None:
        """Call the appropriate turn_on/turn_off service for the device."""
        if domain not in ("switch", "input_boolean"):
            return
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        # Skip the call if the device is already in the desired state — both
        # to avoid log spam and to not generate spurious events.
        if (state.state == "on" and on) or (state.state == "off" and not on):
            return
        await self.hass.services.async_call(
            domain,
            "turn_on" if on else "turn_off",
            {"entity_id": entity_id},
            blocking=False,
        )

    async def _set_climate_off(self, entity_id: str) -> None:
        """Send ``climate.set_hvac_mode hvac_mode=off`` to a climate.* target.

        Used by ``_suspend_active_devices`` on AUTO→OFF transitions. State-aware:

        * Missing / unavailable / unknown — one ``_LOGGER.warning``, no call.
        * Already off — silent skip (idempotent on repeated OFF transitions).
        * Otherwise — fire-and-forget service call.
        """
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            _LOGGER.warning(
                "climate_controller[%s]: %s unavailable, skipping suspend",
                self._name,
                entity_id,
            )
            return
        if state.state == HVACMode.OFF:
            return
        await self.hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": entity_id, "hvac_mode": HVACMode.OFF},
            blocking=False,
        )

    async def _set_climate_idle(self, entity_id: str) -> None:
        """Park a climate.* target in an idle mode while the controller sits
        in the comfort zone. Prefer ``HVACMode.FAN_ONLY`` when the device
        advertises it (`state.attributes.hvac_modes`), otherwise fall back
        to :meth:`_set_climate_off`.

        State-aware, symmetric to ``_set_climate_off``:
        * Missing / unavailable / unknown — one ``_LOGGER.warning``, no call.
        * Already in fan_only — silent skip (idempotent).
        """
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            _LOGGER.warning(
                "climate_controller[%s]: %s unavailable, skipping idle",
                self._name,
                entity_id,
            )
            return
        hvac_modes = state.attributes.get("hvac_modes", []) or []
        if HVACMode.FAN_ONLY in hvac_modes:
            if state.state == HVACMode.FAN_ONLY:
                return
            await self.hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": entity_id, "hvac_mode": HVACMode.FAN_ONLY},
                blocking=False,
            )
            return
        # No FAN_ONLY support → just turn it off.
        await self._set_climate_off(entity_id)

    def _update_pwm_fraction(
        self, side: str, entity_id: str, output: float
    ) -> None:
        """Translate PID output to a PWM duty fraction for an on/off device.

        Sign convention from PID: positive output = call for heat,
        negative = call for cool. For each side we only react to the
        side-relevant half of the range:
        * heating: demand = max(0, output) / OUTPUT_MAX
        * cooling: demand = max(0, -output) / -OUTPUT_MIN
        Off-side outputs (e.g. negative on heating) collapse to fraction 0.
        """
        if side == "heating":
            demand = max(0.0, output)
            scale = OUTPUT_MAX if OUTPUT_MAX > 0 else 1.0
        else:  # cooling
            demand = max(0.0, -output)
            scale = -OUTPUT_MIN if OUTPUT_MIN < 0 else 1.0
        fraction = max(0.0, min(1.0, demand / scale))
        key = f"{side}:{entity_id}"
        prev_fraction = self._pwm_fractions.get(key)
        self._pwm_fractions[key] = fraction
        _LOGGER.debug(
            "climate_controller[%s]: pwm side=%s device=%s fraction=%.3f",
            self._name,
            side,
            entity_id,
            fraction,
        )
        # Mid-window we don't interrupt regulation, but transitions to fully
        # off (0) or fully on (1) should apply immediately so we don't leave
        # a heater running for up to PWM_WINDOW_SECONDS after the demand
        # disappears.
        if (fraction <= 0.0 and prev_fraction != 0.0) or (
            fraction >= 1.0 and prev_fraction != 1.0
        ):
            self.hass.async_create_task(self._start_device_window(key, fraction))

    def _device_max_delta(self, side: str, entity_id: str) -> float:
        """How far this device's set_temperature may sit from the measurement.

        Configured per climate.* device; falls back to
        :data:`DEFAULT_MAX_DEVICE_DELTA_C` when unset or unparseable. A bidir
        device is listed on both sides, and the tighter of the two configured
        values wins — otherwise one side could quietly loosen a limit the
        other side deliberately tightened.
        """
        sides = ("cooling", "heating") if side == "bidir" else (side,)
        limits: list[float] = []
        for one_side in sides:
            device_config = (
                self._config_entry.data.get(f"{one_side}_config") or {}
            ).get(entity_id) or {}
            try:
                value = float(device_config["max_delta"])
            except (KeyError, TypeError, ValueError):
                continue
            if value > 0:
                limits.append(value)
        return min(limits) if limits else DEFAULT_MAX_DEVICE_DELTA_C

    def _compute_climate_target(
        self,
        state,
        setpoint: float,
        output: float,
        measured: float | None = None,
        max_delta: float | None = None,
    ) -> float:
        """Compute the set_temperature value for a climate.* device:
        PID-shifted setpoint, softly clamped to ``measured ± max_delta``,
        then clamped to device min/max, then quantised to the device's
        `target_temp_step` (Tuya IR ACs report step=1.0 and round whatever we
        send anyway — quantising on our side lets the state-aware skip see the
        same value the device will actually hold).

        The ``measured``-anchored clamp is what makes regulation gentle: rather
        than driving the AC to its floor when the room is warm, we never ask for
        a value further than ``max_delta`` from the current reading, so the
        device target glides with the room instead of jumping to an extreme.
        ``max_delta`` comes from the device's own configuration and falls back
        to :data:`DEFAULT_MAX_DEVICE_DELTA_C`.
        """
        limit = DEFAULT_MAX_DEVICE_DELTA_C if max_delta is None else max_delta
        target = setpoint + output
        # Hard, measurement-anchored guarantee (both directions). Applied
        # before the device min/max clamp so the device's own limits still win
        # when they are tighter than the band (e.g. an AC that can't go below
        # 16°C when measured − max_delta would be lower).
        if measured is not None:
            target = max(measured - limit, min(measured + limit, target))
        min_t = state.attributes.get("min_temp")
        max_t = state.attributes.get("max_temp")
        if isinstance(min_t, (int, float)):
            target = max(target, float(min_t))
        if isinstance(max_t, (int, float)):
            target = min(target, float(max_t))

        step = state.attributes.get("target_temp_step")
        try:
            step_f = float(step) if step is not None else 0.1
        except (TypeError, ValueError):
            step_f = 0.1
        if step_f > 0:
            target = round(target / step_f) * step_f
        return round(target, 2)

    async def _ensure_hvac_mode(self, entity_id: str, mode: HVACMode) -> None:
        """Switch a climate.* device into ``mode`` with its own service call.

        ``climate.set_temperature`` carries an ``hvac_mode`` field, but whether
        it is honoured is up to the integration behind the entity — it is
        passed straight through to ``async_set_temperature``. tuya_local, for
        one, reads only preset_mode / temperature / target_temp_{high,low} out
        of the call and drops the mode silently. A device left in ``off`` then
        swallows every setpoint we send while reporting no error at all.

        Going through ``set_hvac_mode`` works regardless of the integration,
        and ``blocking=True`` keeps it ordered ahead of the temperature that
        follows.
        """
        _LOGGER.debug(
            "climate_controller[%s]: setting %s hvac_mode to %s",
            self._name,
            entity_id,
            mode,
        )
        await self.hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": entity_id, "hvac_mode": mode},
            blocking=True,
        )

    async def _actuate_climate(
        self,
        side: str,
        entity_id: str,
        setpoint: float,
        output: float,
        measured: float,
    ) -> None:
        """Drive a climate.* target with PID-shifted set_temperature.

        The PID output is interpreted as a degrees-Celsius shift from the
        controller's setpoint: positive demands more heat, negative demands
        more cool. The shifted target is clamped to the device's own
        min_temp / max_temp from its state attributes.
        """
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            _LOGGER.warning(
                "climate_controller[%s]: %s is unavailable, skipping actuation",
                self._name,
                entity_id,
            )
            return

        target = self._compute_climate_target(
            state, setpoint, output, measured, self._device_max_delta(side, entity_id)
        )

        service_data: dict[str, Any] = {
            "entity_id": entity_id,
            ATTR_TEMPERATURE: target,
        }
        # A device sitting in `off` ignores set_temperature, so flip it to the
        # right mode first.
        force_mode_off_path = state.state == HVACMode.OFF

        if not force_mode_off_path and self._already_at_target(
            entity_id, state, planned_hvac_mode=None, planned_target=target
        ):
            return

        if force_mode_off_path:
            await self._ensure_hvac_mode(
                entity_id, HVACMode.HEAT if side == "heating" else HVACMode.COOL
            )

        await self.hass.services.async_call(
            "climate", "set_temperature", service_data, blocking=False
        )

    async def _actuate_climate_bidir(
        self, entity_id: str, setpoint: float, output: float, measured: float
    ) -> None:
        """Drive a climate.* target that is registered on both cooling and
        heating sides. Sign of PID output picks hvac_mode."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            _LOGGER.warning(
                "climate_controller[%s]: %s is unavailable, skipping bidir actuation",
                self._name,
                entity_id,
            )
            return

        planned_hvac_mode = HVACMode.HEAT if output > 0 else HVACMode.COOL

        target = self._compute_climate_target(
            state,
            setpoint,
            output,
            measured,
            self._device_max_delta("bidir", entity_id),
        )

        service_data: dict[str, Any] = {
            "entity_id": entity_id,
            ATTR_TEMPERATURE: target,
        }

        # If the AC is off (user override) we always re-enable it (option 4A).
        if state.state != HVACMode.OFF and self._already_at_target(
            entity_id,
            state,
            planned_hvac_mode=planned_hvac_mode,
            planned_target=target,
        ):
            return

        if state.state != planned_hvac_mode:
            await self._ensure_hvac_mode(entity_id, planned_hvac_mode)

        await self.hass.services.async_call(
            "climate", "set_temperature", service_data, blocking=False
        )

    def _already_at_target(
        self,
        entity_id: str,
        state,
        planned_hvac_mode: HVACMode | None,
        planned_target: float,
    ) -> bool:
        """Return True if the climate device is already in the requested
        hvac_mode and within temp_threshold of the requested target.

        planned_hvac_mode=None means we are not changing the mode — only the
        temperature has to be close enough to skip.
        """
        current_target = state.attributes.get("temperature")
        try:
            current_target_f = (
                float(current_target) if current_target is not None else None
            )
        except (TypeError, ValueError):
            current_target_f = None
        if current_target_f is None:
            return False

        threshold = float(
            self._config_entry.data.get("temp_threshold", 0.25)
        )
        mode_ok = planned_hvac_mode is None or state.state == planned_hvac_mode
        temp_ok = abs(planned_target - current_target_f) < threshold
        if mode_ok and temp_ok:
            _LOGGER.debug(
                "climate_controller[%s]: set_temperature skipped (already at target) "
                "entity=%s planned_mode=%s current_mode=%s planned_target=%.2f current_target=%.2f threshold=%.2f",
                self._name,
                entity_id,
                planned_hvac_mode,
                state.state,
                planned_target,
                current_target_f,
                threshold,
            )
            return True
        return False

    async def async_turn_on(self) -> None:
        """Generic turn_on — drives external integrations (Yandex Smart Home,
        Google Assistant, HomeKit) that hit the climate.turn_on service."""
        await self.async_set_hvac_mode(HVACMode.AUTO)

    async def async_turn_off(self) -> None:
        """Generic turn_off — drives external integrations that hit the
        climate.turn_off service."""
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        prev = self._hvac_mode
        self._hvac_mode = hvac_mode
        if prev != HVACMode.OFF and hvac_mode == HVACMode.OFF:
            await self._suspend_active_devices()
            # Drop any leftover zone state so the next AUTO start
            # re-evaluates from current measured/setpoint without inheriting
            # a stale active zone.
            self._zone = "idle"
            # Reflect the OFF state on the entity card immediately — without
            # this we'd wait up to TICK_INTERVAL_SECONDS for _evaluate to
            # repaint the ring.
            self._hvac_action = HVACAction.OFF
        elif prev == HVACMode.OFF and hvac_mode != HVACMode.OFF:
            # OFF→AUTO: оценить состояние немедленно, не ждать до 30s тика.
            # _first_evaluate=True re-arm-ит триггер на _enter_idle_zone в
            # _evaluate — иначе zone уже idle (после _suspend_active_devices),
            # _has_active_managed_devices=False, и climate.* не переедет в
            # fan_only до следующей зонной транзиции.
            self._first_evaluate = True
            self._evaluate()
        self.async_write_ha_state()

    async def _suspend_active_devices(self) -> None:
        """On AUTO→OFF transition, release non-passive devices.

        Cancels pending PWM off-pulses, resets PID state, and physically
        turns off non-passive devices: switch/input_boolean targets via
        ``_set_onoff_device(on=False)``, climate.* targets via
        ``_set_climate_off`` (sends ``set_hvac_mode=off``).

        Passive devices (``cfg["passive"] = True``) are observed-only and
        deliberately skipped — see README.md, секция «Флаг passive».

        Iterates over registered PID keys (cooling:/heating:/bidir:) so a
        single bidir device is suspended once, not twice.
        """
        for key in list(self._pids.keys()):
            side, entity_id = key.split(":", 1)
            if self._is_passive(side, entity_id):
                continue
            await self._release_device(key, suspend=True)

    async def _release_device(self, key: str, suspend: bool) -> None:
        """Release one non-passive device: cancel its pending PWM off-pulse,
        reset its PID, drop its duty fraction, then physically turn it off.

        ``suspend=True`` (controller OFF) sends climate.* to ``off``;
        ``suspend=False`` (idle zone) parks climate.* via
        :meth:`_set_climate_idle` (``fan_only`` when supported). Switches /
        input_booleans are turned off either way.
        """
        _side, entity_id = key.split(":", 1)
        handle = self._pwm_off_handles.pop(key, None)
        if handle is not None:
            try:
                handle()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Failed to cancel PWM off-handle for %s", key)
        pid = self._pids.get(key)
        if pid is not None:
            pid.reset()
        self._pwm_fractions.pop(key, None)
        domain = entity_id.split(".", 1)[0]
        if domain in ("switch", "input_boolean"):
            await self._set_onoff_device(domain, entity_id, on=False)
        elif domain == "climate":
            if suspend:
                await self._set_climate_off(entity_id)
            else:
                await self._set_climate_idle(entity_id)

    async def _enter_idle_zone(self) -> None:
        """On entering the idle zone (state machine drops out of an active
        zone after reaching the target, or the room never left the band
        ``[setpoint − threshold, setpoint + threshold]``), park every
        non-passive registered device in idle:

        * ``switch`` / ``input_boolean`` → ``turn_off`` via :meth:`_set_onoff_device`.
        * ``climate.*`` (incl. bidir) → ``fan_only`` via :meth:`_set_climate_idle`,
          which falls back to ``set_hvac_mode=off`` for devices that don't
          support FAN_ONLY.

        Symmetric to :meth:`_suspend_active_devices` (cancels pending PWM
        off-pulses, resets PIDs, clears pwm_fractions) — passive devices
        are observed-only and deliberately untouched.
        """
        for key in list(self._pids.keys()):
            side, entity_id = key.split(":", 1)
            if self._is_passive(side, entity_id):
                continue
            await self._release_device(key, suspend=False)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        if preset_mode not in PRESET_MODES:
            return
        self._preset_mode = preset_mode
        # PRESET_NONE = "no preset": keep whatever target the user last set,
        # and skip schedule lookup (schedules are keyed by named presets only).
        if preset_mode == PRESET_NONE:
            self.async_write_ha_state()
            return
        if preset_mode in self._preset_temperatures:
            self._target_temperature = self._preset_temperatures[preset_mode]
        scheduled = self._scheduled_target_for(preset_mode)
        if scheduled is not None:
            self._target_temperature = round(scheduled, 2)
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is not None:
            self._target_temperature = temperature
            # A manual target set overrides the active preset — drop back to
            # PRESET_NONE so the per-preset schedule (which would otherwise
            # overwrite this value on the next tick) no longer applies.
            self._preset_mode = PRESET_NONE
            self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Occupancy grid services (driven by the Lovelace card)
    # ------------------------------------------------------------------

    async def async_set_occupancy_slot(
        self, day: list[int], hour: list[int], mode: str
    ) -> None:
        """Force the given slots on/off, or hand them back to learning.

        Applied as a rectangle — every listed day × every listed hour — which
        is what a drag across the card selects.
        """
        if self._occupancy is None:
            return
        for one_day in day:
            for one_hour in hour:
                self._occupancy.store.set_override(one_day, one_hour, mode)
        self._occupancy.store.schedule_save()
        _LOGGER.info(
            "climate_controller[%s]: occupancy slots days=%s hours=%s set to '%s'",
            self._name,
            day,
            hour,
            mode,
        )
        await self._async_tick()

    async def async_clear_occupancy_overrides(self) -> None:
        """Drop every manual cell, leaving a purely learned week."""
        if self._occupancy is None:
            return
        dropped = self._occupancy.store.clear_overrides()
        self._occupancy.store.schedule_save()
        _LOGGER.info(
            "climate_controller[%s]: cleared %d occupancy override(s)",
            self._name,
            dropped,
        )
        await self._async_tick()

    async def async_reset_occupancy_learning(self) -> None:
        """Forget the observed history and start learning again.

        Manual cells are deliberately kept: they are the part the user typed
        in, and "relearn the week" is not a request to throw that away.
        """
        if self._occupancy is None:
            return
        self._occupancy.store.reset_learning()
        self._occupancy.store.schedule_save()
        _LOGGER.info(
            "climate_controller[%s]: occupancy learning reset", self._name
        )
        await self._async_tick()
