import math
import random

import pytest

from canister_monitor import simulate, trends
from canister_monitor.compensation import compensated_gauge_kpa, psi_to_kpa
from canister_monitor.config import TrendsConfig
from canister_monitor.decoders.fbb0_ac00 import Fbb0Ac00Decoder

DAY, HOUR = trends.DAY_S, trends.HOUR_S
ATM = 101.325
SETTINGS = trends.TrendSettings.from_config(TrendsConfig())  # 24 h, 0.3 psi/day, 12 h, 96 h


def series(fn, start, end, step=300.0):
    """Sample fn(t) -> gauge kPa every ``step`` seconds."""
    ts = [start + i * step for i in range(int((end - start) / step) + 1)]
    return ts, [fn(t) for t in ts]


# --- slope math ------------------------------------------------------------------


def test_least_squares_exact_line():
    ts = [0.0, 10.0, 20.0, 30.0]
    assert trends.least_squares_slope(ts, [5 + 2 * t for t in ts]) == pytest.approx(2.0)
    assert trends.least_squares_slope(ts, [7.0] * 4) == 0.0


def test_least_squares_hand_calculated():
    # x = 0,1,2,3  y = 1,3,2,5: sxy = 5.5, sxx = 5 -> 1.1
    assert trends.least_squares_slope([0, 1, 2, 3], [1, 3, 2, 5]) == pytest.approx(1.1)


def test_least_squares_degenerate():
    assert trends.least_squares_slope([], []) is None
    assert trends.least_squares_slope([1.0], [2.0]) is None
    assert trends.least_squares_slope([5.0, 5.0, 5.0], [1.0, 2.0, 3.0]) is None
    assert trends.least_squares_slope([1.0, 2.0], [1.0]) is None


def test_least_squares_recovers_slope_from_noise():
    rng = random.Random(7)
    ts = [float(i) for i in range(2000)]
    ys = [0.25 * t + rng.gauss(0, 5) for t in ts]
    assert trends.least_squares_slope(ts, ys) == pytest.approx(0.25, abs=0.01)


def test_window_slope_units_and_window():
    # -1.5 psi/day, but only the last day is looked at.
    rate = psi_to_kpa(-1.5) / DAY
    ts, ys = series(lambda t: 200 + rate * t, 0, 3 * DAY)
    assert trends.window_slope(ts, ys, 3 * DAY, DAY) == pytest.approx(psi_to_kpa(-1.5))


def test_window_slope_needs_points_and_coverage():
    ts, ys = series(lambda t: 100.0, 0, 5 * HOUR)  # 5 h of data, 24 h window
    assert trends.window_slope(ts, ys, 5 * HOUR, DAY) is None  # covers < half the window
    assert trends.window_slope([0.0, DAY], [1.0, 2.0], DAY, 2 * DAY) is None  # too few points


# --- classification ------------------------------------------------------------------


def test_insufficient_data():
    ts, ys = series(lambda t: 150.0, 0, 6 * HOUR)
    assert trends.compute_trend(ts, ys, 6 * HOUR, SETTINGS).status == trends.INSUFFICIENT
    assert trends.compute_trend([], [], 0.0, SETTINGS).status == trends.INSUFFICIENT


def test_flat_for_long_enough_is_levelled_off():
    now = 3 * DAY
    ts, ys = series(lambda t: 150.0, 0, now)
    t = trends.compute_trend(ts, ys, now, SETTINGS)
    assert t.status == trends.LEVELLED
    assert t.slope_kpa_per_day == pytest.approx(0.0, abs=1e-9)
    assert now - t.levelled_since >= SETTINGS.plateau_s


def test_recently_flat_is_levelling_off():
    # Exponential decay that just got under the threshold: flat now, but not for 12 h.
    tau = 10 * HOUR
    delta = psi_to_kpa(10)

    def decay(t):
        return 150 + delta * math.exp(-t / tau)

    # Find when the 24 h slope first drops under 0.3 psi/day, then look 3 h later.
    ts, ys = series(decay, 0, 5 * DAY)
    first_flat = next(
        end
        for end in range(int(DAY), int(5 * DAY), int(HOUR))
        if abs(trends.window_slope(ts, ys, end, DAY)) < SETTINGS.threshold_kpa_per_day
    )
    t = trends.compute_trend(ts, ys, first_flat + 3 * HOUR, SETTINGS)
    assert t.status == trends.LEVELLING
    assert first_flat + 3 * HOUR - t.levelled_since < SETTINGS.plateau_s
    later = trends.compute_trend(ts, ys, first_flat + 14 * HOUR, SETTINGS)
    assert later.status == trends.LEVELLED


