"""Frames in, measured drops out: shared by clips and live cameras (PLAN.md 6.3)."""

import math
from collections import deque
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path

import cv2
import numpy as np

from canister_monitor import __version__
from canister_monitor.video.detect import BallDetector
from canister_monitor.video.measure import (
    CalibrationEvent,
    Drop,
    DropEvent,
    Measurer,
    Observation,
    RefinedEvent,
    Session,
    Settings,
)

TOOL = f"canister-video {__version__}"
BUFFER_S = 1.2  # recent frames kept for drop pictures (a drop ends <~0.6 s after its apex)
PREVIEW_ROWS = 640  # buffered frames are shrunk to this height


@dataclass(frozen=True)
class VideoInfo:
    source: str  # "file" or "camera"
    name: str  # file path, or the recording's path for a camera
    width: int
    height: int
    fps: float  # nominal; timestamps come from the video or the capture clock
    frames: int  # 0 if unknown (camera)


def open_clip(path: Path) -> tuple[VideoInfo, Iterator[tuple[float, np.ndarray]]]:
    """Frames of a video file with their timestamps (seconds).

    Phone clips often have a variable frame rate, so each frame's own timestamp
    is used; the nominal rate is only the fallback.
    """
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise ValueError(f"can't open {path} as a video")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    info = VideoInfo(
        source="file",
        name=str(path),
        width=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
        height=int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        fps=float(fps),
        frames=int(cap.get(cv2.CAP_PROP_FRAME_COUNT)),
    )

    def frames() -> Iterator[tuple[float, np.ndarray]]:
        i, last = 0, -math.inf
        try:
            while True:
                ok, frame = cap.read()
                if not ok:
                    return
                t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000
                if not t > last:  # missing or not increasing: fall back to the frame count
                    t = i / fps if i / fps > last else last + 1 / fps
                last = t
                i += 1
                yield t, frame
        finally:
            cap.release()

    return info, frames()


@dataclass
class Buffered:
    t: float
    small: np.ndarray  # whole frame at PREVIEW_ROWS high
    scale: float
    obs: Observation | None


@dataclass
class Result:
    video: VideoInfo
    drop_height_cm: float
    mid_mark_cm: float | None
    drops_per_ball: int
    settings: Settings
    color: dict
    frames: int = 0
    ball_found: int = 0
    holds: list = field(default_factory=list)
    calibration: object | None = None
    entries: list[tuple[int, Drop]] = field(default_factory=list)
    images: dict[int, np.ndarray] = field(default_factory=dict)  # drop seq -> picture

    def to_json(self) -> dict:
        cal = self.calibration
        return {
            "tool": TOOL,
            "video": asdict(self.video),
            "drop_height_cm": self.drop_height_cm,
            "mid_mark_cm": self.mid_mark_cm,
            "drops_per_ball": self.drops_per_ball,
            "frames": {"total": self.frames, "ball_found": self.ball_found},
            "params": {"color": self.color, "settings": asdict(self.settings)},
            "holds": [asdict(h) for h in self.holds],
            "calibration": None if cal is None else asdict(cal),
            "drops": [
                {
                    "seq": d.seq,
                    "ball_no": ball,
                    "rebound_cm": d.rebound_cm,
                    "release_cm": d.release_cm,
                    "floor_cm": d.floor_cm,
                    "method": d.method,
                    "times": {"release": d.t_release, "impact": d.t_impact, "apex": d.t_apex},
                    "rows": {"release": d.release_row, "floor": d.floor_row, "apex": d.apex_row},
                    "fit_rms_r": d.fit_rms_r,
                    "samples": d.samples,
                    "warnings": list(d.warnings),
                }
                for ball, d in self.entries
            ],
        }


