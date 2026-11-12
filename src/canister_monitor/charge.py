"""Charge sessions: how a batch's bounce follows the pressure around it, and how long
to keep a canister pumped up before lowering it to maintenance pressure (PLAN.md 6.4).

Everything here works on ``r``: a batch's mean rebound as a share of its new-ball
rebound (1.0 is like new). Air moves through the rubber at a rate proportional to
the pressure difference, so r follows the gauge pressure P around the balls:

    dr/dt = k * (r_eq(P) - r),    r_eq(P) = 1 + per_kpa * (P - maintenance)

At the maintenance pressure the balls settle at their new-ball rebound; above it
they gain, below it (and out of a canister, P = 0) they lose. An hour of play
takes ``play_per_hour`` off r. P is measured, as steps between readings.

``per_kpa`` and ``play_per_hour`` start from configured values and are refitted
from pairs of consecutive bounce tests (``fit``); ``k`` and the maintenance
pressure stay as configured.

Pure functions, like trends.py and bounce.py. Standard library only: this runs on the Pi.
"""

import math
from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

from canister_monitor.compensation import psi_to_kpa

DAY_S = 86_400.0

# A session's pressure counts as "at maintenance" within this much of it: the
# sensors are only good to a psi or two.
MARGIN_KPA = psi_to_kpa(2.0)
# A canister reading this or less has been opened, which ends the sessions in it:
# the sensors read 0 at ambient pressure and step by 0.46 psi.
OPEN_KPA = psi_to_kpa(1.0)
RAISE_BELOW = 0.02  # at maintenance pressure, suggest charging once r is this far under 1
PAIR_SD = 0.012  # error of one test-to-test change in r (about 1.5 cm on a 110 cm rebound)
PRIOR_REL_SD = 0.5  # how far the configured per_kpa is trusted, as a share of itself
LEARN_LIMITS = (0.25, 4.0)  # the fitted per_kpa stays within these multiples of the configured one

# Advice statuses (the dashboard turns these into words).
UNKNOWN = "unknown"  # no estimate: no test, no new-ball value, or no pressure readings
OUT = "out"  # not in a canister: losing pressure
NO_PRESSURE = "no_pressure"  # in a canister, but no fresh reading
CHARGING = "charging"  # elevated pressure, not back to new yet: hold for hold_s
LOWER_NOW = "lower_now"  # elevated pressure and back to new: lower to maintenance
HOLDING = "holding"  # at maintenance pressure, at about new
RAISE = "raise"  # at or below maintenance pressure, but short of new: pump up for charge_s
LOW = "low"  # below maintenance pressure: slowly losing


@dataclass(frozen=True)
class Params:
    k_per_day: float  # how fast balls follow the pressure around them
    per_kpa: float  # r gained per kPa of ball pressure
    per_kpa_sd: float  # how well that is known
    play_per_hour: float  # r lost per hour of play
    maintenance_kpa: float  # gauge pressure at which like-new balls neither gain nor lose
    pairs: int = 0  # test pairs the values were learned from (0: as configured)


def configured(
    uptake_per_day: float,
    rebound_pct_per_psi: float,
    play_loss_pct_per_hour: float,
    maintenance_psi: float,
) -> Params:
    """Starting values, in the units of config.toml's [charge]."""
    per_kpa = rebound_pct_per_psi / 100 / psi_to_kpa(1.0)
    return Params(
        k_per_day=uptake_per_day,
        per_kpa=per_kpa,
        per_kpa_sd=PRIOR_REL_SD * per_kpa,
        play_per_hour=play_loss_pct_per_hour / 100,
        maintenance_kpa=psi_to_kpa(maintenance_psi),
    )


# --- pressure around a batch ---------------------------------------------------------


@dataclass(frozen=True)
class Track:
    """Gauge pressure around a batch as steps: from ``ts[i]`` on it is ``kpa[i]``
    (None: in a canister, but nothing was read). Before the first step it is 0:
    out of a canister."""

    ts: tuple[float, ...] = ()
    kpa: tuple[float | None, ...] = ()

    def at(self, t: float) -> float | None:
        i = bisect_right(self.ts, t) - 1
        return 0.0 if i < 0 else self.kpa[i]


def build_track(
    sessions: Iterable[tuple[float, float | None, Sequence[tuple[float, float]]]],
) -> Track:
    """From a batch's sessions, oldest first and not overlapping: (start, end or None
    while open, readings as (ts, gauge kPa) in time order). A reading holds until the
    next; the first one also covers the time from the session's start."""
    ts: list[float] = []
    kpa: list[float | None] = []
    for start, end, readings in sessions:
        inside = [(t, p) for t, p in readings if t >= start and (end is None or t < end)]
        ts.append(start)
        kpa.append(inside[0][1] if inside else None)
        for t, p in inside:
            if t > ts[-1]:
                ts.append(t)
                kpa.append(p)
        if end is not None:
            ts.append(end)
            kpa.append(0.0)
    return Track(tuple(ts), tuple(kpa))


