"""Constants for the climate controller integration."""

DOMAIN = "climate_controller"

# Default PID gains. Tuned for a slow-thermal room with on/off heaters; the
# integral term contributes most of the steady-state output, derivative
# damps fast sensor jitter.
DEFAULT_KP = 2.0
DEFAULT_KI = 0.05
DEFAULT_KD = 0.5

# PID output range in degrees (interpreted as the demanded delta from the
# controller target). Positive = call for heat, negative = call for cool.
OUTPUT_MIN = -10.0
OUTPUT_MAX = 10.0

# Time-proportional output (PWM) window for switch / input_boolean targets.
# 300s = 5 minutes — faster reaction to zone transitions vs the previous
# 600s, at the cost of more frequent relay toggles.
PWM_WINDOW_SECONDS = 300

# How often the PID loop is evaluated even when no sensor change occurs.
# Lets the integral term advance during quiet sensor periods.
TICK_INTERVAL_SECONDS = 30

# Below this |PID output| (in degrees of demanded delta) climate.* devices
# are not actuated — the room is "close enough" to target. switch/input_boolean
# devices are unaffected (their PWM fraction already collapses to 0 below
# ~OUTPUT_MAX/MIN scale, and the existing off-pulse cancellation handles it).
ACTUATION_DEADBAND = 0.2

# Default ceiling on how far a climate.* device's set_temperature may deviate
# from the *currently measured* room temperature (in either direction).
# Overridable per device: a thermostat driving underfloor heating wants a
# wider band than an AC that reacts within minutes, so each climate.* device
# carries its own ``max_delta`` and falls back to this value.
# Anchoring the clamp to the measurement — not to our setpoint — does two
# things at once:
#   * softness: instead of slamming the AC to its minimum when the room is
#     warm, the device target glides down alongside the room, staying within
#     this band of the current reading.
#   * a hard guarantee: the value we send never differs from the measured
#     temperature by more than this many degrees, for cooling and heating
#     alike. Prevents the jarring "sensor is 27°C, AC is freezing at 16°C".
DEFAULT_MAX_DEVICE_DELTA_C = 4.0

# --- Multi-sensor aggregation --------------------------------------------
# The controller can be fed several temperature sensors at once — one per
# room when it drives a whole-house boiler, for instance. They are collapsed
# into the single "measured" value the PID loop consumes by one of these
# methods:
#   mean   — the house on average; the gentlest on fuel
#   min    — heat until the *coldest* room is satisfied (no room left behind)
#   max    — stop as soon as the warmest room is satisfied (avoids overheating)
#   median — like mean, but a single misplaced sensor cannot skew the loop
AGGREGATION_MEAN = "mean"
AGGREGATION_MIN = "min"
AGGREGATION_MAX = "max"
AGGREGATION_MEDIAN = "median"

TEMPERATURE_AGGREGATIONS = (
    AGGREGATION_MEAN,
    AGGREGATION_MIN,
    AGGREGATION_MAX,
    AGGREGATION_MEDIAN,
)

# Mean reduces to "the reading itself" for a single sensor, so upgrading an
# existing single-sensor setup changes nothing until a second one is added.
DEFAULT_TEMPERATURE_AGGREGATION = AGGREGATION_MEAN

# --- Occupancy schedule (24×7 presence grid) ------------------------------
# The controller can learn *when the room is used* from motion sensors and
# drive itself from that forecast instead of running around the clock. The
# grid is a week of hourly slots: index = weekday * 24 + hour, weekday 0 =
# Monday (matching ``datetime.weekday()``).
HOURS_PER_DAY = 24
DAYS_PER_WEEK = 7
GRID_SLOTS = DAYS_PER_WEEK * HOURS_PER_DAY

