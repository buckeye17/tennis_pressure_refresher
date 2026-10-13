"""Bounce tests and predictions (PLAN.md section 6.2).

Each sampled ball is dropped a few times per test; its rebound is the median of
its drops, which shrugs off one bad release. A test's summary is the mean and
spread of those per-ball medians. Lengths are in cm.

Pure functions, like trends.py and leak.py.
"""

import statistics
from collections.abc import Iterable
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