@dataclass(frozen=True)
class PressureSummary:
    start_kpa: float
    peak_kpa: float
    mean_kpa: float  # weighted by how long each reading held
    end_kpa: float
    readings: int


def summarize_pressure(
    readings: Sequence[tuple[float, float]], until: float
) -> PressureSummary | None:
    """A session's pressure from its readings (ts, gauge kPa), the last one held to ``until``."""
    if not readings:
        return None
    total = weighted = 0.0
    for (t, p), nxt in zip(readings, [*[t for t, _ in readings[1:]], until], strict=True):
        dt = max(nxt - t, 0.0)
        total += dt
        weighted += p * dt
    values = [p for _, p in readings]
    mean = weighted / total if total > 0 else sum(values) / len(values)
    return PressureSummary(values[0], max(values), mean, values[-1], len(values))


# --- following the pressure -----------------------------------------------------------


def _relax(r: float, kpa: float | None, dt: float, params: Params) -> float | None:
    if dt <= 0:
        return r
    if kpa is None:
        return None
    r_eq = 1.0 + params.per_kpa * (kpa - params.maintenance_kpa)
    return r_eq + (r - r_eq) * math.exp(-params.k_per_day * dt / DAY_S)


def follow(
    r0: float,
    t0: float,
    times: Sequence[float],
    track: Track,
    plays: Sequence[tuple[float, float | None]],
    params: Params,
) -> list[float | None]:
    """r at each of ``times`` (ascending, none before ``t0``), starting from ``r0`` at ``t0``.

    ``plays`` are (ts, hours) in time order; those in (t0, t] have happened by t,
    and ones with no hours noted take nothing off. None from the point where the
    batch sat in a canister with no readings.
    """
    i = bisect_right(track.ts, t0) - 1
    kpa = 0.0 if i < 0 else track.kpa[i]
    j = bisect_right([ts for ts, _ in plays], t0)
    r: float | None = r0
    t = t0
    out: list[float | None] = []
    for target in times:
        while r is not None:
            next_step = track.ts[i + 1] if i + 1 < len(track.ts) else math.inf
            next_play = plays[j][0] if j < len(plays) else math.inf
            stop = min(next_step, next_play)
            if stop > target:
                break
            r = _relax(r, kpa, stop - t, params)
            t = stop
            if r is None:
                break
            if next_step <= next_play:
                i += 1
                kpa = track.kpa[i]
            else:
                r -= params.play_per_hour * (plays[j][1] or 0.0)
                j += 1
        if r is not None:
            r = _relax(r, kpa, target - t, params)
            t = target
        out.append(r)
    return out


def estimate_line(
    tests: Sequence[tuple[float, float]],
    now: float,
    track: Track,
    plays: Sequence[tuple[float, float | None]],
    params: Params,
    marks: Iterable[float] = (),
    points: int = 160,
) -> tuple[list[float], list[float | None]]:
    """The estimate of r from the first test to ``now``, for a chart.

    ``tests`` are (ts, r) oldest first. Each test restarts the estimate at what
    was measured, so model error doesn't carry over. ``marks`` are times worth a
    point of their own (a session's start and end, play).
    """
    if not tests or tests[0][0] >= now:
        return [], []
    step = max((now - tests[0][0]) / points, 1.0)
    marks = sorted(marks)
    xs: list[float] = []
    ys: list[float | None] = []
    for n, (t0, r0) in enumerate(tests):
        if t0 >= now:
            break
        # Stop just short of the next test: its own point follows.
        end = min(tests[n + 1][0] - 1.0, now) if n + 1 < len(tests) else now
        if end <= t0:
            continue
        grid = [t0 + step * m for m in range(1, int((end - t0) / step) + 1)]
        times = sorted({*grid, *(m for m in marks if t0 < m < end), end})
        xs.append(t0)
        ys.append(r0)
        xs.extend(times)
        ys.extend(follow(r0, t0, times, track, plays, params))
    return xs, ys


# --- learning from tests ----------------------------------------------------------------


@dataclass(frozen=True)
class Pair:
    """Two consecutive tests of a batch, and what pushed r between them:
    r1 = 1 + (r0 - 1) * exp(-k T) + per_kpa * x - play_per_hour * h."""

    r0: float
    t0: float
    r1: float
    t1: float
    x: float  # kPa: the pressure's push, per unit of per_kpa
    h: float  # hours of play, less what has since been made up


