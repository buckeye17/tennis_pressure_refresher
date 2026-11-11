"""Find the tennis ball in each video frame (PLAN.md section 6.3).

Optic-yellow pixels are grouped into blobs; blobs of the wrong shape are
dropped. The ball is picked up as the first yellow blob that **moves**, so a
yellow thing sitting still in the background is ignored, and from then on it
is tracked as the blob nearest where it should be. Holding the ball still
doesn't lose it: tracking only needs it to stay put or move continuously.
Blobs too narrow to be a ball are ignored, unless the user clicked one.
If the user drew a tracking area, blobs whose centre is outside it are ignored.

Frames are analysed at about ``analysis_height`` rows; results are reported
in pixels of the original frame.

The ball's bottom edge is then refined to a fraction of a pixel in the full
frame: down a strip through the ball's centre, each row's colour is placed
between the ball's colour and the background's, and the edge is where it
crosses halfway. Motion blur smears an edge symmetrically, so that halfway
point is where the ball was at the middle of the exposure: the same measure
works for a ball at rest and one moving fast.
"""

from collections import deque
from dataclasses import dataclass, replace

import cv2
import numpy as np

from canister_monitor.video.measure import Observation


@dataclass(frozen=True)
class ColorRange:
    """HSV bounds in OpenCV units (hue 0-180)."""

    lo: tuple[int, int, int] = (22, 70, 80)
    hi: tuple[int, int, int] = (48, 255, 255)

    @classmethod
    def around(cls, hsv: tuple[float, float, float]) -> "ColorRange":
        """A range centred on a sampled ball colour (``--pick``)."""
        h, s, v = hsv
        return cls(
            (max(int(h) - 9, 0), max(int(s) - 70, 40), max(int(v) - 80, 40)),
            (min(int(h) + 9, 180), 255, 255),
        )


@dataclass(frozen=True)
class Candidate:
    obs: Observation
    area: float
    fill: float  # area / bounding box area: ~0.79 for a disc


# The narrowest a ball may be to get picked up, as a fraction of the frame's height: a
# 6.7 cm ball in a picture 3.7 m tall at the drop line. The drop has to fit in the
# picture, so a real ball is rarely smaller, and yellowish specks in a room are.
# Clicking the ball overrides this, for a camera set further back.
MIN_BALL_FRAC = 0.018
# A ball being followed may look narrower than that (a hand or a leg covers part of it),
# but not by this much: without a limit, following slides from a ball onto a speck.
FOLLOW_SHRINK = 0.75

# A tracking area: a polygon's corners, each (x, y) as fractions of the picture's width
# and height, so it fits the picture at any resolution.
Area = tuple[tuple[float, float], ...]
MAX_AREA_POINTS = 64


def check_area(value: object) -> Area | None:
    """An area from JSON (a list of [x, y] pairs, or null), or ValueError."""
    if value is None:
        return None
    msg = (
        f"the tracking area must be 3 to {MAX_AREA_POINTS} points [x, y], "
        "each a fraction of the picture from 0 to 1"
    )
    if not isinstance(value, (list, tuple)) or not 3 <= len(value) <= MAX_AREA_POINTS:
        raise ValueError(msg)
    points = []
    for p in value:
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            raise ValueError(msg)
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in p):
            raise ValueError(msg)
        x, y = float(p[0]), float(p[1])
        if not (0 <= x <= 1 and 0 <= y <= 1):
            raise ValueError(msg)
        points.append((x, y))
    if abs(_signed_area(points)) < 1e-3:  # under 0.1% of the picture: a line or a dot
        raise ValueError("the tracking area is too small")
    return tuple(points)


def _signed_area(points: list[tuple[float, float]]) -> float:
    return 0.5 * sum(
        x0 * y1 - x1 * y0
        for (x0, y0), (x1, y1) in zip(points, points[1:] + points[:1], strict=True)
    )


