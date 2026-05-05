"""Time-of-day temperature schedule for climate_controller presets.

Pure stdlib helpers — no Home Assistant imports, no logging, no I/O. The
storage format is a list of structured points, e.g.
``[{"time": "21:00", "temp": 20.0}, {"time": "06:00", "temp": 24.0}]``.
"""
from __future__ import annotations

import datetime


def normalize_points(points) -> list[tuple[datetime.time, float]]:
    """Normalize a stored points list into runtime tuples ``[(time, float), ...]``.

    Accepts:
      * list of dicts with ``time`` / ``temp`` keys
      * list of ``(time, temp)`` sequences (already-normalized tuples too)

    Empty / None / non-list input returns ``[]``. Individual entries that
    fail to parse are silently skipped — the caller (UI) is responsible
    for user-visible error reporting.
    """
    if not points or not isinstance(points, list):
        return []
    out: list[tuple[datetime.time, float]] = []
    for p in points:
        if isinstance(p, dict):
            t_raw = p.get("time")
            v_raw = p.get("temp")
        elif isinstance(p, (list, tuple)) and len(p) >= 2:
            t_raw, v_raw = p[0], p[1]
        else:
            continue
        if t_raw is None or v_raw is None:
            continue
        try:
            if isinstance(t_raw, datetime.time):
                t = t_raw
            else:
                t = datetime.time.fromisoformat(str(t_raw))
            v = float(v_raw)
        except (ValueError, TypeError):
            continue
        out.append((t, v))
    return out


def _to_seconds(t: datetime.time) -> int:
    return t.hour * 3600 + t.minute * 60 + t.second


def target_at(schedule, now: datetime.time) -> float | None:
    """Return the interpolated temperature for ``now``.

    ``schedule`` may be a raw stored list (dicts / sequences) or an
    already-normalized list of ``(time, float)`` tuples — both are
    handled via :func:`normalize_points`.

    Empty schedule returns ``None``; a single point returns its
    temperature; otherwise linear interpolation between adjacent points,
    cyclic over 24 hours through midnight.
    """
    pts = normalize_points(schedule)
    if not pts:
        return None
    if len(pts) == 1:
        return float(pts[0][1])

    pts = sorted(pts, key=lambda p: _to_seconds(p[0]))
    secs = [_to_seconds(p) for p, _ in pts]
    temps = [float(v) for _, v in pts]
    n = len(pts)
    now_s = _to_seconds(now)

    # Exact-match the last point so we don't roll into the wrap-around.
    if now_s == secs[-1]:
        return temps[-1]

    for i in range(n - 1):
        if secs[i] <= now_s < secs[i + 1]:
            span = secs[i + 1] - secs[i]
            frac = (now_s - secs[i]) / span if span else 0.0
            return temps[i] + (temps[i + 1] - temps[i]) * frac

    # Wrap-around segment: last point -> first point through midnight.
    span = (86400 - secs[-1]) + secs[0]
    if now_s >= secs[-1]:
        elapsed = now_s - secs[-1]
    else:  # now_s < secs[0]
        elapsed = (86400 - secs[-1]) + now_s
    frac = elapsed / span if span else 0.0
    return temps[-1] + (temps[0] - temps[-1]) * frac