def pair(
    r0: float,
    t0: float,
    r1: float,
    t1: float,
    track: Track,
    plays: Sequence[tuple[float, float | None]],
    params: Params,
) -> Pair | None:
    """None if the pair can't teach anything: no time between the tests, a canister
    with no readings, or play with no hours noted."""
    if t1 <= t0 or any(hours is None for ts, hours in plays if t0 < ts <= t1):
        return None
    x = follow(1.0, t0, [t1], track, plays, replace(params, per_kpa=1.0, play_per_hour=0.0))[0]
    h = follow(1.0, t0, [t1], track, plays, replace(params, per_kpa=0.0, play_per_hour=1.0))[0]
    if x is None or h is None:
        return None
    return Pair(r0, t0, r1, t1, x - 1.0, 1.0 - h)


def fit(pairs: Sequence[Pair], prior: Params) -> Params:
    """Refit ``per_kpa`` and ``play_per_hour`` from test pairs, starting from ``prior``.

    Least squares, with the configured values as a prior: per_kpa known to
    +-``PRIOR_REL_SD`` of itself, play_per_hour to +-itself. So with no pairs the
    configured values come back, and each pair moves them by as much as it can
    tell (a short stay at low pressure says little). per_kpa stays within
    ``LEARN_LIMITS`` of the configured value, and play can't add bounce.
    """
    if not pairs:
        return replace(prior, pairs=0)
    k = prior.k_per_day / DAY_S
    sd_b = PRIOR_REL_SD * prior.per_kpa
    sd_g = max(prior.play_per_hour, 1e-9)
    a11, a12, a22 = 1 / sd_b**2, 0.0, 1 / sd_g**2
    e1, e2 = prior.per_kpa / sd_b**2, prior.play_per_hour / sd_g**2
    w = 1 / PAIR_SD**2
    for p in pairs:
        y = p.r1 - 1.0 - (p.r0 - 1.0) * math.exp(-k * (p.t1 - p.t0))
        a11 += w * p.x * p.x
        a12 -= w * p.x * p.h
        a22 += w * p.h * p.h
        e1 += w * p.x * y
        e2 -= w * p.h * y
    det = a11 * a22 - a12 * a12
    per_kpa = (a22 * e1 - a12 * e2) / det
    play = (a11 * e2 - a12 * e1) / det
    sd = math.sqrt(a22 / det)
    if play < 0:  # play can't add bounce: refit per_kpa with it at 0
        play, per_kpa, sd = 0.0, e1 / a11, math.sqrt(1 / a11)
    lo, hi = (m * prior.per_kpa for m in LEARN_LIMITS)
    return replace(
        prior,
        per_kpa=min(max(per_kpa, lo), hi),
        per_kpa_sd=sd,
        play_per_hour=play,
        pairs=len(pairs),
    )


# --- advice -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Advice:
    status: str
    # CHARGING: seconds to keep the canister at its current pressure before lowering
    # it to maintenance (None if that pressure would never get the balls back to new).
    hold_s: float | None = None
    # Seconds it would take at the configured charge pressure (0: at new already).
    charge_s: float | None = None
    drift_per_day: float | None = None  # how fast r is changing right now


def time_to_new(r: float, kpa: float, params: Params) -> float | None:
    """Seconds at a steady gauge pressure until r is back to 1: 0 if it already is,
    None if that pressure would never get it there."""
    if r >= 1.0:
        return 0.0
    r_eq = 1.0 + params.per_kpa * (kpa - params.maintenance_kpa)
    if r_eq <= 1.0 + 1e-9:
        return None
    return math.log((r_eq - r) / (r_eq - 1.0)) / params.k_per_day * DAY_S


def advise(
    r: float | None, kpa: float | None, in_canister: bool, params: Params, charge_kpa: float
) -> Advice:
    """What to do with a batch estimated at ``r``. ``kpa`` is the gauge pressure in its
    canister now (None: no fresh reading); ignored when it isn't in one."""
    if r is None:
        return Advice(UNKNOWN)
    charge_s = time_to_new(r, charge_kpa, params)

    def drift(p: float) -> float:
        return params.k_per_day * (1.0 + params.per_kpa * (p - params.maintenance_kpa) - r)

    if not in_canister:
        return Advice(OUT, None, charge_s, drift(0.0))
    if kpa is None:
        return Advice(NO_PRESSURE, None, charge_s)
    if kpa > params.maintenance_kpa + MARGIN_KPA:
        if r >= 1.0:
            return Advice(LOWER_NOW, 0.0, charge_s, drift(kpa))
        return Advice(CHARGING, time_to_new(r, kpa, params), charge_s, drift(kpa))
    if r < 1.0 - RAISE_BELOW:
        return Advice(RAISE, None, charge_s, drift(kpa))
    if kpa < params.maintenance_kpa - MARGIN_KPA:
        return Advice(LOW, None, charge_s, drift(kpa))
    return Advice(HOLDING, None, charge_s, drift(kpa))