def test_falling_then_leak_hint_after_session_age():
    rate = psi_to_kpa(-1.5) / DAY
    ts, ys = series(lambda t: 250 + rate * t, 0, 6 * DAY)
    early = trends.compute_trend(ts, ys, 2 * DAY, SETTINGS, session_start=0.0)
    assert early.status == trends.FALLING
    assert early.slope_kpa_per_day == pytest.approx(psi_to_kpa(-1.5))
    late = trends.compute_trend(ts, ys, 5 * DAY, SETTINGS, session_start=0.0)
    assert late.status == trends.LEAK
    # A recent fill resets the clock: no leak hint yet.
    refilled = trends.compute_trend(ts, ys, 5 * DAY, SETTINGS, session_start=4 * DAY)
    assert refilled.status == trends.FALLING
    # Without any session start there's nothing to measure "how long" against.
    assert trends.compute_trend(ts, ys, 5 * DAY, SETTINGS).status == trends.FALLING


def test_rising():
    rate = psi_to_kpa(2.0) / DAY
    ts, ys = series(lambda t: 100 + rate * t, 0, 2 * DAY)
    assert trends.compute_trend(ts, ys, 2 * DAY, SETTINGS).status == trends.RISING


# --- a hand on the valve ------------------------------------------------------------------

# Canister 1 on 2026-11-11: pumped up, let down to maintenance pressure 7 h later, then
# left alone. Seconds after the fill and compensated gauge psi at each change in the
# reading; in between, the same reading came again every 200 s or so.
RELEASED_AT = 25670.0
LET_DOWN = [
    (0, 33.01),
    (314, 32.39),
    (2538, 31.93),
    (21525, 32.09),
    (25670, 23.31),
    (25679, 15.91),
    (25688, 13.60),
    (25697, 12.22),
    (25706, 11.76),
    (25724, 12.22),
    (65286, 12.31),
    (71307, 12.41),
    (71909, 12.31),
]


def held(changes, until, every=200.0):
    """Readings from (seconds, psi) changes, each repeated ``every`` seconds until the next."""
    ts, ys = [], []
    for (t, psi), nxt in zip(changes, [*[t for t, _ in changes[1:]], until], strict=True):
        while t < nxt:
            ts.append(float(t))
            ys.append(psi_to_kpa(psi))
            t += every
    return ts, ys


def test_last_step_is_the_end_of_a_sudden_change():
    ts, ys = held(LET_DOWN, RELEASED_AT + 13 * HOUR)
    step = trends.last_step(ts, ys)
    # The last reading within 15 min of one from before or during the release.
    assert 0 < step - RELEASED_AT <= trends.STEP_WITHIN_S + 60
    assert trends.last_step(ts[:100], ys[:100]) is None  # before it: settling after the fill
    assert trends.last_step([], []) is None


def test_last_step_ignores_leaks_sensor_steps_and_gaps():
    quantum = psi_to_kpa(0.46)
    rate = psi_to_kpa(-5.0) / DAY  # a bad leak, read in the sensors' 0.46 psi steps
    ts, ys = series(lambda t: round((200 + rate * t) / quantum) * quantum, 0, 2 * DAY)
    assert trends.last_step(ts, ys) is None
    # Half an hour between readings: a release or a fast leak, there's no telling.
    assert trends.last_step([0.0, 1800.0, 3600.0], [200.0, 80.0, 80.0]) is None


