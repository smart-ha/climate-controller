"""Plain-Python PID controller used by climate_controller.

Sign convention: positive output means "call for heat" (measured below
setpoint), negative means "call for cool". Output is clamped to
[output_min, output_max]. The caller decides how to translate that scalar
into device-side service calls (set_temperature shift, PWM duty cycle, etc).

No imports from homeassistant.* — keep this module pure so it can be
reasoned about and tested in isolation.
"""
from __future__ import annotations


class PidController:
    """Positional-form PID with conditional anti-windup."""

    def __init__(
        self,
        kp: float,
        ki: float,
        kd: float,
        output_min: float,
        output_max: float,
    ) -> None:
        if output_min >= output_max:
            raise ValueError("output_min must be < output_max")
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.output_min = output_min
        self.output_max = output_max
        self._integral = 0.0
        self._last_error: float | None = None

    def reset(self) -> None:
        """Zero the integral and forget the last error."""
        self._integral = 0.0
        self._last_error = None

    def update(self, setpoint: float, measured: float, dt: float) -> float:
        """Compute the next output for the given measurement.

        ``dt`` is the elapsed seconds since the previous update. Callers must
        pass a positive value; non-positive ``dt`` is clamped to a tiny
        positive number so the derivative term doesn't divide by zero.
        """
        if dt <= 0:
            dt = 1e-3

        error = setpoint - measured

        # Tentatively advance the integral, then decide whether to keep the
        # advance based on saturation (conditional anti-windup).
        tentative_integral = self._integral + error * dt

        if self._last_error is None:
            derivative = 0.0
        else:
            derivative = (error - self._last_error) / dt

        unclamped = (
            self.kp * error
            + self.ki * tentative_integral
            + self.kd * derivative
        )

        if unclamped > self.output_max:
            output = self.output_max
            saturated_high = True
            saturated_low = False
        elif unclamped < self.output_min:
            output = self.output_min
            saturated_high = False
            saturated_low = True
        else:
            output = unclamped
            saturated_high = False
            saturated_low = False

        # Anti-windup: only commit the integral advance if it isn't pushing
        # further into the saturated region. (error > 0 grows integral; if we
        # are already saturated_high, that growth is unhelpful and we keep
        # the previous integral value.)
        push_high = error > 0 and saturated_high
        push_low = error < 0 and saturated_low
        if not (push_high or push_low):
            self._integral = tentative_integral

        self._last_error = error
        return output
