"""Leak-rate calibration from empty-canister leak tests (PLAN.md section 6.1).

A leak test is a canister filled with no balls in it, so the only way air can
leave is a leak. Each test gives one estimate of the leak rate; several tests,
each with the sensor and lid freshly re-seated, show how much the seals vary
from one fill to the next.

Leak model. The fractional leak rate (share of gauge pressure lost per day) is

    rate(P) = exp(mu + beta * ln(P / REF_GAUGE_KPA) + u),   u ~ Normal(0, tau^2)

``mu`` is the typical log rate at the reference pressure, ``beta`` how the
rate changes with pressure (0: leak flow proportional to gauge pressure, as
for permeation; about -0.3 for flow through a small orifice, which follows
absolute pressure), and ``tau`` the spread between sessions (seal
re-seating). ``beta`` can only be fitted from tests at clearly different
pressures, e.g. both maintain (~20 psi) and recharge (~50 psi) fills.

All functions are pure, like trends.py.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

from canister_monitor.compensation import psi_to_kpa

DAY_S = 86_400.0
HOUR_S = 3_600.0
REF_GAUGE_KPA = psi_to_kpa(30.0)  # between maintain (~20 psi) and recharge (~50 psi)
SETTLE_S = 2 * HOUR_S  # skip this long after a fill: the gas cools after being pumped
MIN_HOURS = 20  # hourly bins a session needs before it is fitted
MAX_RHO = 0.95
# Weather. The sensors read gauge pressure, so a change in atmospheric pressure
# shows up as an equal and opposite change in the reading. Over a few days that
# looks just like a slope, and nothing in one session's data can tell the two
# apart, so its effect is added to each session's error from typical figures:
# mid-latitude barometric pressure varies by about 0.8 kPa (sd), with weather
# systems lasting a day or two. Set the sd to 0 if a barometer corrects for it.
WEATHER_SD_KPA = 0.8
WEATHER_TAU_H = 36.0
BETA_PRIOR_SD = 1.0  # weak prior that keeps beta physical when tests barely span pressures
BETA_MIN_PRESSURE_RATIO = 1.5  # sessions must span this much pressure to fit beta
# Seal spread values tried, from "identical every time" to "varies tenfold".
TAU_GRID = [0.02 * (150 ** (i / 24)) for i in range(25)]  # 0.02 ... 3.0
# Half-Cauchy prior on tau. In simulation (tools/leak_sim.py) predictions
# covered close to the intended 68% of new sessions for tau 0.4-0.8 and erred
# wide (85-98%) for tight seals; a uniform prior erred wide throughout.
TAU_PRIOR_SCALE = 0.5
GRID_POINTS = 81


@dataclass(frozen=True)
class SessionFit:
    """One leak test. ``rate_per_day`` can come out slightly negative for a
    near-zero leak measured with noise; the calibration handles that."""

    start: float
    end: float
    rate_per_day: float  # fraction of gauge pressure lost per day, at mean_gauge_kpa
    se_per_day: float
    mean_gauge_kpa: float
    slope_kpa_per_day: float
    hours: int  # hourly bins used
    rho: float  # lag-1 autocorrelation of the hourly residuals


def hourly_means(
    ts: Sequence[float], ys: Sequence[float], start: float, end: float
) -> tuple[list[float], list[float], list[int]]:
    """Average readings in [start, end) into hour bins counted from ``start``.

    Returns (bin mid-times, means, bin indices); empty bins are skipped. Bursts
    of repeated readings after each change would otherwise dominate the fit.
    """
    sums: dict[int, list[float]] = {}
    for t, y in zip(ts, ys, strict=True):
        if start <= t < end and y is not None:
            acc = sums.setdefault(int((t - start) // HOUR_S), [0.0, 0.0, 0])
            acc[0] += t
            acc[1] += y
            acc[2] += 1
    idx = sorted(sums)
    return (
        [sums[i][0] / sums[i][2] for i in idx],
        [sums[i][1] / sums[i][2] for i in idx],
        idx,
    )


def solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Solve a x = b by Gaussian elimination with partial pivoting; None if singular."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[p][c]) < 1e-12:
            return None
        m[c], m[p] = m[p], m[c]
        for r in range(c + 1, n):
            f = m[r][c] / m[c][c]
            for k in range(c, n + 1):
                m[r][k] -= f * m[c][k]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (m[r][n] - sum(m[r][k] * x[k] for k in range(r + 1, n))) / m[r][r]
    return x


def invert(a: list[list[float]]) -> list[list[float]] | None:
    n = len(a)
    cols = []
    for j in range(n):
        col = solve(a, [1.0 if i == j else 0.0 for i in range(n)])
        if col is None:
            return None
        cols.append(col)
    return [[cols[j][i] for j in range(n)] for i in range(n)]


def fit_session(
    ts: Sequence[float],
    gauge_kpa: Sequence[float | None],
    start: float,
    end: float,
    settle_s: float = SETTLE_S,
    min_hours: int = MIN_HOURS,
    weather_sd_kpa: float = WEATHER_SD_KPA,
    weather_tau_h: float = WEATHER_TAU_H,
) -> SessionFit | None:
    """Leak rate of one test from temperature-compensated gauge pressure.

    Fits hourly means with a straight line plus a daily sine and cosine, which
    absorb what temperature compensation misses (the sensor warms and cools
    faster than the gas inside). The slope's standard error is widened for
    autocorrelated residuals (neighbouring hours are not independent), and
    weather drift is added to it (see WEATHER_SD_KPA); without both it would be
    several times too small. Returns None for too little data or a
    non-positive mean pressure.
    """
    pairs = [(t, y) for t, y in zip(ts, gauge_kpa, strict=True) if y is not None]
    hts, hys, idx = hourly_means(
        [t for t, _ in pairs], [y for _, y in pairs], start + settle_s, end
    )
    n = len(hys)
    if n < max(min_hours, 6):
        return None
    mean_y = sum(hys) / n
    if mean_y <= 0:
        return None

    t0 = hts[0]
    rows = []
    for t in hts:
        w = 2 * math.pi * (t % DAY_S) / DAY_S
        rows.append([1.0, (t - t0) / DAY_S, math.sin(w), math.cos(w)])
    p = len(rows[0])
    xtx = [[sum(r[i] * r[j] for r in rows) for j in range(p)] for i in range(p)]
    xty = [sum(r[i] * y for r, y in zip(rows, hys, strict=True)) for i in range(p)]
    coef = solve(xtx, xty)
    cov = invert(xtx)
    if coef is None or cov is None:
        return None
    resid = [
        y - sum(c * x for c, x in zip(coef, r, strict=True)) for r, y in zip(rows, hys, strict=True)
    ]
    sigma2 = sum(e * e for e in resid) / max(n - p, 1)

    # Lag-1 autocorrelation over consecutive hours only.
    num = sum(resid[i] * resid[i + 1] for i in range(n - 1) if idx[i + 1] == idx[i] + 1)
    den = sum(e * e for e in resid)
    rho = min(max(num / den if den > 0 else 0.0, 0.0), MAX_RHO)
    var_slope = max(sigma2 * cov[1][1], 0.0) * (1 + rho) / (1 - rho)

    # Weather: the slope is sum(w_i * y_i), so drift with covariance
    # sd^2 * exp(-|t_i - t_j| / tau) adds sum(w_i * w_j * cov_ij) to its variance.
    if weather_sd_kpa > 0:
        w = [sum(cov[1][k] * r[k] for k in range(p)) for r in rows]
        tau_s = weather_tau_h * HOUR_S
        var_slope += weather_sd_kpa**2 * sum(
            wi * wj * math.exp(-abs(ti - tj) / tau_s)
            for wi, ti in zip(w, hts, strict=True)
            for wj, tj in zip(w, hts, strict=True)
        )
    se_slope = math.sqrt(var_slope)

    slope = coef[1]
    return SessionFit(
        start=start,
        end=end,
        rate_per_day=-slope / mean_y,
        se_per_day=se_slope / mean_y,
        mean_gauge_kpa=mean_y,
        slope_kpa_per_day=slope,
        hours=n,
        rho=rho,
    )


# --- calibration across sessions -------------------------------------------------


@dataclass(frozen=True)
class Calibration:
    mu: float  # ln(rate per day) at REF_GAUGE_KPA
    beta: float
    beta_fitted: bool  # False: tests don't span enough pressure, beta held at 0
    tau: float  # median estimate of the seal spread (sd of ln rate between sessions)
    tau_range: tuple[float, float]  # 16th-84th percentile: how well tau is known
    tau_sq: float  # mean of tau^2 over its uncertainty, used for predictions
    cov: tuple[tuple[float, ...], ...]  # covariance of (mu, beta), or (mu,) if not fitted
    n_sessions: int

    def _x(self, gauge_kpa: float) -> float:
        return math.log(gauge_kpa / REF_GAUGE_KPA)

    def rate_per_day(self, gauge_kpa: float) -> float:
        """Typical (median) leak rate at this gauge pressure."""
        return math.exp(self.mu + self.beta * self._x(gauge_kpa))

    def log_sd(self, gauge_kpa: float) -> float:
        """Spread of ln(rate) for a new session: seal variation plus fit uncertainty."""
        x = self._x(gauge_kpa)
        if self.beta_fitted:
            (vmm, vmb), (_, vbb) = self.cov
            var_eta = vmm + 2 * x * vmb + x * x * vbb
        else:
            var_eta = self.cov[0][0]
        return math.sqrt(self.tau_sq + max(var_eta, 0.0))

    def rate_range(self, gauge_kpa: float, z: float = 1.0) -> tuple[float, float]:
        """Range a new session's rate falls in, z standard deviations either side."""
        mid, sd = math.log(self.rate_per_day(gauge_kpa)), self.log_sd(gauge_kpa)
        return math.exp(mid - z * sd), math.exp(mid + z * sd)


