"""From per-frame ball positions to calibrated drop heights (PLAN.md section 6.3).

``Measurer`` is a streaming state machine: feed it one observation per frame
(``None`` when the ball wasn't found) and it reports calibration holds and
finished drops as they happen, so a live camera and a recorded clip share this
code.

Image rows grow downward. Every "row" here is the row of the ball's **bottom**
edge in pixels of the original frame, as a continuous coordinate (pixel i
spans [i, i + 1)), measured to a fraction of a pixel by the detector. That
halfway-crossing edge is unaffected by motion blur, and the hand holding a
ball covers the top, not the bottom, so one measure serves for a ball at rest
and in flight. The ball's radius (median half-width of recent round blobs)
only scales thresholds.

Heights come from a 1-D projective map from row to height along the drop line,
fixed by three calibration holds at known heights (floor, mid mark, drop mark).
That absorbs camera tilt, which would otherwise add ~0.5 cm per degree. At the
end of a session the drop-mark row is re-anchored on the median of all
releases, so one sloppy hold can't skew every drop, and the drops are
recomputed (``RefinedEvent``).
"""

import statistics
from collections import deque
from dataclasses import dataclass, field, replace

import numpy as np

BALL_DIAMETER_CM = 6.7


@dataclass(frozen=True)
class Observation:
    t: float  # seconds
    cx: float  # blob centroid, px
    cy: float
    width: float  # blob bounding box, px
    height: float
    bottom: float  # bottom edge of the ball, px (sub-pixel when the detector refined it)


@dataclass(frozen=True)
class Settings:
    """Thresholds, mostly in ball radii so they don't depend on resolution or zoom."""

    still_s: float = 0.25  # at rest this long counts as still
    still_tol_r: float = 0.15  # ... moving less than this
    hold_s: float = 1.5  # a calibration hold lasts at least this long
    hold_gap_r: float = 2.0  # calibration holds must be at least this far apart
    fall_start_r: float = 0.6  # a fall starts this far below the rest level
    impact_rise_r: float = 1.5  # risen this far from the lowest point: it bounced
    impact_guard_s: float = 0.03  # samples this close to impact are left out of fits
    apex_band: float = 0.35  # the apex fit uses the top 35% of the rise
    apex_exit_r: float = 1.0  # fallen this far past the apex: the rise is over
    post_apex_s: float = 0.25  # or this long after the apex
    max_gap_s: float = 0.3  # a longer gap in the ball's track abandons a drop
    min_rebound_speed: float = 0.3  # rise speed / fall speed at impact; below: not a bounce
    release_warn_cm: float = 2.0
    floor_warn_cm: float = 3.0
    fit_warn_r: float = 0.15  # fit residual (rms) above this many radii gets a warning
    width_warn: float = 0.15  # ball width off what the scale predicts by more than this


@dataclass(frozen=True)
class Hold:
    t_start: float
    t_end: float
    row: float  # median bottom edge while held


@dataclass(frozen=True)
class Calibration:
    """Height (cm) from bottom row: (p0 + p1 * row) / (1 + p2 * row)."""

    rows: tuple[float, ...]
    heights_cm: tuple[float, ...]
    p: tuple[float, float, float]
    method: str  # "projective" (3 holds) or "linear" (2 holds, assumes a level camera)

    def height_cm(self, row: float) -> float:
        p0, p1, p2 = self.p
        return (p0 + p1 * row) / (1 + p2 * row)

    def px_per_cm(self, row: float, step: float = 1.0) -> float:
        return step / abs(self.height_cm(row) - self.height_cm(row + step))

    def row_for(self, height_cm: float) -> float:
        """The inverse map: which row a height appears at."""
        p0, p1, p2 = self.p
        return (height_cm - p0) / (p1 - p2 * height_cm)


