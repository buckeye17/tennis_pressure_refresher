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
