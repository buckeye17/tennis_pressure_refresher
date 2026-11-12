"""One bounce-test session at a time, for the laptop app (PLAN.md 6.3).

A **live** session opens a camera and shows its preview while the user frames
the shot (phase ``framing``). Start begins the analysis and the recording
(``live``); Finish ends both and goes to ``review``. A **clip** is analysed as
fast as it decodes (``analysing``), then reviewed. Either way the same
``pipeline.Analyzer`` measures the drops.

Threads, for a live session: one reads the camera and timestamps frames as
they arrive; one analyses them, draws the preview and runs the user's
commands (so the analyzer is only ever touched by one thread); one writes the
recording. A clip uses a single thread. The web app reads ``state()`` and the
latest preview JPEG; review edits happen after the threads have finished.
"""

import base64
import logging
import queue
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import cv2
import numpy as np

from canister_monitor.bounce import outlier_json, summarize
from canister_monitor.video.detect import (
    Area,
    BallDetector,
    ColorRange,
    check_area,
    sample_color,
)
from canister_monitor.video.measure import HoldEvent
from canister_monitor.video.pipeline import TOOL, Analyzer, VideoInfo, open_clip, times_path
from canister_monitor.video.sources import FrameSource, check_rotation, rotated

log = logging.getLogger(__name__)

PREVIEW_FPS = 12.0
PREVIEW_ROWS = 540
PREVIEW_QUALITY = 70
FPS_WARN = 25.0  # webcams slow down in dim light, which also blurs the ball
QUEUE_S = 2.0  # live frames waiting for analysis before new ones are dropped
COMMAND_TIMEOUT_S = 5.0
MAX_CM = 500.0
KEEP = object()  # Controller.reanalyse: keep the session's tracking area


def stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


# --- what the user set up ------------------------------------------------------------


@dataclass(frozen=True)
class Params:
    drop_height_cm: float
    mid_mark_cm: float | None = None
    drops_per_ball: int = 3
    batch_id: int | None = None
    batch_label: str | None = None
    sample_size: int | None = None  # balls numbered 1..sample_size get tested
    setup_id: int | None = None
    setup_name: str | None = None
    context: str | None = None

    @classmethod
    def from_json(cls, body: object) -> "Params":
        """Validate the page's set-up form. Raises ValueError with a message for the user."""
        if not isinstance(body, dict):
            raise ValueError("set-up must be a JSON object")
        unknown = set(body) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")

        def number(name: str, lo: float, hi: float, optional: bool = False) -> float | None:
            v = body.get(name)
            if v is None and optional:
                return None
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
                raise ValueError(f"{name} must be a number from {lo:g} to {hi:g}")
            return float(v)

        def whole(name: str, lo: int, hi: int, optional: bool = True) -> int | None:
            v = body.get(name)
            if v is None and optional:
                return None
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise ValueError(f"{name} must be a whole number from {lo} to {hi}")
            return v

        def text(name: str) -> str | None:
            v = body.get(name)
            if v is not None and (not isinstance(v, str) or len(v) > 120):
                raise ValueError(f"{name} must be short text")
            return v or None

        drop = number("drop_height_cm", 10.0, MAX_CM)
        mid = number("mid_mark_cm", 1.0, MAX_CM, optional=True)
        if mid is not None and mid >= drop:
            raise ValueError("the mid mark must be between the floor and the drop height")
        return cls(
            drop_height_cm=drop,
            mid_mark_cm=mid,
            drops_per_ball=whole("drops_per_ball", 1, 20, optional=False)
            if "drops_per_ball" in body
            else 3,
            batch_id=whole("batch_id", 1, 2**62),
            batch_label=text("batch_label"),
            sample_size=whole("sample_size", 1, 500),
            setup_id=whole("setup_id", 1, 2**62),
            setup_name=text("setup_name"),
            context=text("context"),
        )


# --- review ----------------------------------------------------------------------------


@dataclass
class ReviewDrop:
    seq: int
    ball_no: int
    measured_cm: float
    kept: bool = True
    edited_cm: float | None = None  # typed over the measurement

    @property
    def height_cm(self) -> float:
        return self.edited_cm if self.edited_cm is not None else self.measured_cm