def fit_calibration(rows: list[float], heights_cm: list[float]) -> Calibration | None:
    """Map through 2 points (linear) or 3 (projective). None if they don't make a
    usable map: rows must fall as heights rise, with no pole between them."""
    if len(rows) != len(heights_cm) or len(rows) not in (2, 3):
        return None
    if any(r2 >= r1 for r1, r2 in zip(rows, rows[1:], strict=False)):
        return None
    if len(rows) == 2:
        (r0, r1), (h0, h1) = rows, heights_cm
        slope = (h1 - h0) / (r1 - r0)
        p = (h0 - slope * r0, slope, 0.0)
        method = "linear"
    else:
        a = np.array([[1.0, r, -r * h] for r, h in zip(rows, heights_cm, strict=True)])
        try:
            p = tuple(float(v) for v in np.linalg.solve(a, np.array(heights_cm, dtype=float)))
        except np.linalg.LinAlgError:
            return None
        method = "projective"
    lo, hi = min(rows), max(rows)
    span = hi - lo
    # The denominator must not cross zero anywhere near the frame.
    for row in (lo - span, hi + span):
        if 1 + p[2] * row <= 0:
            return None
    return Calibration(tuple(rows), tuple(heights_cm), p, method)


@dataclass(frozen=True)
class Drop:
    seq: int  # 0-based order in the session
    t_release: float
    t_impact: float
    t_apex: float
    release_row: float
    floor_row: float
    apex_row: float
    rebound_cm: float  # corrected for the actual release height when calibrated
    release_cm: float | None  # actual release height (calibrated only)
    floor_cm: float | None  # where the bounce floor maps to; ~0 if the camera didn't move
    method: str  # "projective", "linear" (2-hold) or "self" (per-drop, level camera)
    fit_rms_r: dict  # fit residuals in ball radii: fall, floor_rise, apex
    samples: dict  # sample counts: fall, rise, apex
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class HoldEvent:
    hold: Hold
    count: int  # holds seen so far (before calibration)


@dataclass(frozen=True)
class CalibrationEvent:
    calibration: Calibration


@dataclass(frozen=True)
class DropEvent:
    drop: Drop


@dataclass(frozen=True)
class RefinedEvent:
    """End of session: the calibration re-anchored and every drop recomputed."""

    calibration: Calibration
    drops: tuple[Drop, ...]


def _quad_fit(ts: list[float], ys: list[float], t0: float) -> tuple[np.ndarray, float] | None:
    """Least-squares y = c0 + c1 (t - t0) + c2 (t - t0)^2; returns (coeffs, rms)."""
    if len(ts) < 4:
        return None
    x = np.asarray(ts) - t0
    y = np.asarray(ys)
    c = np.polynomial.polynomial.polyfit(x, y, 2)
    rms = float(np.sqrt(np.mean((np.polynomial.polynomial.polyval(x, c) - y) ** 2)))
    return c, rms


def _quad_eval(c: np.ndarray, t: float, t0: float) -> float:
    return float(np.polynomial.polynomial.polyval(t - t0, c))


def _quad_slope(c: np.ndarray, t: float, t0: float) -> float:
    x = t - t0
    return float(c[1] + 2 * c[2] * x)


