"""Pressure trends: slope, plateau detection and a leak hint (PLAN.md section 6).

As air diffuses into the balls, canister pressure falls and levels off. A fall
that never levels off may mean a leak. Trends use temperature-compensated gauge
pressure so day/night temperature swings don't look like real changes.

Letting a canister down to maintenance pressure, or topping it up, is a step, not
a trend: the fit starts again after one (``last_step``).

All functions are pure: they take readings as plain sequences and an explicit
``now``, so they are easy to test and independent of the database.
"""

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass

from canister_monitor.compensation import psi_to_kpa

DAY_S = 86_400.0
HOUR_S = 3_600.0
MIN_POINTS = 6
MIN_COVERAGE = 0.5  # readings must span at least half the window

# A change this large between readings this close together is a hand on the valve,
# not diffusion or a leak: 2 psi in 15 min is nearly 200 psi/day, and the sensors
# step by 0.46 psi.
STEP_KPA = psi_to_kpa(2.0)
STEP_WITHIN_S = 15 * 60.0

# Status codes (the UI turns these into words).
INSUFFICIENT = "insufficient_data"
FALLING = "falling"
RISING = "rising"
LEVELLING = "levelling_off"  # flat, but not for plateau_hours yet
LEVELLED = "levelled_off"
LEAK = "leak_suspected"  # still falling leak_hint_after_s after the session start


@dataclass(frozen=True)
class TrendSettings:
    window_s: float
    threshold_kpa_per_day: float
    plateau_s: float
    leak_hint_after_s: float

    @classmethod
    def from_config(cls, trends) -> "TrendSettings":
        """Build from config.TrendsConfig (hours and psi/day)."""
        return cls(
            window_s=trends.window_hours * HOUR_S,
            threshold_kpa_per_day=psi_to_kpa(trends.plateau_threshold_psi_per_day),
            plateau_s=trends.plateau_hours * HOUR_S,
            leak_hint_after_s=trends.leak_hint_after_hours * HOUR_S,
        )


@dataclass(frozen=True)
class Trend:
    status: str
    slope_kpa_per_day: float | None
    # Start of the current flat stretch, if flat. Only looked back plateau_s + 1 h,
    # so for LEVELLED it is a lower bound on how long the canister has been flat.
    levelled_since: float | None
    session_start: float | None
    session_source: str | None  # "fill" event, or "first_reading" as a fallback
    # When the pressure last changed suddenly, if it did: only readings after this
    # were looked at.
    step_ts: float | None = None


def least_squares_slope(ts: Sequence[float], ys: Sequence[float]) -> float | None:
    """Ordinary least-squares slope dy/dt (units of y per second).

    Returns None for fewer than two points or when all timestamps are equal.
    """
    n = len(ts)
    if n < 2 or n != len(ys):
        return None
    mean_t = sum(ts) / n
    mean_y = sum(ys) / n
    sxx = sum((t - mean_t) ** 2 for t in ts)
    if sxx == 0:
        return None
    sxy = sum((t - mean_t) * (y - mean_y) for t, y in zip(ts, ys, strict=True))
    return sxy / sxx


def window_slope(
    ts: Sequence[float], ys: Sequence[float], end: float, window_s: float
) -> float | None:
    """Slope in units/day over readings in (end - window_s, end], or None if they
    are too few or cover too little of the window to be meaningful."""
    sel = [(t, y) for t, y in zip(ts, ys, strict=True) if end - window_s < t <= end]
    if len(sel) < MIN_POINTS or sel[-1][0] - sel[0][0] < MIN_COVERAGE * window_s:
        return None
    slope = least_squares_slope([t for t, _ in sel], [y for _, y in sel])
    return None if slope is None else slope * DAY_S


def last_step(ts: Sequence[float], ys: Sequence[float]) -> float | None:
    """When the pressure last changed suddenly, or None if it didn't.

    A reading is part of a sudden change if it differs by ``STEP_KPA`` or more from
    one up to ``STEP_WITHIN_S`` before it. The time returned is that of the last
    such reading, so it is about ``STEP_WITHIN_S`` after the change itself, by when
    the canister has settled. Readings further apart than that can't tell a release
    from a fast leak, so they are never a step.
    """
    found = None
    i = 0
    for j, (t, y) in enumerate(zip(ts, ys, strict=True)):
        while t - ts[i] > STEP_WITHIN_S:
            i += 1
        before = ys[i:j]
        if before and (max(before) - y >= STEP_KPA or y - min(before) >= STEP_KPA):
            found = t
    return found


def compute_trend(
    ts: Sequence[float],
    gauge_kpa: Sequence[float],
    now: float,
    settings: TrendSettings,
    session_start: float | None = None,
    session_source: str | None = None,
) -> Trend:
    """Classify a sensor's recent pressure history.

    ``ts``/``gauge_kpa`` must be sorted by time and should cover at least
    ``window_s + plateau_s`` before ``now``. ``session_start`` is the latest fill
    (or the first reading) and only matters for the leak hint.

    Only readings after the last sudden change count (``last_step``), so for half
    a window after one there is too little to go on.
    """
    thr = settings.threshold_kpa_per_day
    step = last_step(ts, gauge_kpa)
    if step is not None:
        keep = bisect_right(ts, step)
        ts, gauge_kpa = ts[keep:], gauge_kpa[keep:]
    slope = window_slope(ts, gauge_kpa, now, settings.window_s)
    if slope is None:
        return Trend(INSUFFICIENT, None, None, session_start, session_source, step)

    if abs(slope) < thr:
        # Walk back hour by hour while the trailing-window slope stays flat.
        levelled_since = now
        end = now - HOUR_S
        while now - end <= settings.plateau_s + HOUR_S:
            s = window_slope(ts, gauge_kpa, end, settings.window_s)
            if s is None or abs(s) >= thr:
                break
            levelled_since = end
            end -= HOUR_S
        status = LEVELLED if now - levelled_since >= settings.plateau_s else LEVELLING
        return Trend(status, slope, levelled_since, session_start, session_source, step)

    if slope > 0:
        return Trend(RISING, slope, None, session_start, session_source, step)
    falling_for = None if session_start is None else now - session_start
    status = (
        LEAK if falling_for is not None and falling_for >= settings.leak_hint_after_s else FALLING
    )
    return Trend(status, slope, None, session_start, session_source, step)