class BallDetector:
    def __init__(
        self,
        analysis_height: int = 720,
        color: ColorRange | None = None,
        start_point: tuple[float, float] | None = None,
        min_radius_px: float = 3.0,
        lost_after_s: float = 0.6,
        min_ball_frac: float = MIN_BALL_FRAC,
        area: Area | None = None,
    ) -> None:
        self.analysis_height = analysis_height
        self.color = color or ColorRange()
        self.start_point = start_point  # --pick: pick up the blob nearest this first
        self.min_radius_px = min_radius_px
        self.lost_after_s = lost_after_s
        self.min_ball_frac = min_ball_frac
        self.area = area  # only blobs centred inside it count; None: the whole picture
        self._area_px: tuple[tuple[int, int], np.ndarray] | None = None  # (frame size, polygon)
        self._size: tuple[int, int] | None = None  # the last frame's (width, height)
        self._clicked = False  # the user chose this blob, so it may be any size
        self.track: deque[Observation] = deque(maxlen=3)
        self.last_seen: Observation | None = None  # kept after the track is lost
        self._past: deque[tuple[float, list[Candidate]]] = deque()  # for spotting motion
        self._kernel = np.ones((3, 3), np.uint8)

    def forget(self) -> None:
        """Drop everything tied to positions in the picture; the colour stays."""
        self.start_point = None
        self._clicked = False
        self.set_area(None)
        self.track.clear()
        self.last_seen = None
        self._past.clear()

    def set_area(self, area: Area | None) -> None:
        """Track only inside ``area`` from now on. A ball being followed outside it is
        let go at once, rather than when it's next missed."""
        self.area = area
        self._area_px = None
        last = self.track[-1] if self.track else self.last_seen
        if area is not None and last is not None and self._size is not None:
            if not self._inside(last.cx, last.cy, *self._size):
                self.track.clear()
                self.last_seen = None
        self._past.clear()  # blobs seen before may be outside; motion is judged afresh

    def _inside(self, x: float, y: float, w: int, h: int) -> bool:
        if self.area is None:
            return True
        if self._area_px is None or self._area_px[0] != (w, h):
            poly = np.array([(px * w, py * h) for px, py in self.area], np.float32)
            self._area_px = ((w, h), poly.reshape(-1, 1, 2))
        return cv2.pointPolygonTest(self._area_px[1], (float(x), float(y)), False) >= 0

    def restart_clock(self, t0: float) -> None:
        """Make ``t0`` time zero, keeping what has been seen.

        A live session's clock restarts at Start. Every age here is a difference
        of times, so history left on the old clock would look like the future:
        the track would never expire, and nothing could be found again.
        """
        self.track = deque((replace(o, t=o.t - t0) for o in self.track), maxlen=self.track.maxlen)
        if self.last_seen is not None:
            self.last_seen = replace(self.last_seen, t=self.last_seen.t - t0)
        self._past = deque((tp - t0, cands) for tp, cands in self._past)

    def candidates(self, frame: np.ndarray, t: float) -> list[Candidate]:
        """Ball-shaped yellow blobs, in original-frame pixels."""
        h0, w0 = frame.shape[:2]
        self._size = (w0, h0)
        scale = min(1.0, self.analysis_height / h0)
        small = (
            cv2.resize(frame, (round(w0 * scale), round(h0 * scale)), interpolation=cv2.INTER_AREA)
            if scale < 1
            else frame
        )
        hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, self.color.lo, self.color.hi)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self._kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, self._kernel)
        n, _, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
        min_area = np.pi * (self.min_radius_px * scale) ** 2
        out = []
        for i in range(1, n):
            x, y, w, h, area = (int(v) for v in stats[i])
            if area < min_area:
                continue
            fill = area / (w * h)
            aspect = h / w
            if fill < 0.45 or not 0.5 <= aspect <= 5.0:
                continue
            cx, cy = cents[i]
            if not self._inside((cx + 0.5) / scale, (cy + 0.5) / scale, w0, h0):
                continue
            obs = Observation(
                t=t,
                cx=(cx + 0.5) / scale,
                cy=(cy + 0.5) / scale,
                width=w / scale,
                height=h / scale,
                bottom=(y + h) / scale,
            )
            out.append(Candidate(obs, area / scale**2, fill))
        return out

    def detect(self, frame: np.ndarray, t: float) -> Observation | None:
        cands = self.candidates(frame, t)
        self._past.append((t, cands))
        while self._past and t - self._past[0][0] > 0.5:
            self._past.popleft()

        if self.track and t - self.track[-1].t > self.lost_after_s:
            self.track.clear()
        min_width = self.min_ball_frac * frame.shape[0]
        if not self._clicked and self.start_point is None:
            cands = [c for c in cands if c.obs.width >= FOLLOW_SHRINK * min_width]
        pick = self._follow(cands, t) if self.track else self._acquire(cands, t, min_width)
        if pick is None:
            return None
        obs = pick.obs
        blur = abs(obs.cy - self.track[-1].cy) if self.track else 0.0
        edge = refine_bottom(frame, obs, blur)
        if edge is not None:
            obs = replace(obs, bottom=edge)
        self.track.append(obs)
        self.last_seen = obs
        return obs

    def _follow(self, cands: list[Candidate], t: float) -> Candidate | None:
        last = self.track[-1]
        vx = vy = 0.0
        if len(self.track) >= 2:
            prev = self.track[-2]
            dt = last.t - prev.t
            if dt > 0:
                vx, vy = (last.cx - prev.cx) / dt, (last.cy - prev.cy) / dt
        dt = t - last.t
        px, py = last.cx + vx * dt, last.cy + vy * dt
        gate = 3 * last.width + (abs(vx) + abs(vy)) * dt
        best, best_d = None, gate
        for c in cands:
            # Width barely changes (blur stretches blobs vertically, not sideways).
            if not 0.6 <= c.obs.width / last.width <= 1.6:
                continue
            d = float(np.hypot(c.obs.cx - px, c.obs.cy - py))
            if d < best_d:
                best, best_d = c, d
        return best

    def _acquire(self, cands: list[Candidate], t: float, min_width: float) -> Candidate | None:
        if self.start_point is not None and cands:
            x, y = self.start_point
            self.start_point = None
            self._clicked = True
            return min(cands, key=lambda c: np.hypot(c.obs.cx - x, c.obs.cy - y))
        last = self.last_seen
        if last is not None and t - last.t <= 3.0:
            # Missed for a moment while held still: take it back where it was.
            near = [
                c
                for c in cands
                if np.hypot(c.obs.cx - last.cx, c.obs.cy - last.cy) <= 1.5 * last.width
                and 0.6 <= c.obs.width / last.width <= 1.6
            ]
            if near:
                return min(near, key=lambda c: np.hypot(c.obs.cx - last.cx, c.obs.cy - last.cy))
        moving = [
            c
            for c in cands
            if c.fill >= 0.6 and c.obs.width >= min_width and not self._was_here(c, t)
        ]
        if not moving:
            return None
        self._clicked = False
        return max(moving, key=lambda c: c.area)

    def _was_here(self, c: Candidate, t: float) -> bool:
        """True if a blob sat at this spot throughout the last ~0.4 s: not moving."""
        old = [cs for tp, cs in self._past if t - tp >= 0.3]
        if not old:
            return True  # too early to tell: don't grab anything yet
        tol = 0.3 * c.obs.width
        return all(
            any(abs(o.obs.cx - c.obs.cx) <= tol and abs(o.obs.cy - c.obs.cy) <= tol for o in cs)
            for cs in old
        )