class Review:
    """The drops of a finished session, as the user corrects them."""

    def __init__(self, analyzer: Analyzer, params: Params, key: str | None = None) -> None:
        self.analyzer = analyzer
        self.params = params
        # Names this review on the Pi: saving it again replaces the test it made. Kept
        # across Analyse again, which re-measures the same session.
        self.key = key or uuid.uuid4().hex
        self.drops = [ReviewDrop(d.seq, ball, d.rebound_cm) for ball, d in analyzer.result.entries]
        self.version = 0
        self._jpeg: dict[tuple[int, bool], bytes] = {}

    def edit(self, seq: int, changes: dict) -> None:
        """Apply ``ball_no``, ``kept`` and ``edited_cm`` (None clears it). Raises ValueError."""
        drop = next((d for d in self.drops if d.seq == seq), None)
        if drop is None:
            raise LookupError(f"no drop {seq}")
        unknown = set(changes) - {"ball_no", "kept", "edited_cm"}
        if unknown:
            raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
        new = replace(drop)
        if "ball_no" in changes:
            b = changes["ball_no"]
            if isinstance(b, bool) or not isinstance(b, int) or not 1 <= b <= 500:
                raise ValueError("ball_no must be a whole number from 1 to 500")
            new.ball_no = b
        if "kept" in changes:
            if not isinstance(changes["kept"], bool):
                raise ValueError("kept must be true or false")
            new.kept = changes["kept"]
        if "edited_cm" in changes:
            v = changes["edited_cm"]
            h = self.params.drop_height_cm
            if v is not None and (
                isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v <= h
            ):
                raise ValueError(f"the height must be above 0 and no more than the drop ({h:g} cm)")
            new.edited_cm = None if v is None else float(v)
        drop.__dict__.update(new.__dict__)
        self.version += 1

    def kept(self) -> list[ReviewDrop]:
        return [d for d in self.drops if d.kept]

    def outliers(self) -> dict[int, float]:
        """Kept drops that don't fit a normal distribution of the kept drops, at
        their reviewed heights: seq -> z (sd from the others' mean). Flagged only;
        the user decides whether to keep them."""
        kept = self.kept()
        flags, _ = outlier_json([d.height_cm for d in kept])
        return {d.seq: z for d, z in zip(kept, flags, strict=True) if z is not None}

    def summary(self) -> dict:
        s = summarize((d.ball_no, d.height_cm) for d in self.kept())
        counts: dict[int, int] = {}
        for d in self.kept():
            counts[d.ball_no] = counts.get(d.ball_no, 0) + 1
        _, fit = outlier_json([d.height_cm for d in self.kept()])
        return {
            "balls": s.balls,
            "mean_cm": s.mean_cm,
            "sd_cm": s.sd_cm,
            "medians": [
                {"ball_no": b, "rebound_cm": m, "drops": counts[b]} for b, m in s.medians.items()
            ],
            "drops_fit": fit,  # a normal distribution of the kept drops, outliers left out
        }

    def image(self, seq: int, closeup: bool = False) -> bytes | None:
        """A drop's picture as JPEG, or with ``closeup`` the ball at its apex."""
        if (seq, closeup) not in self._jpeg:
            r = self.analyzer.result
            img = (r.closeups if closeup else r.images).get(seq)
            if img is None:
                return None
            quality = 90 if closeup else 80
            jpeg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])[1].tobytes()
            self._jpeg[seq, closeup] = jpeg
        return self._jpeg[seq, closeup]

    def to_json(self) -> dict:
        details = {d.seq: d for _, d in self.analyzer.result.entries}
        outliers = self.outliers()
        return {
            "version": self.version,
            "drops": [
                {
                    **asdict(d),
                    "height_cm": d.height_cm,
                    "outlier_z": outliers.get(d.seq),
                    "release_cm": details[d.seq].release_cm,
                    "method": details[d.seq].method,
                    "warnings": list(details[d.seq].warnings),
                    "image": d.seq in self.analyzer.result.images,
                    "closeup": d.seq in self.analyzer.result.closeups,
                }
                for d in self.drops
            ],
            "summary": self.summary(),
        }

    def export(self, ts: float, filename: str) -> dict:
        """The reviewed test with its analysis and pictures, in one document: what
        Save to Pi will send (PLAN.md 6.3, tables bounce_video_*)."""
        p = self.params
        result = self.analyzer.result.to_json()
        detail = {d["seq"]: d for d in result["drops"]}
        outliers = self.outliers()
        return {
            "key": self.key,
            "tool": TOOL,
            "test": {
                "ts": ts,
                "batch_id": p.batch_id,
                "setup_id": p.setup_id,
                "context": p.context,
                "source": "video",
                "drops": [{"ball_no": d.ball_no, "rebound_cm": d.height_cm} for d in self.kept()],
            },
            "set_up": asdict(p),
            "summary": self.summary(),
            "analysis": {
                "filename": filename,
                "video": {
                    **result["video"],
                    "frames": result["frames"],
                    "holds": result["holds"],
                    "calibration": result["calibration"],
                },
                "params": {"tool": TOOL, **result["params"]},
            },
            "drops": [
                {
                    "seq": d.seq,
                    "ball_no": d.ball_no,
                    "rebound_cm": d.measured_cm,
                    "kept": d.kept,
                    "edited_cm": d.edited_cm,
                    "outlier_z": outliers.get(d.seq),
                    "detail": {k: v for k, v in detail[d.seq].items() if k != "ball_no"},
                    "image_jpeg": b64(self.image(d.seq)),
                    "closeup_jpeg": b64(self.image(d.seq, closeup=True)),
                }
                for d in self.drops
            ],
        }