def _session_loglik(y: float, s: float, eta: float, tau: float) -> float:
    """ln of the probability of measuring rate y (standard error s) when the
    session's true ln(rate) is Normal(eta, tau^2), integrated numerically.

    The grid covers only where both factors matter: within 8 tau of eta, and
    where exp(u) is within 8 s of y. That keeps it accurate whether the
    measurement or the seal spread is the narrower one.
    """
    lo, hi = eta - 8 * tau, eta + 8 * tau
    top = y + 8 * s
    if top <= 0:
        return -1e6 - (top / s) ** 2  # measured far below zero: still has a gradient
    lo = max(lo, math.log(top) - 50) if y - 8 * s <= 0 else max(lo, math.log(y - 8 * s))
    hi = min(hi, math.log(top))
    if hi <= lo:
        # Supports don't overlap: use the closer edge, which still slopes the right way.
        u = eta - 8 * tau if hi < eta - 8 * tau else eta + 8 * tau
        z, e = (u - eta) / tau, (math.exp(u) - y) / s
        return -0.5 * (z * z + e * e) - math.log(tau * s * 2 * math.pi)
    h = (hi - lo) / (GRID_POINTS - 1)
    terms = []
    for k in range(GRID_POINTS):
        u = lo + k * h
        z, e = (u - eta) / tau, (math.exp(u) - y) / s
        terms.append(-0.5 * (z * z + e * e))
    peak = max(terms)
    total = sum(math.exp(v - peak) for v in terms) - 0.5 * (
        math.exp(terms[0] - peak) + math.exp(terms[-1] - peak)
    )
    return peak + math.log(max(total, 1e-300) * h) - math.log(tau * s * 2 * math.pi)


