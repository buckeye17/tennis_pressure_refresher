"""Artificial bounce-test clips with known answers (PLAN.md section 6.3).

A tennis ball with real physics (gravity, air drag, a coefficient of
restitution per ball) is filmed by a pinhole camera that may be tilted, with
motion blur, sensor noise, a cluttered background, a hand at holds and
releases, a second bounce, and optionally a static yellow distractor. The
session follows the protocol: calibration holds (floor, mid mark, drop mark),
then drops in ball order, each caught after its first bounce.

    python -m canister_monitor.video.synth out.mp4 --tilt 12
    # writes out.mp4 and out.json (the true release and rebound heights)
"""

import argparse
import json
import math
import sys
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path

import cv2
import numpy as np

G = 9.81
BALL_RADIUS_M = 0.0335
# 0.5 * air density * drag coefficient * cross-section / mass, for a tennis ball
# (1.2 kg/m3, Cd 0.55, 6.7 cm, 57 g): terminal speed ~22 m/s.
DRAG_K = 0.0204
BALL_BGR = (70, 220, 200)  # optic yellow under indoor light
HAND_BGR = (125, 160, 215)
DT = 1e-4  # physics step, s
SAMPLE_DT = 1e-3  # trajectory sample spacing, s


@dataclass(frozen=True)
class Camera:
    width: int = 540
    height: int = 960
    fps: float = 30.0
    vfov_deg: float = 62.0
    distance_m: float = 3.0
    height_m: float = 1.0
    tilt_deg: float = 0.0  # positive: pointing up, like a laptop screen leaning back
    exposure_s: float = 1 / 120
    noise_sd: float = 3.0

    @property
    def focal_px(self) -> float:
        return (self.height / 2) / math.tan(math.radians(self.vfov_deg) / 2)

    def project(self, x_m: float, y_m: float) -> tuple[float, float, float]:
        """(column, row, pixels per metre) of a point on the drop plane; y is height."""
        th = math.radians(self.tilt_deg)
        dy, dz = y_m - self.height_m, self.distance_m
        yc = dy * math.cos(th) - dz * math.sin(th)
        zc = dy * math.sin(th) + dz * math.cos(th)
        f = self.focal_px
        return self.width / 2 + f * x_m / zc, self.height / 2 - f * yc / zc, f / zc

    def offscreen_x(self) -> float:
        return -(self.width / 2 / self.focal_px) * self.distance_m - 0.4

    @classmethod
    def framing(cls, drop_height_cm: float, tilt_deg: float = 0.0, **kw) -> "Camera":
        """A camera set at the height that puts the floor near the bottom of the frame."""
        cam = cls(tilt_deg=tilt_deg, **kw)
        below = math.radians(cam.vfov_deg / 2 - tilt_deg)  # frame's bottom edge, below level
        margin = 0.16 * math.tan(math.radians(cam.vfov_deg / 2)) * cam.distance_m
        height = cam.distance_m * math.tan(below) - margin
        cam = cls(tilt_deg=tilt_deg, height_m=max(height, 0.2), **kw)
        if cam.project(0, -0.05)[1] > cam.height:
            raise ValueError("the floor isn't in the frame: tilt less or move the camera back")
        if cam.project(0, drop_height_cm / 100 + 0.15)[1] < 0:
            raise ValueError("the drop height doesn't fit in the frame: move the camera back")
        return cam


@dataclass(frozen=True)
class SessionSpec:
    drop_height_cm: float = 182.88  # 72 in
    mid_mark_cm: float | None = 91.44  # 36 in
    cor: tuple[float, ...] = (0.76, 0.72)  # one per ball: rebound speed / impact speed
    drops_per_ball: int = 2
    release_sd_cm: float = 0.5
    calibrate: bool = True
    distractor: bool = True
    catch_bounce: int = 1  # catch after this bounce's apex (2: a second bounce to ignore)
    seed: int = 1


@dataclass
class Truth:
    drop_height_cm: float
    mid_mark_cm: float | None
    drops: list[dict] = field(default_factory=list)  # ball, drop, release_cm, rebound_cm
    camera: dict = field(default_factory=dict)