def b64(data: bytes | None) -> str | None:
    return None if data is None else base64.b64encode(data).decode("ascii")


# --- the preview -------------------------------------------------------------------------

# OpenCV colours are BGR.
GREEN, ORANGE, GREY, WHITE = (60, 220, 60), (40, 150, 255), (170, 170, 170), (255, 255, 255)


def draw_preview(frame: np.ndarray, obs, analyzer: Analyzer | None) -> bytes:
    """A small JPEG of the frame with the ball outlined, the calibration rows and
    the latest drops' apex lines."""
    s = min(1.0, PREVIEW_ROWS / frame.shape[0])
    img = (
        cv2.resize(frame, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else frame.copy()
    )
    w = img.shape[1]

    def hline(row: float, colour, label: str, x0: int = 0, x1: int | None = None) -> None:
        y = int(round(row * s))
        cv2.line(img, (x0, y), (x1 or w, y), colour, 1, cv2.LINE_AA)
        cv2.putText(
            img, label, (x0 + 4, y - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.45, colour, 1, cv2.LINE_AA
        )

    if analyzer is not None:
        m = analyzer.measurer
        cal = m.calibration
        if cal is not None:
            labels = ["floor", "mid mark", "drop mark"][-len(cal.rows) :]
            if len(labels) == 2:
                labels[0] = "floor"
            for h, label in zip(cal.heights_cm, labels, strict=True):
                hline(cal.row_for(h), ORANGE, label)
        else:
            for i, hold in enumerate(m.holds, start=1):
                hline(hold.row, GREY, f"hold {i}")
        count: dict[int, int] = {}
        recent = []
        for ball, d in analyzer.result.entries:
            count[ball] = count.get(ball, 0) + 1
            recent.append((ball, count[ball], d))
        cx = int((obs.cx if obs is not None else w / s / 2) * s)
        for ball, n, d in recent[-3:]:
            hline(d.apex_row, GREEN, f"{ball}.{n}", max(cx - 90, 0), min(cx + 90, w))
    if obs is not None:
        r = max(int(obs.width * s / 2), 3)
        cv2.circle(img, (int(obs.cx * s), int(obs.cy * s)), r + 3, WHITE, 2, cv2.LINE_AA)
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, PREVIEW_QUALITY])[1].tobytes()


def frame_at(path: Path, t: float) -> np.ndarray | None:
    """The frame of a video file at about ``t`` seconds."""
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            return None
        cap.set(cv2.CAP_PROP_POS_MSEC, max(t, 0.0) * 1000)
        ok, frame = cap.read()
        return frame if ok else None
    finally:
        cap.release()


