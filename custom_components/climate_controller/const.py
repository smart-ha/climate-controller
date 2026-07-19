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

# Hard ceiling on how far a climate.* device's set_temperature may deviate
# from the *currently measured* room temperature (in either direction).
# Anchoring the clamp to the measurement — not to our setpoint — does two
# things at once:
#   * softness: instead of slamming the AC to its minimum when the room is
#     warm, the device target glides down alongside the room, staying within
#     this band of the current reading.
#   * a hard guarantee: the value we send never differs from the measured
#     temperature by more than this many degrees, for cooling and heating
#     alike. Prevents the jarring "sensor is 27°C, AC is freezing at 16°C".
MAX_DEVICE_DELTA_C = 4.0