class Trajectory:
    """Ball bottom height and lateral position over time, plus whether a hand holds it."""

    def __init__(self) -> None:
        self.t: list[float] = []
        self.x: list[float] = []
        self.y: list[float] = []
        self.hand: list[bool] = []

    @property
    def end(self) -> float:
        return self.t[-1] if self.t else 0.0

    def _add(self, x: float, y: float, hand: bool) -> None:
        self.t.append(self.end + SAMPLE_DT if self.t else 0.0)
        self.x.append(x)
        self.y.append(y)
        self.hand.append(hand)

    def hold(self, x: float, y: float, seconds: float, hand: bool = True) -> None:
        for _ in range(round(seconds / SAMPLE_DT)):
            self._add(x, y, hand)

    def move(self, x1: float, y1: float, seconds: float, hand: bool = True) -> None:
        x0, y0 = self.x[-1], self.y[-1]
        n = round(seconds / SAMPLE_DT)
        for i in range(1, n + 1):
            a = i / n
            a = a * a * (3 - 2 * a)  # smoothstep
            self._add(x0 + (x1 - x0) * a, y0 + (y1 - y0) * a, hand)

    def drop(self, cor: float, catch_bounce: int = 1, catch_after_apex_s: float = 0.12) -> float:
        """Release from rest at the current height and catch the ball just after the
        apex of bounce ``catch_bounce``; returns the first rebound's apex."""
        x, y, v = self.x[-1], self.y[-1], 0.0
        bounces, apex, t_apex, t = 0, 0.0, None, 0.0
        next_sample = SAMPLE_DT
        while True:
            v += (-G - DRAG_K * v * abs(v)) * DT
            y += v * DT
            t += DT
            if y <= 0 and v < 0:
                y, v, bounces, t_apex = -y, -cor * v, bounces + 1, None
            if bounces and v <= 0 and t_apex is None:
                t_apex = t
                if bounces == 1:
                    apex = y
            if bounces == catch_bounce and t_apex is not None and t - t_apex >= catch_after_apex_s:
                break
            if t >= next_sample:
                self._add(x, y, False)
                next_sample += SAMPLE_DT
        return apex

    def at(self, t: float) -> tuple[float, float, bool]:
        i = min(max(round(t / SAMPLE_DT), 0), len(self.t) - 1)
        return self.x[i], self.y[i], self.hand[i]


def build(spec: SessionSpec, camera: Camera) -> tuple[Trajectory, Truth]:
    rng = np.random.default_rng(spec.seed)
    h = spec.drop_height_cm / 100
    out = camera.offscreen_x()
    tr = Trajectory()
    truth = Truth(spec.drop_height_cm, spec.mid_mark_cm, camera=asdict(camera))
    tr.hold(out, 0.3, 0.3)
    tr.move(0.0, 0.0, 1.2)  # bring the first ball in
    if spec.calibrate:
        tr.hold(0.0, 0.0, 2.0, hand=False)  # resting on the floor
        if spec.mid_mark_cm is not None:
            tr.move(0.0, spec.mid_mark_cm / 100, 0.8)
            tr.hold(0.0, spec.mid_mark_cm / 100, 2.0)
    for ball, cor in enumerate(spec.cor, start=1):
        if ball > 1:  # swap balls: out of frame and back
            tr.move(out, 0.8, 0.8)
            tr.hold(out, 0.8, 0.5)
            tr.move(0.0, 0.8, 0.8)
        for n in range(1, spec.drops_per_ball + 1):
            release = h + rng.normal(0, spec.release_sd_cm / 100)
            tr.move(0.0, release, 0.8)
            first = spec.calibrate and ball == 1 and n == 1
            tr.hold(0.0, release, 2.0 if first else 1.0)  # the drop-mark hold, then let go
            apex = tr.drop(cor, spec.catch_bounce)
            truth.drops.append(
                {"ball": ball, "drop": n, "release_cm": release * 100, "rebound_cm": apex * 100}
            )
            tr.hold(tr.x[-1], tr.y[-1], 0.3)  # caught
    tr.move(out, 1.0, 1.0)
    tr.hold(out, 1.0, 0.5)
    return tr, truth