# --- recording -----------------------------------------------------------------------------


class Recorder:
    """Writes a live session to ``<name>.mp4``, with the capture time of every frame
    in ``<name>.times.txt`` so the recording can be re-analysed with true timing."""

    def __init__(self, path: Path, fps: float) -> None:
        self.path = path
        self.fps = fps
        self.lost = 0
        self.error: str | None = None
        self._times: list[float] = []
        self._q: queue.Queue = queue.Queue(maxsize=max(int(fps * 4), 30))
        self._thread = threading.Thread(target=self._run, name="recorder", daemon=True)
        self._thread.start()

    def put(self, t: float, frame: np.ndarray) -> None:
        try:
            self._q.put_nowait((t, frame))
        except queue.Full:
            self.lost += 1

    def close(self) -> None:
        self._q.put(None)
        self._thread.join()

    def _run(self) -> None:
        writer = None
        item = ...
        try:
            while (item := self._q.get()) is not None:
                t, frame = item
                if writer is None:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    h, w = frame.shape[:2]
                    writer = cv2.VideoWriter(
                        str(self.path), cv2.VideoWriter_fourcc(*"mp4v"), self.fps, (w, h)
                    )
                    if not writer.isOpened():
                        self.error = f"can't write {self.path}"
                        writer = None
                        break
                writer.write(frame)
                self._times.append(t)
        except Exception as e:  # a full disk must not stop the analysis
            log.exception("recording failed")
            self.error = f"recording stopped: {e}"
        finally:
            if writer is not None:
                writer.release()
                times_path(self.path).write_text("".join(f"{t:.6f}\n" for t in self._times))
            while item is not None:  # stopped early: drain until close()
                item = self._q.get()


# --- sessions ------------------------------------------------------------------------------