# Cell values as published in the ``occupancy_grid`` attribute and read by the
# Lovelace card. A manual override always wins over what learning inferred —
# the point of an override is to state something the sensor cannot know
# ("I work from home on Tuesdays", "never heat the guest room").
CELL_INACTIVE = 0  # no motion ever recorded in this hour
CELL_AUTO = 1  # expected, inferred from motion history (blue in the card)
CELL_MANUAL_ON = 2  # expected, forced on by the user (orange)
CELL_MANUAL_OFF = 3  # forced off by the user even if learning says otherwise
# Motion *was* seen here, but in too few of the observed weeks to count as a
# habit. Rendered grey: "something happened here" is worth showing — it is the
# difference between a slot that never sees anyone and one sitting just under
# the threshold, and it is what you look at when deciding where to put that
# threshold.
CELL_SEEN = 4

# Cells that mean "a person is expected in this hour".
ACTIVE_CELLS = (CELL_AUTO, CELL_MANUAL_ON)

# Override modes accepted by the ``set_occupancy_slot`` service.
SLOT_MODE_AUTO = "auto"  # drop the override, fall back to learning
SLOT_MODE_ACTIVE = "active"
SLOT_MODE_INACTIVE = "inactive"
SLOT_MODES = (SLOT_MODE_AUTO, SLOT_MODE_ACTIVE, SLOT_MODE_INACTIVE)

# Learned history lives in its own Store rather than in the config entry:
# a click on the card rewrites it, and core.config_entries is neither meant
# for that write rate nor safe to rewrite while an OptionsFlow holds a draft.
OCCUPANCY_STORAGE_VERSION = 1
OCCUPANCY_SAVE_DELAY_SECONDS = 30

# Learning window, in weeks. Each slot is observed exactly once a week, so the
# window doubles as the sample size: with the default 4 (a month), a slot that
# saw motion on 2 of those 4 Mondays at 13:00 scores 2/4 = 0.5. Older weeks
# fall out of the window, so a habit that stops decays away on its own.
DEFAULT_LEARNING_WEEKS = 4
# A slot counts as occupied once its score reaches this value. 0.5 with the
# default window = "at least half the recent weeks".
DEFAULT_LEARNING_THRESHOLD = 0.5
# Until this many days have been observed, learned cells stay inactive —
# a single evening of data is not a weekly habit. Manual cells work at once.
DEFAULT_LEARNING_MIN_DAYS = 3

# How early to start heating so the floor is already warm when the person
# arrives. Purely a lookahead over the grid: while any slot inside the window
# is active, the controller is already in the "expected" state.
DEFAULT_PREHEAT_MINUTES = 60

# Motion right now beats the forecast — somebody being home out of schedule
# still wants a warm floor. The expected state is held this long after the
# last trigger.
DEFAULT_MOTION_HOLD_MINUTES = 20

# What the controller does on each transition between the two states.
# ``none`` leaves the entity alone (useful to wire one side by hand).
ACTION_NONE = "none"
ACTION_OFF = "off"
ACTION_ON = "on"
ACTION_PRESET_PREFIX = "preset_"

OCCUPANCY_ACTIONS = (
    ACTION_NONE,
    ACTION_OFF,
    ACTION_ON,
    "preset_work",
    "preset_chill",
    "preset_sleep",
)

DEFAULT_EXPECTED_ACTION = "preset_work"
DEFAULT_AWAY_ACTION = ACTION_OFF

# Runtime occupancy states. ``preheat`` is reported separately from
# ``expected`` so the card and the logbook can tell "warming up for the 7:00
# slot" from "the slot is running", but both drive the expected action.
OCCUPANCY_EXPECTED = "expected"
OCCUPANCY_PREHEAT = "preheat"
OCCUPANCY_AWAY = "away"

DEFAULT_OCCUPANCY_CONFIG = {
    "enabled": False,
    "motion_sensors": [],
    "preheat_minutes": DEFAULT_PREHEAT_MINUTES,
    "motion_hold_minutes": DEFAULT_MOTION_HOLD_MINUTES,
    "learning_weeks": DEFAULT_LEARNING_WEEKS,
    "learning_threshold": DEFAULT_LEARNING_THRESHOLD,
    "learning_min_days": DEFAULT_LEARNING_MIN_DAYS,
    "expected_action": DEFAULT_EXPECTED_ACTION,
    "away_action": DEFAULT_AWAY_ACTION,
}
