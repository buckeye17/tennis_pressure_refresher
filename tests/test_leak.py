import math
import random
import time

import pytest

from canister_monitor import leak, simulate
from canister_monitor.compensation import compensated_gauge_kpa, psi_to_kpa

ATM = 101.325
DAY, HOUR = leak.DAY_S, leak.HOUR_S
T0 = 1_700_000_000.0


# --- numerics ----------------------------------------------------------------------


def test_solve_and_invert():
    a = [[4.0, 1.0, 0.0], [1.0, 3.0, 1.0], [0.0, 1.0, 2.0]]
    x = leak.solve(a, [1.0, 2.0, 3.0])
    assert [sum(r[j] * x[j] for j in range(3)) for r in a] == pytest.approx([1.0, 2.0, 3.0])
    inv = leak.invert(a)
    for i in range(3):
        for j in range(3):
            assert sum(a[i][k] * inv[k][j] for k in range(3)) == pytest.approx(float(i == j))
    assert leak.solve([[1.0, 2.0], [2.0, 4.0]], [1.0, 2.0]) is None


def test_nelder_mead_finds_quadratic_minimum():
    x, v = leak.nelder_mead(lambda p: (p[0] - 3) ** 2 + 2 * (p[1] + 1) ** 2 + 5, [0, 0], [1, 1])
    assert x == pytest.approx([3, -1], abs=1e-3)
    assert v == pytest.approx(5, abs=1e-6)


# --- one session ---------------------------------------------------------------------


def test_hourly_means():
    ts = [0, 600, 1200, 7300, 7400]  # hours 0, 0, 0, 2, 2 (hour 1 empty)
    ys = [1.0, 2.0, 3.0, 10.0, 20.0]
    mids, means, idx = leak.hourly_means(ts, ys, 0, 8000)
    assert idx == [0, 2]
    assert means == [2.0, 15.0]
    assert mids == [600.0, 7350.0]
    assert leak.hourly_means(ts, ys, 100, 7350)[1] == [2.5, 10.0]  # [start, end)


def exponential(rate, gauge0=None, hours=96, step=300.0):
    gauge0 = psi_to_kpa(50) if gauge0 is None else gauge0
    ts = [T0 + i * step for i in range(int(hours * HOUR / step))]
    return ts, [gauge0 * math.exp(-rate * (t - T0) / DAY) for t in ts]


def test_fit_session_recovers_clean_exponential():
    ts, ys = exponential(0.01)
    fit = leak.fit_session(ts, ys, T0, ts[-1] + 1, weather_sd_kpa=0)
    assert fit.rate_per_day == pytest.approx(0.01, rel=0.01)
    assert fit.se_per_day < 1e-3
    assert fit.hours == 96 - 2  # first two hours skipped while the gas settles


def test_fit_session_ignores_daily_ripple():
    # A daily wave that temperature compensation missed must not change the rate.
    ts, ys = exponential(0.01)
    wavy = [
        y + 2.0 * math.sin(2 * math.pi * (t % DAY) / DAY + 1) for t, y in zip(ts, ys, strict=True)
    ]
    fit = leak.fit_session(ts, wavy, T0, ts[-1] + 1, weather_sd_kpa=0)
    assert fit.rate_per_day == pytest.approx(0.01, rel=0.02)


def test_fit_session_handles_none_and_too_little_data():
    ts, ys = exponential(0.01, hours=96)
    ys = [None if i % 2 else y for i, y in enumerate(ys)]
    assert leak.fit_session(ts, ys, T0, ts[-1] + 1) is not None
    short_ts, short_ys = exponential(0.01, hours=20)
    assert leak.fit_session(short_ts, short_ys, T0, short_ts[-1] + 1) is None
    assert leak.fit_session(ts, [0.0] * len(ts), T0, ts[-1] + 1) is None


def test_weather_widens_the_error():
    ts, ys = exponential(0.01)
    calm = leak.fit_session(ts, ys, T0, ts[-1] + 1, weather_sd_kpa=0)
    windy = leak.fit_session(ts, ys, T0, ts[-1] + 1, weather_sd_kpa=0.8)
    assert windy.rate_per_day == calm.rate_per_day
    # 0.8 kPa of drift over 4 days is worth roughly 0.2 kPa/day of slope.
    assert 0.1 < windy.se_per_day * windy.mean_gauge_kpa < 0.4


def simulated_fit(test, hours, seed, room=simulate.QUIET_ROOM):
    rng = random.Random(seed)
    rows = simulate.leak_test_readings(test, T0, hours * HOUR, rng, room, ATM)
    ts = [r[0] for r in rows]
    gauge = [compensated_gauge_kpa(r[1], r[2], ATM, simulate.REFERENCE_TEMP_C) for r in rows]
    return leak.fit_session(
        ts, gauge, T0, T0 + hours * HOUR + 1, weather_sd_kpa=room.weather_sd_kpa
    )


def test_simulated_leak_test_through_real_decoder():
    fit = simulated_fit(simulate.LeakTest(fill_psi=50, rate_per_day=0.01), 72, seed=3)
    assert fit.rate_per_day == pytest.approx(0.01, rel=0.1)


def test_session_error_matches_actual_error_with_weather():
    # Without the weather term the errors were 3-4x too small (PLAN.md 6.1).
    test = simulate.LeakTest(fill_psi=50, rate_per_day=0.005, ref_gauge_psi=50)
    zs = []
    for seed in range(12):
        fit = simulated_fit(test, 72, seed, simulate.Room())
        zs.append((fit.rate_per_day - 0.005) / fit.se_per_day)
    rms = math.sqrt(sum(z * z for z in zs) / len(zs))
    assert 0.4 < rms < 1.8