def background(camera: Camera, spec: SessionSpec) -> np.ndarray:
    rng = np.random.default_rng(spec.seed + 1000)
    w, h = camera.width, camera.height
    img = np.zeros((h, w, 3), np.float32)
    grad = np.linspace(170, 140, h, dtype=np.float32)[:, None]
    img[:] = np.stack([grad * 0.95, grad * 0.92, grad * 0.88], axis=-1)  # warm grey wall
    floor_row = int(camera.project(0, 0)[1])
    if floor_row < h:
        img[max(floor_row, 0) :] = (95, 110, 125)  # floor
    for _ in range(6):  # posters and furniture, never yellow
        x0, y0 = rng.integers(0, w - 40), rng.integers(0, max(h // 2, 41))
        x1, y1 = x0 + rng.integers(30, 160), y0 + rng.integers(30, 200)
        colour = rng.choice([(40, 40, 160), (150, 70, 40), (60, 120, 60), (180, 180, 180)])
        cv2.rectangle(
            img, (int(x0), int(y0)), (int(x1), int(y1)), tuple(float(c) for c in colour), -1
        )
    u0 = camera.project(-0.15, 0)[0]  # door frame edge behind the drop line
    cv2.line(img, (int(u0), 0), (int(u0), h), (110, 105, 100), 6)
    marks = [spec.drop_height_cm] + ([spec.mid_mark_cm] if spec.mid_mark_cm else [])
    for cm in marks:  # tape marks
        u, v, _ = camera.project(-0.12, cm / 100)
        cv2.line(img, (int(u - 15), int(v)), (int(u + 5), int(v)), (30, 30, 30), 2)
    if spec.distractor:  # a yellow ball sitting still on the floor
        u, v, ppm = camera.project(0.7, BALL_RADIUS_M)
        cv2.circle(
            img, (int(u), int(v)), max(int(BALL_RADIUS_M * ppm), 2), BALL_BGR, -1, cv2.LINE_AA
        )
    return img


def render(
    camera: Camera,
    spec: SessionSpec,
    tr: Trajectory,
    times: list[float] | None = None,
    blur_samples: int = 6,
) -> Iterator[tuple[float, np.ndarray]]:
    """Yield (t, BGR frame). ``times`` defaults to a steady frame rate."""
    rng = np.random.default_rng(spec.seed + 2000)
    bg = background(camera, spec)
    noise = [rng.normal(0, camera.noise_sd, bg.shape).astype(np.float32) for _ in range(8)]
    colour = np.array(BALL_BGR, np.float32)
    if times is None:
        times = [i / camera.fps for i in range(int(tr.end * camera.fps))]
    shift = 4  # sub-pixel drawing: coordinates in 1/16 px
    for n, t in enumerate(times):
        frame = bg + noise[n % len(noise)]
        spots = []
        for k in range(blur_samples):
            x, y, hand = tr.at(t + camera.exposure_s * (k + 0.5) / blur_samples)
            u, v, ppm = camera.project(x, y + BALL_RADIUS_M)
            spots.append((u, v, BALL_RADIUS_M * ppm, hand))
        # Draw only in the box the ball covers during the exposure.
        pad = max(sp[2] for sp in spots) + 3
        bx0 = max(int(min(sp[0] for sp in spots) - pad), 0)
        by0 = max(int(min(sp[1] for sp in spots) - pad), 0)
        bx1 = min(int(max(sp[0] for sp in spots) + pad) + 1, camera.width)
        by1 = min(int(max(sp[1] for sp in spots) + pad) + 1, camera.height)
        hand_at = None
        if bx1 > bx0 and by1 > by0:
            cover = np.zeros((by1 - by0, bx1 - bx0), np.float32)
            for u, v, r, hand in spots:
                layer = np.zeros_like(cover)
                cv2.circle(
                    layer,
                    (round((u - bx0) * 2**shift), round((v - by0) * 2**shift)),
                    round(r * 2**shift),
                    1.0,
                    -1,
                    cv2.LINE_AA,
                    shift,
                )
                cover += layer / blur_samples
                if hand:
                    hand_at = (u, v, r)
            roi = frame[by0:by1, bx0:bx1]
            c = cover[..., None]
            ball = colour + noise[(n + 3) % len(noise)][by0:by1, bx0:bx1]
            frame[by0:by1, bx0:bx1] = roi * (1 - c) + ball * c
        if hand_at is not None:  # fingers over the top of the ball
            u, v, r = hand_at
            cv2.ellipse(
                frame,
                (int(u - 0.2 * r), int(v - 1.0 * r)),
                (int(1.4 * r), int(0.8 * r)),
                -15,
                0,
                360,
                HAND_BGR,
                -1,
                cv2.LINE_AA,
            )
        yield t, np.clip(frame, 0, 255).astype(np.uint8)


def write_clip(path: Path, camera: Camera, spec: SessionSpec) -> Truth:
    tr, truth = build(spec, camera)
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"mp4v"), camera.fps, (camera.width, camera.height)
    )
    if not writer.isOpened():
        raise RuntimeError(f"can't write {path}")
    try:
        for _, frame in render(camera, spec, tr):
            writer.write(frame)
    finally:
        writer.release()
    return truth


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("out", type=Path, help="video file to write (.mp4)")
    ap.add_argument("--tilt", type=float, default=0.0, help="camera tilt, degrees (up)")
    ap.add_argument("--landscape", action="store_true", help="1280x720, like a webcam")
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--balls", type=int, default=2)
    ap.add_argument("--drops-per-ball", type=int, default=2)
    ap.add_argument("--no-calibration", action="store_true")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args(argv)
    h = SessionSpec().drop_height_cm
    if args.landscape:
        camera = Camera.framing(
            h, args.tilt, width=1280, height=720, fps=args.fps, vfov_deg=45, distance_m=3.2
        )
    else:
        camera = Camera.framing(h, args.tilt, fps=args.fps)
    rng = np.random.default_rng(args.seed)
    spec = SessionSpec(
        cor=tuple(float(c) for c in rng.uniform(0.68, 0.8, args.balls)),
        drops_per_ball=args.drops_per_ball,
        calibrate=not args.no_calibration,
        seed=args.seed,
    )
    truth = write_clip(args.out, camera, spec)
    args.out.with_suffix(".json").write_text(json.dumps(asdict(truth), indent=2))
    for d in truth.drops:
        print(f"ball {d['ball']} drop {d['drop']}: rebound {d['rebound_cm']:.1f} cm")
    return 0


if __name__ == "__main__":
    sys.exit(main())