def refine_bottom(frame: np.ndarray, obs: Observation, blur_px: float = 0.0) -> float | None:
    """Sub-pixel bottom edge (see the module docstring), or None without enough contrast."""
    h, w = frame.shape[:2]
    r = obs.width / 2
    x0, x1 = max(round(obs.cx - 0.35 * r), 0), min(round(obs.cx + 0.35 * r) + 1, w)
    y0 = max(int(obs.cy), 0)
    y1 = min(int(obs.bottom + max(4.0, 0.6 * r) + blur_px) + 1, h)
    if x1 - x0 < 1 or y1 - y0 < 6:
        return None
    prof = frame[y0:y1, x0:x1].astype(np.float32).mean(axis=1)
    ball, bg = prof[:2].mean(axis=0), prof[-2:].mean(axis=0)
    d = ball - bg
    contrast = float(d @ d)
    if contrast < 400:  # under ~20 levels apart: can't place the edge
        return None
    a = (prof - bg) @ d / contrast
    cross = np.nonzero((a[:-1] >= 0.5) & (a[1:] < 0.5))[0]
    if len(cross) == 0:
        return None
    i = int(cross[-1])
    frac = (a[i] - 0.5) / (a[i] - a[i + 1])
    return y0 + i + 0.5 + float(frac)  # between the centres of rows i and i + 1


def sample_color(frame: np.ndarray, x: float, y: float, radius: int = 3) -> tuple[float, ...]:
    """Median HSV around a clicked point."""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    xi, yi = int(x), int(y)
    patch = hsv[max(yi - radius, 0) : yi + radius + 1, max(xi - radius, 0) : xi + radius + 1]
    return tuple(float(v) for v in np.median(patch.reshape(-1, 3), axis=0))
