"""Occupancy schedule: a 24×7 presence grid learned from motion sensors.

Two things live here:

* :class:`OccupancyStore` — the learned week (one hourly slot per cell) plus
  the user's manual overrides, persisted through ``helpers.storage.Store``
  rather than through the config entry. A click on the Lovelace card rewrites
  an override, and ``core.config_entries`` is neither meant for that write
  rate nor safe to rewrite underneath an open OptionsFlow holding a draft.
* :class:`OccupancyController` — the decision layer: slot bookkeeping, the
  preheat lookahead, live-motion handling, and the "is somebody expected right
  now?" answer the climate entity acts on.

Learning model, deliberately simple enough to explain in one sentence: each
slot is observed once a week, the last ``learning_weeks`` observations are
kept, and the slot's score is the share of them in which motion was seen —
2 of the last 4 Mondays at 13:00 is 0.5. At or above the configured threshold
the slot counts as occupied; below it, but with motion somewhere in the
window, the cell is still shown (grey) so the threshold can be judged against
what was actually observed.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
import homeassistant.util.dt as dt_util

from .const import (
    ACTION_NONE,
    ACTIVE_CELLS,
    CELL_AUTO,
    CELL_INACTIVE,
    CELL_MANUAL_OFF,
    CELL_MANUAL_ON,
    CELL_SEEN,
    DAYS_PER_WEEK,
    DEFAULT_AWAY_ACTION,
    DEFAULT_EXPECTED_ACTION,
    DEFAULT_LEARNING_MIN_DAYS,
    DEFAULT_LEARNING_THRESHOLD,
    DEFAULT_LEARNING_WEEKS,
    DEFAULT_MOTION_HOLD_MINUTES,
    DEFAULT_PREHEAT_MINUTES,
    DOMAIN,
    GRID_SLOTS,
    HOURS_PER_DAY,
    OCCUPANCY_AWAY,
    OCCUPANCY_EXPECTED,
    OCCUPANCY_PREHEAT,
    OCCUPANCY_SAVE_DELAY_SECONDS,
    OCCUPANCY_STORAGE_VERSION,
    SLOT_MODE_ACTIVE,
    SLOT_MODE_INACTIVE,
)

_LOGGER = logging.getLogger(__name__)

# Weeks of history kept on disk per slot, regardless of the configured window.
# Raising ``learning_weeks`` later then widens the window over data already
# collected instead of starting the count from scratch.
MAX_HISTORY_WEEKS = 12


def slot_index(day: int, hour: int) -> int:
    """Grid index for a weekday (0 = Monday) and an hour."""
    return day * HOURS_PER_DAY + hour


def slot_of(moment: dt.datetime) -> int:
    """Grid index the given local time falls into."""
    return slot_index(moment.weekday(), moment.hour)


def is_active_cell(cell: int) -> bool:
    """Does this cell mean "a person is expected"?"""
    return cell in ACTIVE_CELLS


class OccupancyStore:
    """Learned motion history + manual slot overrides for one config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store = Store(
            hass, OCCUPANCY_STORAGE_VERSION, f"{DOMAIN}.{entry_id}.occupancy"
        )
        # slot index -> observation string, newest observation last, e.g.
        # "1011" = motion in 3 of the last 4 observed weeks. Kept as a string
        # because it is both compact on disk and readable when debugging the
        # .storage file by hand.
        self._history: dict[int, str] = {}
        # slot index -> True (force expected) / False (force away).
        self._manual: dict[int, bool] = {}
        self.days_observed: int = 0
        self._last_day: str | None = None
        # The slot currently being observed and whether motion has been seen
        # in it yet. Persisted so a restart mid-slot doesn't lose the partial
        # observation.
        self._current_slot: int | None = None
        self._current_seen: bool = False

    # -- persistence ------------------------------------------------------

    async def async_load(self) -> None:
        """Read persisted state. A missing / unreadable file starts empty."""
        data = await self._store.async_load() or {}
        raw_history = data.get("history") or {}
        for key, value in raw_history.items():
            try:
                slot = int(key)
            except (TypeError, ValueError):
                continue
            if 0 <= slot < GRID_SLOTS and isinstance(value, str):
                # Defensive: only 0/1 characters, newest MAX_HISTORY_WEEKS.
                cleaned = "".join(c for c in value if c in "01")
                if cleaned:
                    self._history[slot] = cleaned[-MAX_HISTORY_WEEKS:]

        raw_manual = data.get("manual") or {}
        for key, value in raw_manual.items():
            try:
                slot = int(key)
            except (TypeError, ValueError):
                continue
            if 0 <= slot < GRID_SLOTS and isinstance(value, bool):
                self._manual[slot] = value

        try:
            self.days_observed = int(data.get("days_observed") or 0)
        except (TypeError, ValueError):
            self.days_observed = 0
        last_day = data.get("last_day")
        self._last_day = last_day if isinstance(last_day, str) else None
        current_slot = data.get("current_slot")
        self._current_slot = (
            current_slot
            if isinstance(current_slot, int) and 0 <= current_slot < GRID_SLOTS
            else None
        )
        self._current_seen = bool(data.get("current_seen"))
        _LOGGER.debug(
            "occupancy store loaded: %d learned slots, %d overrides, %d days",
            len(self._history),
            len(self._manual),
            self.days_observed,
        )

    def _snapshot(self) -> dict[str, Any]:
        return {
            "history": {str(k): v for k, v in self._history.items()},
            "manual": {str(k): v for k, v in self._manual.items()},
            "days_observed": self.days_observed,
            "last_day": self._last_day,
            "current_slot": self._current_slot,
            "current_seen": self._current_seen,
        }

    def schedule_save(self) -> None:
        """Queue a debounced write. Called far more often than it writes."""
        self._store.async_delay_save(self._snapshot, OCCUPANCY_SAVE_DELAY_SECONDS)

    # -- learned scores ---------------------------------------------------

    def window(self, slot: int, weeks: int) -> str:
        """The most recent ``weeks`` observations of this slot."""
        if weeks <= 0:
            return ""
        return self._history.get(slot, "")[-weeks:]

    def score(self, slot: int, weeks: int) -> float | None:
        """Share of observed weeks with motion, or ``None`` if never observed.

        The denominator is how many weeks were actually observed, not the
        configured window: a controller two weeks old should act on what it
        has rather than divide by a month it has not lived through yet. The
        ``learning_min_days`` gate is what keeps those early weeks from
        driving anything.
        """
        window = self.window(slot, weeks)
        if not window:
            return None
        return window.count("1") / len(window)

    def samples(self, slot: int, weeks: int) -> int:
        """How many weeks of this slot the score is computed from."""
        return len(self.window(slot, weeks))

    # -- grid -------------------------------------------------------------

    def cell(
        self, slot: int, weeks: int, threshold: float, min_days: int
    ) -> int:
        """Resolve one cell: manual override first, then learning."""
        override = self._manual.get(slot)
        if override is True:
            return CELL_MANUAL_ON
        if override is False:
            return CELL_MANUAL_OFF

        window = self.window(slot, weeks)
        if "1" not in window:
            return CELL_INACTIVE
        if self.days_observed < min_days:
            # Motion was seen, but there is not enough history to call it a
            # habit yet — show it, don't act on it.
            return CELL_SEEN
        ratio = window.count("1") / len(window)
        return CELL_AUTO if ratio >= threshold else CELL_SEEN

    def grid(self, weeks: int, threshold: float, min_days: int) -> list[int]:
        """The whole week as a flat list of 168 cell values."""
        return [
            self.cell(slot, weeks, threshold, min_days)
            for slot in range(GRID_SLOTS)
        ]

    # -- manual overrides -------------------------------------------------

    def set_override(self, day: int, hour: int, mode: str) -> None:
        """Force a slot on/off, or drop the override back to learning."""
        slot = slot_index(day, hour)
        if mode == SLOT_MODE_ACTIVE:
            self._manual[slot] = True
        elif mode == SLOT_MODE_INACTIVE:
            self._manual[slot] = False
        else:  # SLOT_MODE_AUTO
            self._manual.pop(slot, None)

    def clear_overrides(self) -> int:
        """Drop every manual override. Returns how many were dropped."""
        count = len(self._manual)
        self._manual.clear()
        return count

    def reset_learning(self) -> None:
        """Forget the observed history, keeping manual overrides intact."""
        self._history.clear()
        self.days_observed = 0
        self._last_day = None
        self._current_slot = None
        self._current_seen = False

    @property
    def override_count(self) -> int:
        return len(self._manual)

    # -- observation bookkeeping ------------------------------------------

    def record_motion(self) -> None:
        """Mark the slot being observed as having seen motion."""
        self._current_seen = True

    def roll(self, now: dt.datetime) -> bool:
        """Advance slot / day bookkeeping to ``now``.

        Returns True when something was committed, so the caller can queue a
        save. Slots the controller was not running through are simply never
        observed — inventing "no motion" for a period nobody was watching
        would quietly push real habits below the threshold.
        """
        changed = False
        slot = slot_of(now)
        day = now.date().isoformat()

        if self._last_day != day:
            if self._last_day is not None:
                self.days_observed += 1
            self._last_day = day
            changed = True

        if self._current_slot is None:
            self._current_slot = slot
            self._current_seen = False
            return changed

        if self._current_slot != slot:
            history = self._history.get(self._current_slot, "")
            history = (history + ("1" if self._current_seen else "0"))[
                -MAX_HISTORY_WEEKS:
            ]
            self._history[self._current_slot] = history
            _LOGGER.debug(
                "occupancy: slot %d closed with seen=%s, history=%s",
                self._current_slot,
                self._current_seen,
                history,
            )
            self._current_slot = slot
            self._current_seen = False
            changed = True

        return changed