class Run:
    """What both kinds of session share: phase, analyzer, preview, commands, review."""

    kind = ""

    def __init__(self, run_id: int, recordings: Path, save_key: str | None = None) -> None:
        self.id = run_id
        self.recordings = recordings
        self.save_key = save_key  # the review's key on the Pi (Review.key)
        self.saved: dict | None = None  # the last Save to Pi: sent, or kept to send later
        self.phase = "starting"
        self.error: str | None = None
        self.notice: str | None = None
        self.params: Params | None = None
        self.started_ts = time.time()
        self.detector = BallDetector()
        self.analyzer: Analyzer | None = None
        self.review: Review | None = None
        self.file: Path | None = None  # the clip, or the live recording
        self.duration_s: float | None = None
        self.holds_seen = 0  # calibration holds long enough so far (since the last reset)
        self._preview: tuple[int, bytes] | None = None
        self._preview_cond = threading.Condition()
        self._preview_due = 0.0
        self._preview_times: list[float] = []
        self._obs = None
        self._last_frame: np.ndarray | None = None
        self._commands: queue.Queue = queue.Queue()
        self._accepting = True  # until the analysis thread ends
        self._commands_lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # --- called from the web app --------------------------------------------------------

    def command(self, name: str, **kw) -> object:
        """Run a command on the analysis thread and wait for it."""
        fut: Future = Future()
        with self._commands_lock:
            if not self._accepting:
                raise RuntimeError("this session has ended")
            self._commands.put((name, kw, fut))
        return fut.result(timeout=COMMAND_TIMEOUT_S)

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(timeout=10)

    def wait_preview(self, after: int, timeout: float) -> tuple[int, bytes] | None:
        with self._preview_cond:
            self._preview_cond.wait_for(
                lambda: self._preview is not None and self._preview[0] > after, timeout
            )
            return self._preview

    def preview_fps(self) -> float | None:
        ts = self._preview_times
        return (len(ts) - 1) / (ts[-1] - ts[0]) if len(ts) >= 5 and ts[-1] > ts[0] else None

    def state(self) -> dict:
        out = {
            "session": self.id,
            "kind": self.kind,
            "phase": self.phase,
            "error": self.error,
            "notice": self.notice,
            "params": None if self.params is None else asdict(self.params),
            "file": None if self.file is None else self.file.name,
            "duration_s": self.duration_s,
            "preview_fps": self.preview_fps(),
            "ball_seen": self._obs is not None,
            "color_picked": self.detector.color != ColorRange(),
            "area": None if self.detector.area is None else [list(p) for p in self.detector.area],
            "saved": self.saved,
        }
        if self.analyzer is not None and self.phase != "review":
            out.update(self._progress())
        if self.review is not None:
            out["review"] = self.review.to_json()
        return out

    def _progress(self) -> dict:
        a = self.analyzer
        assert a is not None and self.params is not None
        count: dict[int, int] = {}
        drops = []
        for ball, d in a.result.entries:
            count[ball] = count.get(ball, 0) + 1
            drops.append(
                {
                    "seq": d.seq,
                    "ball_no": ball,
                    "drop": count[ball],
                    "rebound_cm": d.rebound_cm,
                    "release_cm": d.release_cm,
                    "warnings": list(d.warnings),
                }
            )
        cal = a.measurer.calibration
        return {
            "calibration": None if cal is None else {"method": cal.method},
            "next": {"ball": a.session.next_ball, "drop": a.session.next_drop},
            "prompt": self._prompt(),
            "drops": drops,
            "frames": {"done": a.result.frames, "ball_found": a.result.ball_found},
        }

    def _prompt(self) -> dict:
        return {"text": "", "detail": None, "warn": None}

    # --- on the analysis thread ---------------------------------------------------------

    def _analyse(self, t: float, frame: np.ndarray) -> None:
        assert self.analyzer is not None
        found = self.analyzer.result.ball_found
        events = self.analyzer.process(t, frame)
        seen = self.analyzer.result.ball_found > found
        self._obs = self.detector.track[-1] if seen and self.detector.track else None
        for e in events:
            if isinstance(e, HoldEvent):
                self.holds_seen = max(self.holds_seen, e.count)

    def _maybe_preview(self, frame: np.ndarray, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now < self._preview_due:
            return
        self._preview_due = max(self._preview_due + 1 / PREVIEW_FPS, now)
        jpeg = draw_preview(frame, self._obs, self.analyzer)
        self._preview_times = [*self._preview_times[-24:], now]
        with self._preview_cond:
            seq = self._preview[0] + 1 if self._preview else 1
            self._preview = (seq, jpeg)
            self._preview_cond.notify_all()

    def _run_commands(self) -> None:
        while True:
            try:
                name, kw, fut = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                handler = getattr(self, f"_cmd_{name}", None)
                if handler is None:
                    raise ValueError(f"{name!r} can't be done now")
                fut.set_result(handler(**kw))
            except Exception as e:
                fut.set_exception(e)

    def _fail_commands(self) -> None:
        """The analysis thread is ending: refuse queued and later commands."""
        with self._commands_lock:
            self._accepting = False
        while True:
            try:
                _, _, fut = self._commands.get_nowait()
            except queue.Empty:
                return
            fut.set_exception(RuntimeError("the session has ended"))

    def _cmd_pick(self, x: float, y: float) -> dict:
        """Teach the ball's colour from a click at (x, y), as fractions of the frame."""
        frame = self._last_frame
        if frame is None:
            raise ValueError("no picture yet")
        h, w = frame.shape[:2]
        px, py = x * w, y * h
        color = ColorRange.around(sample_color(frame, px, py))
        self.detector.color = color
        self.detector.start_point = (px, py)
        self.detector.track.clear()
        self.detector.last_seen = None
        return asdict(color)

    def _cmd_area(self, area: object) -> None:
        """Track the ball only inside ``area`` (see detect.check_area); None: anywhere."""
        if self.kind == "clip":
            raise ValueError("a clip's tracking area is set with Analyse again")
        self.detector.set_area(check_area(area))
        self._obs = None

    def _to_review(self) -> None:
        assert self.analyzer is not None and self.params is not None
        self.analyzer.finish()
        self.review = Review(self.analyzer, self.params, self.save_key)
        self.save_key = self.review.key
        self.phase = "review"

    def _fail(self, message: str) -> None:
        log.error("session %d: %s", self.id, message)
        self.error = message
        self.phase = "error"


class LiveRun(Run):
    """A camera: framing, then live (analysed and recorded), then review."""

    kind = "camera"

    def __init__(
        self,
        run_id: int,
        recordings: Path,
        source: FrameSource,
        rotation: int = 0,
        area: Area | None = None,
    ) -> None:
        super().__init__(run_id, recordings)
        self.detector.area = area
        self.source = source
        self.rotation = rotation  # applied to each frame before anything else sees it
        self.phase = "framing"
        self.fps: float | None = None  # measured
        self.skipped = 0  # frames the analysis couldn't keep up with
        self.recorder: Recorder | None = None
        self._t0: float | None = None  # capture time when Start was pressed
        maxsize = max(int(QUEUE_S * (source.fps or 30)), 10)
        self._frames: queue.Queue = queue.Queue(maxsize=maxsize)
        self._threads = [
            threading.Thread(target=self._capture, name="capture", daemon=True),
            threading.Thread(target=self._process, name="analysis", daemon=True),
        ]
        for t in self._threads:
            t.start()

    def _size(self) -> tuple[int, int]:
        """The picture's width and height after rotation."""
        src = self.source
        return (src.height, src.width) if self.rotation in (90, 270) else (src.width, src.height)

    def state(self) -> dict:
        out = super().state()
        src = self.source
        width, height = self._size()
        warn = None
        if self.fps is not None and self.fps < FPS_WARN:
            warn = (
                f"The camera gives only {self.fps:.0f} fps: more light helps "
                "(webcams slow down in dim light, which also blurs the ball)."
            )
        out["source"] = {
            "name": src.name,
            "width": width,
            "height": height,
            "rotation": self.rotation,
            "fps_nominal": src.fps,
            "fps": self.fps,
            "fps_warning": warn,
            "skipped": self.skipped,
            "recording": None if self.recorder is None else self.recorder.path.name,
            "recording_error": None if self.recorder is None else self.recorder.error,
            "recording_lost": 0 if self.recorder is None else self.recorder.lost,
        }
        return out

    def _prompt(self) -> dict:
        a = self.analyzer
        assert a is not None and self.params is not None
        m = a.measurer
        ball, drop = a.session.next_ball, a.session.next_drop
        if m.taking_holds:
            names = ["on the floor", "at the mid mark", "at the drop mark"]
            if self.params.mid_mark_cm is None:
                names.pop(1)
            want = len(names)
            holding = self.holds_seen > len(m.holds)  # the current hold is long enough
            failed = len(m.holds) >= want  # holds ended without making a calibration
            warn = "Those holds didn't line up. Start again from the floor." if failed else None
            if holding and self.holds_seen % want == 0:
                return {
                    "text": "Let go when ready",
                    "detail": f"That's ball {ball}, drop {drop}. Catch it after the first bounce.",
                    "warn": warn,
                }
            k = self.holds_seen % want
            detail = "Its bottom on the mark; keep it still for 2 seconds."
            if k == want - 1:
                detail += f" Then let go: that's ball {ball}, drop {drop}."
            return {"text": f"Hold the ball still {names[k]}", "detail": detail, "warn": warn}
        warn = None
        if m.calibration is None:
            warn = (
                "Not calibrated: heights assume a level camera. Press Recalibrate and do the holds."
            )
        size = self.params.sample_size
        if size is not None and ball > size:
            return {"text": f"All {size} balls done", "detail": "Press Finish.", "warn": warn}
        latest = a.result.entries[-1][1].warnings if a.result.entries else ()
        if warn is None and any("camera move" in w for w in latest):
            warn = "The floor has moved in the picture: did the camera move? Recalibrate."
        return {
            "text": f"Ball {ball}, drop {drop}",
            "detail": "Release it with its bottom at the drop mark. "
            "Catch it after the first bounce.",
            "warn": warn,
        }

    def _capture(self) -> None:
        times: list[float] = []
        try:
            while not self._stop.is_set():
                item = self.source.read()
                if item is None:
                    if not self._stop.is_set():
                        self._frames.put(None)  # the camera stopped
                    return
                t, frame = item
                times = [*times[-60:], t]
                if len(times) >= 10 and times[-1] > times[0]:
                    self.fps = (len(times) - 1) / (times[-1] - times[0])
                if self.source.drops_when_late:
                    try:
                        self._frames.put_nowait((t, frame))
                    except queue.Full:
                        self.skipped += 1
                else:
                    while not self._stop.is_set():
                        try:
                            self._frames.put((t, frame), timeout=0.2)
                            break
                        except queue.Full:
                            continue
        except Exception as e:
            log.exception("capture failed")
            self._frames.put(("error", e))
        finally:
            self.source.close()

    def _process(self) -> None:
        try:
            while not self._stop.is_set():
                self._run_commands()
                if self.phase == "review":
                    return
                try:
                    item = self._frames.get(timeout=0.1)
                except queue.Empty:
                    continue
                if item is None or item[0] == "error":
                    why = "the camera stopped giving pictures" if item is None else str(item[1])
                    if self.phase == "live":
                        self._finish()
                        self.notice = f"The session ended early: {why}."
                    else:
                        self._fail(f"Camera: {why}")
                    return
                t, frame = item
                frame = rotated(frame, self.rotation)
                self._last_frame = frame
                if self.phase == "live":
                    if self._t0 is None:
                        self._t0 = t
                        self.detector.restart_clock(t)
                    t -= self._t0
                    assert self.recorder is not None
                    self.recorder.put(t, frame)
                    self._analyse(t, frame)
                    self.duration_s = t
                else:
                    self._obs = self.detector.detect(frame, t)
                self._maybe_preview(frame)
        except Exception as e:
            log.exception("analysis failed")
            self._fail(f"Analysis failed: {e}")
        finally:
            self._stop.set()
            if self.recorder is not None and self.phase != "review":
                self.recorder.close()
            self._fail_commands()

    # --- commands --------------------------------------------------------------------

    def _cmd_rotate(self, rotation: object, area: object = None) -> None:
        """Turn the picture; ``area`` is the tracking area for the turned picture."""
        if self.phase != "framing":
            raise ValueError("the picture can only be rotated before Start")
        turn, region = check_rotation(rotation), check_area(area)
        self.rotation = turn
        self.detector.forget()
        self.detector.area = region
        self._obs = None

    def _cmd_start(self, params: Params) -> None:
        if self.phase != "framing":
            raise ValueError("already started")
        self.params = params
        src = self.source
        info = VideoInfo("camera", src.name, *self._size(), src.fps, 0)
        self.analyzer = Analyzer(
            info, params.drop_height_cm, params.mid_mark_cm, params.drops_per_ball, self.detector
        )
        self.file = self.recordings / f"{stamp()}.mp4"
        self.recorder = Recorder(self.file, self.fps or src.fps or 30.0)
        self.analyzer.result.video = replace(info, name=str(self.file))
        self.started_ts = time.time()
        self.phase = "live"
        begin = getattr(src, "start", None)  # the synthetic camera's session begins now
        if begin is not None:
            begin()

    def _live(self) -> Analyzer:
        if self.phase != "live" or self.analyzer is None:
            raise ValueError("only during a live session")
        return self.analyzer

    def _cmd_undo(self) -> bool:
        return self._live().undo() is not None

    def _cmd_next_ball(self) -> None:
        self._live().skip_ball()

    def _cmd_recalibrate(self) -> None:
        self._live().recalibrate()
        self.holds_seen = 0

    def _cmd_finish(self) -> None:
        self._live()
        self._finish()

    def _finish(self) -> None:
        assert self.recorder is not None
        self._to_review()
        self.recorder.close()
        if self.recorder.error:
            self.notice = self.recorder.error
        self._stop.set()  # releases the camera


class ClipRun(Run):
    """A video file, analysed as fast as it decodes, then reviewed."""

    kind = "clip"

    def __init__(
        self,
        run_id: int,
        recordings: Path,
        path: Path,
        params: Params,
        color: ColorRange | None = None,
        area: Area | None = None,
        save_key: str | None = None,
    ) -> None:
        super().__init__(run_id, recordings, save_key)
        self.file = path
        self.params = params
        self.phase = "analysing"
        self.total_frames = 0
        self.detector = BallDetector(color=color, area=area)
        self._threads = [threading.Thread(target=self._process, name="clip", daemon=True)]
        self._threads[0].start()

    def state(self) -> dict:
        out = super().state()
        if self.analyzer is not None:
            out["source"] = asdict(self.analyzer.result.video)
            out["progress"] = {"done": self.analyzer.result.frames, "total": self.total_frames}
        return out

    def _prompt(self) -> dict:
        return {"text": "Analysing the clip", "detail": None, "warn": None}

    def _process(self) -> None:
        assert self.file is not None and self.params is not None
        p = self.params
        try:
            info, frames = open_clip(self.file)
            self.total_frames = info.frames
            self.duration_s = info.frames / info.fps if info.fps else None
            self.analyzer = Analyzer(
                info, p.drop_height_cm, p.mid_mark_cm, p.drops_per_ball, self.detector
            )
            for t, frame in frames:
                if self._stop.is_set():
                    frames.close()
                    return
                self._run_commands()
                self._last_frame = frame
                self._analyse(t, frame)
                self._maybe_preview(frame)
            self.duration_s = self.duration_s or None
            self._to_review()
        except ValueError as e:
            self._fail(str(e).replace(str(self.file), self.file.name))
        except Exception as e:
            log.exception("clip analysis failed")
            self._fail(f"Analysis failed: {e}")
        finally:
            self._fail_commands()


# --- one session at a time -------------------------------------------------------------------


class Controller:
    """Holds the current session for the web app."""

    def __init__(self, recordings: Path, open_camera: Callable[[str], FrameSource]) -> None:
        self.recordings = recordings
        self.open_camera = open_camera
        self.run: Run | None = None
        self._next_id = 1
        self._lock = threading.RLock()

    def _replace(self, make: Callable[[int], Run]) -> Run:
        with self._lock:
            self.close()
            run = make(self._next_id)
            self._next_id += 1
            self.run = run
            return run

    def camera(self, key: str, rotation: object = 0, area: object = None) -> Run:
        turn, region = check_rotation(rotation), check_area(area)
        with self._lock:
            self.close()  # a camera can't be opened twice
            source = self.open_camera(key)  # raises ValueError if it can't
            return self._replace(lambda i: LiveRun(i, self.recordings, source, turn, region))

    def clip(
        self,
        path: Path,
        params: Params,
        color: ColorRange | None = None,
        area: Area | None = None,
        save_key: str | None = None,
    ) -> Run:
        return self._replace(
            lambda i: ClipRun(i, self.recordings, path, params, color, area, save_key)
        )

    def reanalyse(self, pick: dict | None, area: object = KEEP) -> Run:
        """Analyse the current session's clip or recording again, optionally with the
        ball's colour taught by a click on one of its frames, and a tracking area
        (by default the one the session had)."""
        region = None if area is KEEP else check_area(area)
        with self._lock:
            run = self.run
            if run is None or run.phase not in ("review", "error") or run.file is None:
                raise ValueError("nothing to analyse again")
            if run.params is None or not run.file.is_file():
                raise ValueError("the video file is gone")
            color = None
            if pick is not None:
                frame = frame_at(run.file, float(pick["t"]))
                if frame is None:
                    raise ValueError("can't read that frame")
                h, w = frame.shape[:2]
                color = ColorRange.around(sample_color(frame, pick["x"] * w, pick["y"] * h))
            if area is KEEP:
                region = run.detector.area
            saved = run.saved
            new = self.clip(run.file, run.params, color, region, run.save_key)
            if saved is not None:  # the same test on the Pi, but its drops are measured anew
                new.saved = {**saved, "version": None}
            return new

    def current(self, session: int | None = None) -> Run:
        run = self.run
        if run is None or (session is not None and run.id != session):
            raise LookupError("that session is no longer open")
        return run

    def close(self) -> None:
        with self._lock:
            if self.run is not None:
                self.run.stop()
                self.run = None

    def state(self) -> dict:
        run = self.run
        if run is None:
            return {"session": None, "phase": "idle"}
        return run.state()