def _neg_log_lik(
    loc: Sequence[float], tau: float, sessions: Sequence[SessionFit], fit_beta: bool
) -> float:
    """-ln P(measured rates | mu, beta, tau), plus the weak prior on beta."""
    mu = loc[0]
    beta = loc[1] if fit_beta else 0.0
    total = 0.5 * (beta / BETA_PRIOR_SD) ** 2 if fit_beta else 0.0
    for f in sessions:
        eta = mu + beta * math.log(f.mean_gauge_kpa / REF_GAUGE_KPA)
        total -= _session_loglik(f.rate_per_day, f.se_per_day, eta, tau)
    return total


def nelder_mead(f, x0: Sequence[float], step: Sequence[float], iters: int = 400, tol: float = 1e-7):
    """Minimize f with the Nelder-Mead simplex method; returns (x, f(x))."""
    pts = [list(x0)] + [
        [x + (step[i] if i == j else 0.0) for i, x in enumerate(x0)] for j in range(len(x0))
    ]
    vals = [f(p) for p in pts]
    for _ in range(iters):
        order = sorted(range(len(pts)), key=vals.__getitem__)
        pts, vals = [pts[i] for i in order], [vals[i] for i in order]
        if abs(vals[-1] - vals[0]) < tol:
            break
        n = len(x0)
        cen = [sum(p[i] for p in pts[:-1]) / n for i in range(n)]

        worst = pts[-1]

        def toward(a: float, _cen=cen, _worst=worst) -> list[float]:
            return [c + a * (w - c) for c, w in zip(_cen, _worst, strict=True)]

        xr = toward(-1.0)
        fr = f(xr)
        if fr < vals[0]:
            xe = toward(-2.0)
            fe = f(xe)
            pts[-1], vals[-1] = (xe, fe) if fe < fr else (xr, fr)
        elif fr < vals[-2]:
            pts[-1], vals[-1] = xr, fr
        else:
            xc = toward(0.5)
            fc = f(xc)
            if fc < vals[-1]:
                pts[-1], vals[-1] = xc, fc
            else:
                best = pts[0]
                pts = [best] + [
                    [b + 0.5 * (p - b) for b, p in zip(best, q, strict=True)] for q in pts[1:]
                ]
                vals = [vals[0]] + [f(p) for p in pts[1:]]
    i = min(range(len(pts)), key=vals.__getitem__)
    return pts[i], vals[i]


