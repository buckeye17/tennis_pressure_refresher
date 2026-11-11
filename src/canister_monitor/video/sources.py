"""Live frame sources for the laptop app (PLAN.md 6.3): webcams, found by probing.

A source has ``name``, ``width``, ``height``, ``fps`` (nominal), ``read()`` returning
``(t, frame)`` or ``None`` when it has stopped, ``close()``, and ``drops_when_late``:
whether frames are lost if not read in time. Webcams are timestamped with the
capture clock as each frame arrives, so a dropped frame doesn't distort the fits.
``synth.SyntheticCamera`` is the stand-in used by tests and ``--demo-camera``.
"""

import sys
import time
from dataclasses import dataclass
from typing import Protocol

import cv2
import numpy as np

WANT_WIDTH, WANT_HEIGHT, WANT_FPS = 1280, 720, 60
PROBE_INDICES = range(5)  # OpenCV can't list cameras by name, so indices are tried

# Degrees clockwise to turn a camera's picture so that real down is at the bottom.
# Turning a webcam on its side puts its long edge along the drop.
ROTATIONS = {
    0: None,
    90: cv2.ROTATE_90_CLOCKWISE,
    180: cv2.ROTATE_180,
    270: cv2.ROTATE_90_COUNTERCLOCKWISE,
}


def check_rotation(rotation: object) -> int:
    """The rotation if it's one of ``ROTATIONS``. Raises ValueError."""
    if isinstance(rotation, bool) or not isinstance(rotation, int) or rotation not in ROTATIONS:
        raise ValueError("rotation must be 0, 90, 180 or 270 degrees")
    return rotation


def rotated(frame: np.ndarray, rotation: int) -> np.ndarray:
    turn = ROTATIONS[rotation]
    return frame if turn is None else cv2.rotate(frame, turn)


class FrameSource(Protocol):
    name: str
    width: int
    height: int
    fps: float
    drops_when_late: bool

    def read(self) -> tuple[float, np.ndarray] | None: ...

    def close(self) -> None: ...


def _backend() -> int:
    # DirectShow opens faster than Media Foundation on Windows and honours MJPG,
    # which most webcams need for 720p above 30 fps.
    return cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY


def _open(index: int) -> cv2.VideoCapture | None:
    cap = cv2.VideoCapture(index, _backend())
    if not cap.isOpened():
        cap.release()
        return None
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, WANT_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, WANT_HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, WANT_FPS)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap


class CameraSource:
    """A webcam asked for 1280x720 at 60 fps; it may deliver less."""

    drops_when_late = True

    def __init__(self, index: int) -> None:
        cap = _open(index)
        if cap is None:
            raise ValueError(f"camera {index} can't be opened")
        ok, frame = cap.read()
        if not ok:
            cap.release()
            raise ValueError(f"camera {index} opened but gave no picture")
        self._cap = cap
        self.index = index
        self.name = f"camera {index}"
        self.height, self.width = frame.shape[:2]
        self.fps = float(cap.get(cv2.CAP_PROP_FPS) or WANT_FPS)

    def read(self) -> tuple[float, np.ndarray] | None:
        ok, frame = self._cap.read()
        if not ok:
            return None
        # perf_counter, not monotonic: before Python 3.13, monotonic ticks every
        # 15.6 ms on Windows, half a frame at 30 fps and ~10 cm of a falling ball.
        return time.perf_counter(), frame

    def close(self) -> None:
        self._cap.release()


@dataclass(frozen=True)
class CameraInfo:
    index: int
    width: int
    height: int
    fps: float  # what the driver says it will deliver; the app measures the real rate


def probe_cameras(indices=PROBE_INDICES) -> list[CameraInfo]:
    """The webcams that open and give a picture, with the size each delivers."""
    found = []
    for i in indices:
        try:
            src = CameraSource(i)
        except ValueError:
            continue
        found.append(CameraInfo(i, src.width, src.height, src.fps))
        src.close()
    return found