# --- simulator leak model ---------------------------------------------------------


@pytest.mark.parametrize("beta", [0.0, -0.3, 0.5])
def test_leak_test_model_obeys_its_equation(beta):
    test = simulate.LeakTest(fill_psi=50, rate_per_day=0.02, beta=beta)
    assert test.ref_gauge_psi_at(0) == pytest.approx(50)
    h = 1e-3
    for days in (0.5, 3.0):
        p = test.ref_gauge_psi_at(days * DAY)
        dpdt = (
            test.ref_gauge_psi_at((days + h) * DAY) - test.ref_gauge_psi_at((days - h) * DAY)
        ) / (2 * h)
        assert -dpdt / p == pytest.approx(0.02 * (p / 30) ** beta, rel=1e-4)


# --- calibration across sessions -----------------------------------------------------


def brute_force_loglik(y, s, eta, tau, n=200_001):
    lo, hi = eta - 10 * tau, eta + 10 * tau
    h = (hi - lo) / (n - 1)
    total = 0.0
    for k in range(n):
        u = lo + k * h
        total += math.exp(-0.5 * (((u - eta) / tau) ** 2 + ((math.exp(u) - y) / s) ** 2))
    return math.log(total * h / (2 * math.pi * tau * s))


@pytest.mark.parametrize(
    "y, s, eta, tau",
    [
        (0.005, 0.001, math.log(0.005), 0.4),  # typical
        (0.005, 0.0001, math.log(0.004), 1.0),  # precise measurement, wide seal spread
        (0.005, 0.003, math.log(0.005), 0.03),  # noisy measurement, tight seals
        (-0.002, 0.003, math.log(0.004), 0.4),  # measured slightly negative
    ],
)
def test_session_loglik_matches_brute_force(y, s, eta, tau):
    assert leak._session_loglik(y, s, eta, tau) == pytest.approx(
        brute_force_loglik(y, s, eta, tau), abs=1e-3
    )


def synthetic_sessions(mu, tau, beta, specs, seed):
    """SessionFits with known truth, skipping the simulator. specs: (psi, se / typical rate)."""
    rng = random.Random(seed)
    out = []
    for psi, rel_se in specs:
        gauge = psi_to_kpa(psi)
        rate = math.exp(mu + beta * math.log(gauge / leak.REF_GAUGE_KPA) + tau * rng.gauss(0, 1))
        se = rel_se * math.exp(mu)
        out.append(leak.SessionFit(0, 1, rate + rng.gauss(0, se), se, gauge, 0, 72, 0))
    return out


def test_calibration_recovers_known_population():
    mu, tau, beta = math.log(0.005), 0.5, -0.3
    cal = leak.fit_calibration(synthetic_sessions(mu, tau, beta, [(20, 0.05), (50, 0.05)] * 15, 4))
    assert cal.beta_fitted
    assert cal.mu == pytest.approx(mu, abs=0.3)
    assert cal.beta == pytest.approx(beta, abs=0.4)
    assert cal.tau == pytest.approx(tau, rel=0.4)
    assert cal.tau_range[0] <= cal.tau <= cal.tau_range[1]
    assert cal.n_sessions == 30


def test_calibration_tells_tight_from_loose_seals():
    specs = [(50, 0.05)] * 8
    tight = leak.fit_calibration(synthetic_sessions(math.log(0.005), 0.02, 0, specs, 5))
    loose = leak.fit_calibration(synthetic_sessions(math.log(0.005), 0.8, 0, specs, 5))
    assert tight.tau < 0.15 < 0.4 < loose.tau
    g = psi_to_kpa(50)
    lo, hi = loose.rate_range(g)
    assert lo < loose.rate_per_day(g) < hi
    tight_lo, tight_hi = tight.rate_range(g)
    assert hi / lo > 2 * tight_hi / tight_lo


def test_calibration_holds_beta_at_zero_for_one_pressure():
    cal = leak.fit_calibration(synthetic_sessions(math.log(0.005), 0.3, 0, [(50, 0.1)] * 4, 6))
    assert not cal.beta_fitted
    assert cal.beta == 0.0
    assert cal.rate_per_day(psi_to_kpa(20)) == cal.rate_per_day(psi_to_kpa(50))


def test_calibration_from_one_noisy_session_says_so():
    cal = leak.fit_calibration(synthetic_sessions(math.log(0.005), 0.3, 0, [(20, 0.6)], 7))
    lo, hi = cal.rate_range(psi_to_kpa(20))
    assert hi / lo > 3


def test_calibration_handles_negative_rates_and_nothing():
    sessions = synthetic_sessions(math.log(0.002), 0.3, 0, [(20, 1.5)] * 4, 8)
    sessions[0] = leak.SessionFit(0, 1, -0.001, 0.003, psi_to_kpa(20), 0, 72, 0)
    assert leak.fit_calibration(sessions) is not None
    assert leak.fit_calibration([]) is None


def test_calibration_is_quick():
    sessions = synthetic_sessions(math.log(0.005), 0.4, -0.3, [(20, 0.5), (50, 0.2)] * 5, 9)
    t = time.perf_counter()
    leak.fit_calibration(sessions)
    assert time.perf_counter() - t < 5