def test_letting_a_canister_down_is_not_a_fall():
    # 13 h after the release the dashboard said "Still dropping, 31 psi/day".
    now = RELEASED_AT + 13.2 * HOUR
    ts, ys = held(LET_DOWN, now)
    whole = trends.window_slope(ts, ys, now, SETTINGS.window_s)
    assert whole == pytest.approx(psi_to_kpa(-31), rel=0.1)  # what counting the step gives

    t = trends.compute_trend(ts, ys, now, SETTINGS, session_start=0.0)
    assert t.status == trends.LEVELLING
    assert abs(t.slope_kpa_per_day) < SETTINGS.threshold_kpa_per_day
    assert 0 < t.step_ts - RELEASED_AT <= trends.STEP_WITHIN_S + 60

    # Too soon after it to say anything, and the trend says when it started again.
    soon = RELEASED_AT + 6 * HOUR
    early = trends.compute_trend(*held(LET_DOWN[:10], soon), soon, SETTINGS, session_start=0.0)
    assert early.status == trends.INSUFFICIENT
    assert early.step_ts == t.step_ts

    # Left alone for a day and a half, it has levelled off.
    later = RELEASED_AT + 36 * HOUR
    settled = trends.compute_trend(*held(LET_DOWN, later), later, SETTINGS, session_start=0.0)
    assert settled.status == trends.LEVELLED


def test_topping_up_is_not_a_rise():
    top_up = 2 * DAY
    changes = [(0, 12.2), (top_up, 21.0), (top_up + 9, 30.4), (top_up + 18, 30.0)]
    now = top_up + 13 * HOUR
    t = trends.compute_trend(*held(changes, now), now, SETTINGS)
    assert t.status == trends.LEVELLING
    assert t.slope_kpa_per_day == pytest.approx(0.0, abs=1e-6)
    soon = top_up + 3 * HOUR
    assert trends.compute_trend(*held(changes, soon), soon, SETTINGS).status == trends.INSUFFICIENT


def test_no_step_leaves_the_trend_as_it_was():
    ts, ys = series(lambda t: 150.0, 0, 3 * DAY)
    assert trends.compute_trend(ts, ys, 3 * DAY, SETTINGS).step_ts is None


def test_settings_from_config_converts_units():
    s = trends.TrendSettings.from_config(
        TrendsConfig(window_hours=6, plateau_threshold_psi_per_day=1)
    )
    assert s.window_s == 6 * HOUR
    assert s.threshold_kpa_per_day == pytest.approx(6.894757)


# --- acceptance: the simulator's canisters -----------------------------------------------


def simulated_compensated_series(days: float):
    """Run the simulator and decode its adverts exactly as the collector would,
    keeping one reading per new payload (as the write policy does)."""
    start = 1_000_000.0
    sim = simulate.Simulator(simulate.DEFAULT_SENSORS, start_ts=start, seed=11)
    decoder = Fbb0Ac00Decoder()
    out = {s.mac: ([], []) for s in simulate.DEFAULT_SENSORS}
    last_payload = {}
    ts = start
    while ts < start + days * DAY:
        ts += simulate.STEP_S
        for adv in sim.step(ts):
            payload = adv.manufacturer_data[0x00AC]
            if last_payload.get(adv.mac) == payload:
                continue
            last_payload[adv.mac] = payload
            r = decoder.decode(adv, ATM)
            out[adv.mac][0].append(r.ts)
            out[adv.mac][1].append(compensated_gauge_kpa(r.pressure_kpa_abs, r.temp_c, ATM, 20.0))
    return start, out


def test_simulator_leak_is_flagged_and_healthy_canisters_level_off():
    start, data = simulated_compensated_series(days=7)
    now = start + 7 * DAY
    leak_mac = simulate.DEFAULT_SENSORS[3].mac
    for mac, (ts, ys) in data.items():
        t = trends.compute_trend(ts, ys, now, SETTINGS, session_start=start)
        if mac == leak_mac:
            assert t.status == trends.LEAK, mac
            assert t.slope_kpa_per_day == pytest.approx(psi_to_kpa(-1.5), abs=psi_to_kpa(0.3))
        else:
            assert t.status == trends.LEVELLED, (mac, t)


def test_simulator_early_on_everything_is_still_falling():
    start, data = simulated_compensated_series(days=1.5)
    for ts, ys in data.values():
        t = trends.compute_trend(ts, ys, start + 1.5 * DAY, SETTINGS, session_start=start)
        assert t.status == trends.FALLING  # settling, too early for a leak hint
