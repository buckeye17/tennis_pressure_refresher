"""Find the tennis ball in each video frame (PLAN.md section 6.3).

Optic-yellow pixels are grouped into blobs; blobs of the wrong shape are
dropped. The ball is picked up as the first yellow blob that **moves**, so a
yellow thing sitting still in the background is ignored, and from then on it
is tracked as the blob nearest where it should be. Holding the ball still
doesn't lose it: tracking only needs it to stay put or move continuously.

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


class BallDetector:
    def __init__(
        self,
        analysis_height: int = 720,
        color: ColorRange | None = None,
        start_point: tuple[float, float] | None = None,
        min_radius_px: float = 3.0,
        lost_after_s: float = 0.6,
    ) -> None:
        self.analysis_height = analysis_height
        self.color = color or ColorRange()
        self.start_point = start_point  # --pick: pick up the blob nearest this first
        self.min_radius_px = min_radius_px
        self.lost_after_s = lost_after_s
        self.track: deque[Observation] = deque(maxlen=3)
        self.last_seen: Observation | None = None  # kept after the track is lost
        self._past: deque[tuple[float, list[Candidate]]] = deque()  # for spotting motion
        self._kernel = np.ones((3, 3), np.uint8)

    def candidates(self, frame: np.ndarray, t: float) -> list[Candidate]:
        """Ball-shaped yellow blobs, in original-frame pixels."""
        h0, w0 = frame.shape[:2]
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
        pick = self._follow(cands, t) if self.track else self._acquire(cands, t)
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

    def _acquire(self, cands: list[Candidate], t: float) -> Candidate | None:
        if self.start_point is not None and cands:
            x, y = self.start_point
            self.start_point = None
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
        moving = [c for c in cands if c.fill >= 0.6 and not self._was_here(c, t)]
        return max(moving, key=lambda c: c.area) if moving else None

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