class Measurer:
    """Feed observations in time order; collect events. See the module docstring."""

    def __init__(
        self,
        drop_height_cm: float,
        mid_mark_cm: float | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.drop_height_cm = drop_height_cm
        self.mid_mark_cm = mid_mark_cm
        self.s = settings or Settings()
        self.holds: list[Hold] = []
        self.calibration: Calibration | None = None
        self.drops: list[Drop] = []
        self._radii: deque[float] = deque(maxlen=120)
        self._recent: deque[Observation] = deque()  # for stillness
        self._still_rows: list[float] = []  # bottom edges during the current still spell
        self._still_start: float | None = None
        self._still_last: float | None = None
        self._hold_reported = False
        self._state = "idle"
        self._samples: list[tuple[float, float]] = []  # (t, bottom row) this drop
        self._rest: tuple[float, float] | None = None  # (t, row) where it last sat still
        self._extreme: tuple[float, float] | None = None  # (t, row) lowest / highest point
        self._impact: tuple[float, float] | None = None
        self._last_t: float | None = None

    # --- public ----------------------------------------------------------------------

    @property
    def radius(self) -> float | None:
        return statistics.median(self._radii) if len(self._radii) >= 3 else None

    def reset_calibration(self) -> None:
        """Start the calibration holds over (live mode, key R)."""
        self.holds.clear()
        self.calibration = None

    def feed(self, t: float, obs: Observation | None) -> list:
        events: list = []
        if obs is not None and 0.75 <= obs.height / max(obs.width, 1e-9) <= 1.33:
            self._radii.append(obs.width / 2)
        radius = self.radius or (obs.width / 2 if obs is not None else None)
        if obs is None or radius is None:
            self._gap(t, events)
            self._last_t = t
            return events
        self._last_t = t
        still = self._update_still(obs, radius)
        self._update_holds(t, still, events)
        self._step_drop(t, obs.bottom, radius, still, events)
        return events

    def finish(self) -> list:
        """End of the session: close an open hold and a drop that reached its apex,
        then refine the calibration."""
        events: list = []
        if self._still_start is not None:
            self._end_still(events)
        if self._state == "rising":
            self._finalize(events)
        events += self._refine()
        return events

    def _refine(self) -> list:
        """Anchor the drop-height row on the median release instead of one hold.

        Every release is meant to be at the mark, so their median is a better
        estimate of where the mark is than the single calibration hold.
        """
        cal = self.calibration
        if cal is None or len(self.drops) < 2:
            return []
        anchor = statistics.median([cal.rows[-1]] + [d.release_row for d in self.drops])
        new = fit_calibration([*cal.rows[:-1], anchor], list(cal.heights_cm))
        if new is None:
            return []
        self.calibration = new
        self.drops = [self._recompute(d, new) for d in self.drops]
        return [RefinedEvent(new, tuple(self.drops))]

    def _recompute(self, d: Drop, cal: Calibration) -> Drop:
        release_cm = cal.height_cm(d.release_row)
        floor_cm = cal.height_cm(d.floor_row)
        apex_cm = cal.height_cm(d.apex_row)
        kept = [w for w in d.warnings if not w.startswith(("released at", "bounce floor"))]
        return replace(
            d,
            rebound_cm=apex_cm * self.drop_height_cm / release_cm,
            release_cm=release_cm,
            floor_cm=floor_cm,
            warnings=tuple(kept + self._height_warnings(release_cm, floor_cm)),
        )

    def _height_warnings(self, release_cm: float, floor_cm: float) -> list[str]:
        h, s = self.drop_height_cm, self.s
        out = []
        if abs(release_cm - h) > s.release_warn_cm:
            out.append(f"released at {release_cm:.1f} cm, not {h:.1f}: rebound scaled to match")
        if abs(floor_cm) > s.floor_warn_cm:
            out.append(f"bounce floor maps to {floor_cm:+.1f} cm: did the camera move? Recalibrate")
        return out

    # --- stillness and calibration holds ---------------------------------------------

    def _update_still(self, obs: Observation, radius: float) -> bool:
        self._recent.append(obs)
        while self._recent and obs.t - self._recent[0].t > self.s.still_s:
            self._recent.popleft()
        span = obs.t - self._recent[0].t
        rows = [o.bottom for o in self._recent]
        tol = max(1.0, self.s.still_tol_r * radius)
        # Starting a still spell needs enough time seen; continuing one only needs
        # the ball not to move, so a dropped frame can't break a hold.
        enough = self._still_start is not None or (
            len(self._recent) >= 3 and span >= 0.7 * self.s.still_s
        )
        still = enough and len(self._recent) >= 2 and max(rows) - min(rows) <= tol
        if still:
            if self._still_start is None:
                self._still_start = self._recent[0].t
                self._still_rows = [o.bottom for o in self._recent]
            else:
                self._still_rows.append(obs.bottom)
            self._still_last = obs.t
        return still

    def _update_holds(self, t: float, still: bool, events: list) -> None:
        if still:
            assert self._still_start is not None
            if not self._hold_reported and t - self._still_start >= self.s.hold_s:
                self._hold_reported = True
                if self._calibrating():
                    hold = Hold(self._still_start, t, statistics.median(self._still_rows))
                    events.append(HoldEvent(hold, len(self.holds) + 1))
        elif self._still_start is not None:
            self._end_still(events)

    def _end_still(self, events: list) -> None:
        assert self._still_start is not None and self._still_last is not None
        duration = self._still_last - self._still_start
        if duration >= self.s.hold_s and self._calibrating():
            self.holds.append(
                Hold(self._still_start, self._still_last, statistics.median(self._still_rows))
            )
            self._try_calibrate(events)
        self._still_start = None
        self._still_last = None
        self._hold_reported = False

    def _calibrating(self) -> bool:
        return self.calibration is None and not self.drops

    def _try_calibrate(self, events: list) -> None:
        """Use the latest holds once they run floor -> (mid) -> drop height."""
        radius = self.radius or 1.0
        want = 3 if self.mid_mark_cm is not None else 2
        if len(self.holds) < want:
            return
        rows = [h.row for h in self.holds[-want:]]
        if any(
            r1 - r2 < self.s.hold_gap_r * radius for r1, r2 in zip(rows, rows[1:], strict=False)
        ):
            return
        heights = [0.0, self.drop_height_cm]
        if want == 3:
            heights.insert(1, self.mid_mark_cm)
        cal = fit_calibration(rows, heights)
        if cal is not None:
            self.calibration = cal
            events.append(CalibrationEvent(cal))

    # --- drops ---------------------------------------------------------------------

    def _gap(self, t: float, events: list) -> None:
        """The ball wasn't seen. A long gap ends a rise (if past the apex) or abandons a drop."""
        if self._state in ("falling", "rising") and self._samples:
            if t - self._samples[-1][0] > self.s.max_gap_s:
                if self._state == "rising":
                    self._finalize(events)
                else:
                    self._reset_drop()

    def _reset_drop(self, state: str = "idle") -> None:
        self._state = state
        self._samples = []
        self._extreme = None
        self._impact = None

    def _step_drop(self, t: float, row: float, radius: float, still: bool, events: list) -> None:
        s = self.s
        if self._state in ("idle", "armed"):
            if still:
                self._state = "armed"
                self._rest = (t, statistics.median(self._still_rows[-15:]))
            elif self._state == "armed" and self._rest is not None:
                if row - self._rest[1] > s.fall_start_r * radius:
                    self._state = "falling"
                    self._samples = [(t, row)]
                    self._extreme = (t, row)
                elif t - self._rest[0] > 1.0:
                    self._state = "idle"  # moved, but not a fall: carried somewhere
            return

        if self._samples and t - self._samples[-1][0] > s.max_gap_s:
            self._gap(t, events)
            if self._state not in ("falling", "rising"):
                return
        self._samples.append((t, row))

        if self._state == "falling":
            if still:  # caught or set down before reaching the floor
                self._reset_drop("armed")
                return
            assert self._extreme is not None
            if row > self._extreme[1]:
                self._extreme = (t, row)
            elif self._extreme[1] - row > s.impact_rise_r * radius:
                self._impact = self._extreme
                self._extreme = (t, row)  # now tracks the highest point of the rise
                self._state = "rising"
            return

        # rising
        assert self._extreme is not None
        if row < self._extreme[1]:
            self._extreme = (t, row)
        past_apex = row - self._extreme[1] > s.apex_exit_r * radius
        if past_apex or still or t - self._extreme[0] > s.post_apex_s:
            self._finalize(events)

    def _finalize(self, events: list) -> None:
        drop = self._measure()
        self._reset_drop()
        if drop is not None:
            self.drops.append(drop)
            events.append(DropEvent(drop))

    def _apex_points(
        self, rise: list[tuple[float, float]], band_row: float, t_top: float, radius: float
    ) -> list[tuple[float, float]]:
        """Points in the top band, symmetric about the highest one seen. A catch
        soon after the apex freezes the ball, so trailing points that leave the
        parabola are trimmed one by one."""
        before = [(t, r) for t, r in rise if r < band_row and t <= t_top]
        if not before:
            return []
        reach = t_top - before[0][0]
        after = [(t, r) for t, r in rise if r < band_row and t_top < t <= t_top + reach]
        tol = max(1.0, 0.1 * radius)
        while after:
            pts = before + after
            fit = _quad_fit([t for t, _ in pts], [r for _, r in pts], t_top)
            if fit is None:
                break
            t_last, r_last = after[-1]
            if abs(_quad_eval(fit[0], t_last, t_top) - r_last) <= max(tol, 3 * fit[1]):
                break
            after.pop()
        return before + after

    def _measure(self) -> Drop | None:
        """Fit the fall, the start of the rise and the apex; None if it wasn't a bounce.

        With a calibration, rows are converted to heights first and the parabolas
        are fitted in centimetres: through a tilted camera a fall isn't a parabola
        in rows. Without one, rows are used and the drop calibrates itself.
        Either way the working coordinate ``z`` grows downward, like rows.
        """
        s = self.s
        if self._rest is None or self._impact is None or self._extreme is None:
            return None
        radius = self.radius or 1.0
        cal = self.calibration
        t_rel, release_row = self._rest
        t_imp, imp_row = self._impact
        t_top, top_row = self._extreme
        if cal is not None:

            def to_z(row: float) -> float:
                return -cal.height_cm(row)

            radius_z = radius / cal.px_per_cm(top_row)
        else:

            def to_z(row: float) -> float:
                return row

            radius_z = radius
        samples = [(t, to_z(r)) for t, r in self._samples]
        release_z, imp_z, top_z = to_z(release_row), to_z(imp_row), to_z(top_row)

        fall = [(t, z) for t, z in samples if t < t_imp - s.impact_guard_s]
        rise = [(t, z) for t, z in samples if t > t_imp + s.impact_guard_s]
        # The release itself: a parabola from rest starts at the rest level.
        fall_fit = _quad_fit(
            [t_rel] + [t for t, _ in fall], [release_z] + [z for _, z in fall], t_imp
        )
        span = imp_z - top_z
        if fall_fit is None or span <= 0:
            return None
        low_rise = [(t, z) for t, z in rise if z > top_z + 0.4 * span and t < t_top]
        rise_fit = _quad_fit([t for t, _ in low_rise], [z for _, z in low_rise], t_imp)
        top = self._apex_points(rise, top_z + s.apex_band * span, t_top, radius_z)
        apex_fit = _quad_fit([t for t, _ in top], [z for _, z in top], t_top)
        if rise_fit is None or apex_fit is None:
            return None

        # Floor: where the fall and the rise meet, nearest the observed lowest point.
        diff = fall_fit[0] - rise_fit[0]
        roots = [z.real for z in np.polynomial.polynomial.polyroots(diff) if abs(z.imag) < 1e-9]
        roots = [t_imp + x for x in roots if abs(x) < 0.15]
        t_floor = min(roots, key=lambda x: abs(x - t_imp)) if roots else t_imp
        floor_z = _quad_eval(fall_fit[0], t_floor, t_imp)
        v_in = _quad_slope(fall_fit[0], t_floor, t_imp)
        v_out = _quad_slope(rise_fit[0], t_floor, t_imp)
        if v_in <= 0 or v_out >= 0 or -v_out < s.min_rebound_speed * v_in:
            return None  # no sharp reversal: carried down and up, not bounced

        c = apex_fit[0]
        if c[2] <= 0:
            return None
        t_apex = t_top - c[1] / (2 * c[2])
        apex_z = _quad_eval(c, t_apex, t_top)

        warnings: list[str] = []
        if not roots:
            warnings.append("fall and rise fits don't meet: floor taken at the lowest point seen")
        if len(fall) < 4 or len(low_rise) < 4 or len(top) < 5:
            warnings.append("few frames: the fit may be rough")
        rms = {
            "fall": fall_fit[1] / radius_z,
            "floor_rise": rise_fit[1] / radius_z,
            "apex": apex_fit[1] / radius_z,
        }
        if max(rms.values()) > s.fit_warn_r:
            worst = max(rms, key=rms.get)
            warnings.append(f"{worst} fit is rough ({rms[worst]:.2f} ball radii rms)")

        h = self.drop_height_cm
        release_cm = floor_cm = None
        if cal is not None:
            release_cm, floor_cm, apex_cm = -release_z, -floor_z, -apex_z
            floor_row, apex_row = cal.row_for(floor_cm), cal.row_for(apex_cm)
            rebound = apex_cm * h / release_cm if release_cm > 0 else apex_cm
            method = cal.method
            warnings += self._height_warnings(release_cm, floor_cm)
            if cal.method == "linear":
                warnings.append("calibrated without a mid mark: assumes a level camera")
            expected_w = BALL_DIAMETER_CM * cal.px_per_cm(apex_row)
        else:
            floor_row, apex_row = floor_z, apex_z
            rebound = h * (floor_row - apex_row) / (floor_row - release_row)
            method = "self"
            warnings.append("no calibration holds: assumes a level camera")
            expected_w = BALL_DIAMETER_CM * (floor_row - release_row) / h
        if abs(2 * radius / expected_w - 1) > s.width_warn:
            warnings.append(
                f"ball looks {2 * radius:.0f} px wide; the scale predicts {expected_w:.0f}"
            )
        if not 0.3 <= rebound / h <= 0.8:
            warnings.append(f"rebound is {rebound / h:.0%} of the drop height: check this drop")

        return Drop(
            seq=len(self.drops),
            t_release=t_rel,
            t_impact=t_floor,
            t_apex=t_apex,
            release_row=release_row,
            floor_row=floor_row,
            apex_row=apex_row,
            rebound_cm=rebound,
            release_cm=release_cm,
            floor_cm=floor_cm,
            method=method,
            fit_rms_r=rms,
            samples={"fall": len(fall), "rise": len(low_rise), "apex": len(top)},
            warnings=tuple(warnings),
        )


@dataclass
class Session:
    """Drops assigned to balls in order, with undo and skip (PLAN.md 6.3)."""

    drops_per_ball: int = 3
    first_ball: int = 1
    entries: list[tuple[int, Drop]] = field(default_factory=list)  # (ball_no, drop)
    _next_ball: int = field(init=False)
    _count: int = field(init=False, default=0)  # drops on the current ball

    def __post_init__(self) -> None:
        self._next_ball = self.first_ball

    @property
    def next_ball(self) -> int:
        return self._next_ball

    @property
    def next_drop(self) -> int:
        return self._count + 1

    def add(self, drop: Drop) -> int:
        ball = self._next_ball
        self.entries.append((ball, drop))
        self._count += 1
        if self._count >= self.drops_per_ball:
            self._next_ball += 1
            self._count = 0
        return ball

    def undo(self) -> tuple[int, Drop] | None:
        if not self.entries:
            return None
        ball, drop = self.entries.pop()
        self._next_ball = ball
        self._count = sum(1 for b, _ in self.entries if b == ball)
        return ball, drop

    def skip_ball(self) -> None:
        self._next_ball += 1
        self._count = 0