class Analyzer:
    """Detector + measurer + ball numbering, one frame at a time."""

    def __init__(
        self,
        video: VideoInfo,
        drop_height_cm: float,
        mid_mark_cm: float | None = None,
        drops_per_ball: int = 3,
        detector: BallDetector | None = None,
        settings: Settings | None = None,
        trace: list | None = None,
    ) -> None:
        self.detector = detector or BallDetector()
        self.measurer = Measurer(drop_height_cm, mid_mark_cm, settings)
        self.session = Session(drops_per_ball)
        self.trace = trace  # if a list, (t, Observation | None) per frame is appended
        self._buffer: deque[Buffered] = deque()
        self.result = Result(
            video=video,
            drop_height_cm=drop_height_cm,
            mid_mark_cm=mid_mark_cm,
            drops_per_ball=drops_per_ball,
            settings=self.measurer.s,
            color=asdict(self.detector.color),
        )

    def process(self, t: float, frame: np.ndarray) -> list:
        obs = self.detector.detect(frame, t)
        self.result.frames += 1
        self.result.ball_found += obs is not None
        if self.trace is not None:
            self.trace.append((t, obs))
        scale = min(1.0, PREVIEW_ROWS / frame.shape[0])
        small = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        self._buffer.append(Buffered(t, small, scale, obs))
        while t - self._buffer[0].t > BUFFER_S:
            self._buffer.popleft()
        return self._handle(self.measurer.feed(t, obs))

    def finish(self) -> list:
        return self._handle(self.measurer.finish())

    def _handle(self, events: list) -> list:
        for e in events:
            if isinstance(e, CalibrationEvent):
                self.result.calibration = e.calibration
                self.result.holds = list(self.measurer.holds)
            elif isinstance(e, DropEvent):
                ball = self.session.add(e.drop)
                self.result.entries = list(self.session.entries)
                image = self.drop_image(e.drop, ball)
                if image is not None:
                    self.result.images[e.drop.seq] = image
            elif isinstance(e, RefinedEvent):
                self.result.calibration = e.calibration
                by_seq = {d.seq: d for d in e.drops}
                self.session.entries = [(b, by_seq.get(d.seq, d)) for b, d in self.session.entries]
                self.result.entries = list(self.session.entries)
        return events

    def undo(self) -> tuple[int, Drop] | None:
        undone = self.session.undo()
        self.result.entries = list(self.session.entries)
        if undone is not None:
            self.result.images.pop(undone[1].seq, None)
        return undone

    def drop_image(self, drop: Drop, ball: int) -> np.ndarray | None:
        """The frame nearest the apex, cropped to the drop with its lines drawn."""
        if not self._buffer:
            return None
        b = min(self._buffer, key=lambda x: abs(x.t - drop.t_apex))
        img = b.small.copy()
        s = b.scale
        h, w = img.shape[:2]
        cx = next((x.obs.cx for x in self._buffer if x.obs is not None), w / s / 2)
        top = int(max(drop.release_row * s - 25, 0))
        bottom = int(min(drop.floor_row * s + 15, h))
        half = max(int((bottom - top) * 0.35), 60)
        x0 = int(min(max(cx * s - half, 0), max(w - 2 * half, 0)))
        lines = [
            (drop.release_row, (255, 160, 60), "release"),
            (drop.apex_row, (60, 220, 60), "apex"),  # no number: it may be refined
            (drop.floor_row, (60, 60, 230), "floor"),
        ]
        for row, colour, label in lines:
            y = int(round(row * s))
            cv2.line(img, (x0, y), (x0 + 2 * half, y), colour, 1, cv2.LINE_AA)
            ty = y + 14 if label == "release" else y - 4  # release label below its line
            cv2.putText(img, label, (x0 + 3, ty), cv2.FONT_HERSHEY_SIMPLEX, 0.45, colour, 1)
        crop = img[top:bottom, x0 : x0 + 2 * half]
        cv2.putText(
            crop,
            f"ball {ball}",
            (4, crop.shape[0] - 6),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )
        return crop if crop.size else None


def analyze_clip(
    path: Path,
    drop_height_cm: float,
    mid_mark_cm: float | None = None,
    drops_per_ball: int = 3,
    detector: BallDetector | None = None,
    trace: list | None = None,
) -> Result:
    info, frames = open_clip(path)
    analyzer = Analyzer(info, drop_height_cm, mid_mark_cm, drops_per_ball, detector, trace=trace)
    for t, frame in frames:
        analyzer.process(t, frame)
    analyzer.finish()
    return analyzer.result