def _hessian(f, x: Sequence[float], h: float = 1e-3) -> list[list[float]]:
    n = len(x)
    out = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i, n):

            def at(di: float, dj: float, i=i, j=j) -> float:
                p = list(x)
                p[i] += di
                p[j] += dj
                return f(p)

            v = (at(h, h) - at(h, -h) - at(-h, h) + at(-h, -h)) / (4 * h * h)
            out[i][j] = out[j][i] = v
    return out


def tau_log_prior(tau: float) -> float:
    """ln of the (unnormalized) prior density of tau: half-Cauchy, scale TAU_PRIOR_SCALE."""
    return -math.log(1 + (tau / TAU_PRIOR_SCALE) ** 2)


def fit_calibration(
    sessions: Sequence[SessionFit], fit_beta: bool | None = None
) -> Calibration | None:
    """Fit mu, beta and tau to the leak tests.

    Each test's measurement error is integrated out, so a short noisy test
    adds little and a near-zero or slightly negative rate is fine. ``fit_beta``
    defaults to fitting it only when there are three or more tests spanning at
    least BETA_MIN_PRESSURE_RATIO in mean pressure.

    tau is not taken at its single best fit: with a handful of noisy tests
    that is often ~0 even when seals vary, and predictions would be
    overconfident. Instead mu and beta are fitted at each tau on TAU_GRID, and
    the results are averaged, weighted by how well each tau explains the tests
    (see tau_log_prior; mu and beta are integrated out by Laplace's
    approximation).
    """
    usable = [s for s in sessions if s.se_per_day > 0 and s.mean_gauge_kpa > 0]
    if not usable:
        return None
    if fit_beta is None:
        ps = [s.mean_gauge_kpa for s in usable]
        fit_beta = len(usable) >= 3 and max(ps) / min(ps) >= BETA_MIN_PRESSURE_RATIO
    k = 2 if fit_beta else 1

    weights = [1 / s.se_per_day**2 for s in usable]
    pooled = sum(w * s.rate_per_day for w, s in zip(weights, usable, strict=True)) / sum(weights)
    loc = [math.log(max(pooled, 1e-4)), 0.0][:k]

    grid = []  # (tau, log weight, loc, cov)
    for tau in TAU_GRID:

        def f(p: Sequence[float], tau=tau) -> float:
            return _neg_log_lik(p, tau, usable, fit_beta)

        loc, nll = nelder_mead(f, loc, [0.3] * k)
        cov = invert(_hessian(f, loc))
        if cov is None or any(cov[i][i] <= 0 for i in range(k)):
            continue
        det = cov[0][0] if k == 1 else cov[0][0] * cov[1][1] - cov[0][1] ** 2
        if det <= 0:
            continue
        # + ln(tau): the grid is evenly spaced in ln(tau), the prior is a density in tau.
        grid.append(
            (tau, -nll + 0.5 * math.log(det) + tau_log_prior(tau) + math.log(tau), loc, cov)
        )
    if not grid:
        return None

    top = max(g[1] for g in grid)
    ws = [math.exp(g[1] - top) for g in grid]
    total = sum(ws)
    ws = [w / total for w in ws]
    mean = [sum(w * g[2][i] for w, g in zip(ws, grid, strict=True)) for i in range(k)]
    cov = [
        [
            sum(
                w * (g[3][i][j] + (g[2][i] - mean[i]) * (g[2][j] - mean[j]))
                for w, g in zip(ws, grid, strict=True)
            )
            for j in range(k)
        ]
        for i in range(k)
    ]

    def tau_quantile(q: float) -> float:
        acc = 0.0
        for w, g in zip(ws, grid, strict=True):
            acc += w
            if acc >= q:
                return g[0]
        return grid[-1][0]

    return Calibration(
        mu=mean[0],
        beta=mean[1] if fit_beta else 0.0,
        beta_fitted=fit_beta,
        tau=tau_quantile(0.5),
        tau_range=(tau_quantile(0.16), tau_quantile(0.84)),
        tau_sq=sum(w * g[0] ** 2 for w, g in zip(ws, grid, strict=True)),
        cov=tuple(tuple(row) for row in cov),
        n_sessions=len(usable),
    )