class OccupancyController:
    """Decides whether a person is expected right now, and why.

    Owns no Home Assistant state of its own beyond reading motion sensors;
    the climate entity drives it from its existing tick and applies whatever
    action the state transition calls for.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry,
        store: OccupancyStore,
        name: str,
    ) -> None:
        self.hass = hass
        self._config_entry = config_entry
        self.store = store
        self._name = name
        self._last_motion: dt.datetime | None = None

    # -- configuration accessors -----------------------------------------

    @property
    def config(self) -> dict:
        return self._config_entry.data.get("occupancy") or {}

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled"))

    @property
    def motion_sensors(self) -> list[str]:
        return [
            entity_id
            for entity_id in (self.config.get("motion_sensors") or [])
            if entity_id
        ]

    def _number(self, key: str, default: float) -> float:
        try:
            return float(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    @property
    def learning_weeks(self) -> int:
        return max(1, int(self._number("learning_weeks", DEFAULT_LEARNING_WEEKS)))

    @property
    def learning_threshold(self) -> float:
        return self._number("learning_threshold", DEFAULT_LEARNING_THRESHOLD)

    @property
    def learning_min_days(self) -> int:
        return max(0, int(self._number("learning_min_days", DEFAULT_LEARNING_MIN_DAYS)))

    @property
    def preheat_minutes(self) -> float:
        return max(0.0, self._number("preheat_minutes", DEFAULT_PREHEAT_MINUTES))

    @property
    def motion_hold_minutes(self) -> float:
        return max(0.0, self._number("motion_hold_minutes", DEFAULT_MOTION_HOLD_MINUTES))

    @property
    def expected_action(self) -> str:
        return str(self.config.get("expected_action") or DEFAULT_EXPECTED_ACTION)

    @property
    def away_action(self) -> str:
        return str(self.config.get("away_action") or DEFAULT_AWAY_ACTION)

    @property
    def last_motion(self) -> dt.datetime | None:
        return self._last_motion

    # -- motion -----------------------------------------------------------

    def _motion_now(self) -> bool:
        """Is any configured motion sensor reporting ``on`` right now?"""
        for entity_id in self.motion_sensors:
            state = self.hass.states.get(entity_id)
            if state is not None and state.state == "on":
                return True
        return False

    def note_motion(self, now: dt.datetime | None = None) -> None:
        """Record a motion trigger — from an event, or from the tick's poll."""
        self._last_motion = now or dt_util.now()
        self.store.record_motion()

    def _motion_held(self, now: dt.datetime) -> bool:
        """Motion recently enough to keep the expected state alive."""
        if self._last_motion is None:
            return False
        held = dt.timedelta(minutes=self.motion_hold_minutes)
        return now - self._last_motion <= held

    # -- the grid ---------------------------------------------------------

    def cells(self) -> list[int]:
        return self.store.grid(
            self.learning_weeks, self.learning_threshold, self.learning_min_days
        )

    def grid_by_day(self) -> list[list[int]]:
        """The flat grid reshaped to 7 rows of 24 — what the card consumes."""
        cells = self.cells()
        return [
            cells[day * HOURS_PER_DAY : (day + 1) * HOURS_PER_DAY]
            for day in range(DAYS_PER_WEEK)
        ]

    def scores_by_day(self) -> list[list[int]]:
        """Per-cell score as a percentage, for the card's tooltips.

        ``-1`` marks a slot that has never been observed, which is not the
        same as one observed and never busy.
        """
        weeks = self.learning_weeks
        rows: list[list[int]] = []
        for day in range(DAYS_PER_WEEK):
            row: list[int] = []
            for hour in range(HOURS_PER_DAY):
                score = self.store.score(slot_index(day, hour), weeks)
                row.append(-1 if score is None else round(score * 100))
            rows.append(row)
        return rows

    def _preheat_pending(self, now: dt.datetime, cells: list[int]) -> bool:
        """Is an active slot close enough ahead to start warming up for it?

        Walks the slot boundaries inside the preheat window rather than just
        the next one, so a 3-hour preheat on underfloor heating still finds a
        slot two hours out.
        """
        minutes = self.preheat_minutes
        if minutes <= 0:
            return False
        horizon = now + dt.timedelta(minutes=minutes)
        moment = now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
        while moment <= horizon:
            if is_active_cell(cells[slot_of(moment)]):
                return True
            moment += dt.timedelta(hours=1)
        return False

    # -- the decision -----------------------------------------------------

    def tick(self, now: dt.datetime | None = None) -> dt.datetime:
        """Per-tick bookkeeping: poll sensors, roll the slot, queue a save."""
        now = now or dt_util.now()
        if self._motion_now():
            self.note_motion(now)
        if self.store.roll(now):
            self.store.schedule_save()
        return now

    def evaluate(self, now: dt.datetime) -> str:
        """Current occupancy state: expected / preheat / away."""
        cells = self.cells()
        if is_active_cell(cells[slot_of(now)]):
            return OCCUPANCY_EXPECTED
        # Somebody being home outside the forecast still wants a warm floor —
        # a live trigger outranks the grid.
        if self._motion_held(now):
            return OCCUPANCY_EXPECTED
        if self._preheat_pending(now, cells):
            return OCCUPANCY_PREHEAT
        return OCCUPANCY_AWAY

    @staticmethod
    def action_for(state: str, expected_action: str, away_action: str) -> str:
        """Which configured action a state maps to.

        Preheat is not a third setting: warming up for an upcoming slot is
        exactly the expected state, just entered early.
        """
        if state in (OCCUPANCY_EXPECTED, OCCUPANCY_PREHEAT):
            return expected_action
        if state == OCCUPANCY_AWAY:
            return away_action
        return ACTION_NONE
