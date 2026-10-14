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
        return time.monotonic(), frame

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
