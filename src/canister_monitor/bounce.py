"""Bounce tests and predictions (PLAN.md section 6.2).

Each sampled ball is dropped a few times per test; its rebound is the median of
its drops, which shrugs off one bad release. A test's summary is the mean and
spread of those per-ball medians. Lengths are in cm.

A test's drops can also be checked for outliers (``fit_normal``): a normal
distribution is fitted to them, and drops too far out to belong to it are flagged
for the user to look at. They aren't dropped automatically.

Pure functions, like trends.py and leak.py. Standard library only: this runs on the Pi.
"""

import math
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

CM_PER_IN = 2.54
CONTEXTS = ("new", "before", "after", "after_play", "other")
ITF_DROP_CM = 254.0  # 100 in


@dataclass(frozen=True)
class TestSummary:
    balls: int
    mean_cm: float | None  # mean of the per-ball medians
    sd_cm: float | None  # between-ball standard deviation (None for one ball)
    medians: dict[int, float]  # ball number -> median rebound


def ball_medians(drops: Iterable[tuple[int, float]]) -> dict[int, float]:
    """Median rebound per ball from (ball_no, rebound_cm) drops, in ball order."""
    by_ball: dict[int, list[float]] = {}
    for ball, rebound in drops:
        by_ball.setdefault(ball, []).append(rebound)
    return {ball: statistics.median(by_ball[ball]) for ball in sorted(by_ball)}


def summarize(drops: Iterable[tuple[int, float]]) -> TestSummary:
    medians = ball_medians(drops)
    values = list(medians.values())
    return TestSummary(
        balls=len(values),
        mean_cm=statistics.fmean(values) if values else None,
        sd_cm=statistics.stdev(values) if len(values) > 1 else None,
        medians=medians,
    )


# --- outliers ---------------------------------------------------------------------------

OUTLIER_ALPHA = 0.05  # chance of flagging anything in a test whose drops are all fine
OUTLIER_MIN_N = 8  # fewer values can't tell an outlier from an unlucky spread


@dataclass(frozen=True)
class NormalFit:
    n: int  # values fitted, outliers included
    mean: float  # of the values that aren't outliers
    sd: float  # ditto (sample standard deviation)
    z: tuple[float, ...]  # each value's distance from the mean, in sd (nan if sd is 0)
    outliers: tuple[int, ...]  # indices into the values, most extreme first
    tested: bool  # False: too few values to look for outliers


def t_quantile(p: float, df: float) -> float:
    """Student's t quantile, from the normal one by the Cornish-Fisher expansion
    (Abramowitz & Stegun 26.7.5). Within 0.3% for df >= 5 out to p = 0.999."""
    z = statistics.NormalDist().inv_cdf(p)
    g = (
        (z**3 + z) / 4,
        (5 * z**5 + 16 * z**3 + 3 * z) / 96,
        (3 * z**7 + 19 * z**5 + 17 * z**3 - 15 * z) / 384,
        (79 * z**9 + 776 * z**7 + 1482 * z**5 - 1920 * z**3 - 945 * z) / 92160,
    )
    return z + sum(gi / df ** (i + 1) for i, gi in enumerate(g))


def fit_normal(
    values: Sequence[float],
    alpha: float = OUTLIER_ALPHA,
    max_outliers: int | None = None,
) -> NormalFit | None:
    """Fit a normal distribution to ``values`` and find the ones that don't belong.

    Rosner's generalized ESD test: take out the value furthest from the mean,
    refit, and repeat up to ``max_outliers`` times (default a fifth of the
    values); the outliers are the most taken out whose distance beat the
    test's critical value. Refitting each time keeps one bad value from hiding
    another, and the critical values hold the chance of a false flag in the
    whole set at ``alpha``. None for fewer than 2 values.
    """
    n = len(values)
    if n < 2:
        return None
    tested = n >= OUTLIER_MIN_N
    r = 0 if not tested else max_outliers if max_outliers is not None else max(1, n // 5)
    left = list(range(n))
    taken: list[int] = []
    found = 0
    for i in range(1, min(r, n - 3) + 1):
        xs = [values[k] for k in left]
        m, s = statistics.fmean(xs), statistics.stdev(xs)
        if s == 0:
            break
        far = max(left, key=lambda k: abs(values[k] - m))
        size = n - i + 1
        t = t_quantile(1 - alpha / (2 * size), size - 2)
        critical = (size - 1) * t / math.sqrt((size - 2 + t * t) * size)
        taken.append(far)
        left.remove(far)
        if abs(values[far] - m) / s > critical:
            found = i
    outliers = tuple(taken[:found])
    kept = [values[k] for k in range(n) if k not in outliers]
    mean, sd = statistics.fmean(kept), statistics.stdev(kept)
    z = tuple((v - mean) / sd if sd > 0 else math.nan for v in values)
    return NormalFit(n, mean, sd, z, outliers, tested)


def outlier_json(values: Sequence[float]) -> tuple[list[float | None], dict | None]:
    """For the apps: each value's z if it's an outlier (else None), and the fit."""
    fit = fit_normal(values)
    if fit is None:
        return [None] * len(values), None
    flags: list[float | None] = [None] * len(values)
    for i in fit.outliers:
        flags[i] = fit.z[i]
    return flags, {
        "drops": fit.n,
        "mean_cm": fit.mean,
        "sd_cm": fit.sd,
        "outliers": len(fit.outliers),
        "tested": fit.tested,
    }
