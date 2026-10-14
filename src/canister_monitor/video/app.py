"""The laptop web app for video bounce tests (PLAN.md 6.3): ``canister-video`` with
no arguments.

It serves a page on this laptop only (http://127.0.0.1:5100). The browser is
just the screen: capture and analysis run here, in ``session.Controller``. The
page polls ``/api/state`` a few times a second and shows ``/preview.mjpg``.
Batches and setups come from the Pi's API. Saving a reviewed test to the Pi
comes in a later step; meanwhile the review downloads as JSON.
"""

import json
import logging
import threading
import time
import webbrowser
from collections.abc import Callable
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request
from werkzeug.utils import secure_filename

from canister_monitor import __version__
from canister_monitor.video.pi import PiClient, PiError
from canister_monitor.video.session import Controller, Params, frame_at, stamp
from canister_monitor.video.sources import CameraSource, FrameSource, probe_cameras

log = logging.getLogger(__name__)

DEFAULT_PORT = 5100
DEFAULT_PI = "http://canisters.local:5000"
UPLOAD_CHUNK = 1 << 20


def webcams() -> list[dict]:
    return [
        {"key": str(c.index), "label": f"Camera {c.index}", **c.__dict__} for c in probe_cameras()
    ]


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message, self.status = message, status


def create_app(
    recordings: Path,
    pi: PiClient,
    open_camera: Callable[[str], FrameSource] | None = None,
    probe: Callable[[], list[dict]] | None = None,
) -> Flask:
    """``open_camera(key)`` and ``probe()`` default to real webcams; tests pass a
    synthetic camera instead."""
    app = Flask(__name__)
    app.json.sort_keys = False

    def default_open(key: str) -> FrameSource:
        if not key.isdigit():
            raise ValueError(f"no camera {key!r}")
        return CameraSource(int(key))

    controller = Controller(recordings, open_camera or default_open)
    probe = probe or webcams
    cameras: dict = {"list": None, "busy": threading.Lock()}
    app.extensions["video"] = {"controller": controller, "pi": pi, "recordings": recordings}

    @app.errorhandler(ApiError)
    def _api_error(e: ApiError):
        return jsonify({"error": e.message}), e.status

    def body() -> dict:
        b = request.get_json(silent=True)
        if not isinstance(b, dict):
            raise ApiError("request body must be a JSON object")
        return b

    def call(fn: Callable, *args, **kw):
        """Run a controller call, turning its exceptions into replies for the page."""
        try:
            return fn(*args, **kw)
        except ValueError as e:
            raise ApiError(str(e)) from None
        except LookupError as e:
            raise ApiError(str(e).strip("'\""), 404) from None
        except TimeoutError:
            raise ApiError("the session didn't answer in time", 504) from None
        except RuntimeError as e:
            raise ApiError(str(e), 409) from None

    def state():
        return jsonify(controller.state())

    @app.get("/")
    def index():
        return render_template("video.html", version=__version__, pi_url=pi.base_url)

    @app.get("/api/state")
    def get_state():
        return state()

    # --- the Pi ------------------------------------------------------------------------

    @app.get("/api/pi")
    def get_pi():
        try:
            data = pi.bounce()
        except PiError as e:
            return jsonify({"url": pi.base_url, "ok": False, "error": str(e)})
        batches = [
            {
                "id": b["id"],
                "brand": b["brand"],
                "model": b["model"],
                "opened_ts": b["opened_ts"],
                "sample_size": b["sample_size"],
                "tests": len(b.get("tests", [])),
                "in_canister": bool((b.get("location") or {}).get("in_canister")),
            }
            for b in data.get("batches", [])
            if b.get("retired_ts") is None
        ]
        return jsonify(
            {
                "url": pi.base_url,
                "ok": True,
                "error": None,
                "settings": data.get("settings", {}),
                "setups": data.get("setups", []),
                "batches": batches,
            }
        )

    # --- sources -----------------------------------------------------------------------

    @app.get("/api/cameras")
    def get_cameras():
        """Webcams that give a picture, with their sizes. Probing takes a few seconds,
        so the list is kept until ``?refresh=1``."""
        if request.args.get("refresh") or cameras["list"] is None:
            run = controller.run
            if run is not None and run.kind == "camera" and run.phase in ("framing", "live"):
                raise ApiError("close the camera before looking for others", 409)
            with cameras["busy"]:
                cameras["list"] = probe()
        return jsonify(cameras["list"])

    @app.post("/api/camera")
    def open_camera_route():
        key = body().get("key")
        if not isinstance(key, str):
            raise ApiError("key must be text")
        call(controller.camera, key)
        return state()

    @app.post("/api/start")
    def start():
        params = call(Params.from_json, body())
        call(lambda: controller.current().command("start", params=params))
        return state()

    @app.post("/api/command/<name>")
    def command(name: str):
        if name not in ("undo", "next_ball", "recalibrate", "finish"):
            raise ApiError(f"unknown command {name!r}", 404)
        call(lambda: controller.current().command(name))
        return state()

    @app.post("/api/pick")
    def pick():
        b = body()
        x, y = b.get("x"), b.get("y")
        if not all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in (x, y)):
            raise ApiError("x and y must be fractions of the picture, 0 to 1")
        call(lambda: controller.current().command("pick", x=float(x), y=float(y)))
        return state()

    @app.post("/api/upload")
    def upload():
        """A clip as the raw request body (the page shows the upload's progress)."""
        name = secure_filename(request.args.get("name", "")) or "clip.mp4"
        recordings.mkdir(parents=True, exist_ok=True)
        path = recordings / f"{stamp()}-{name}"
        size = 0
        try:
            with path.open("wb") as f:
                while chunk := request.stream.read(UPLOAD_CHUNK):
                    f.write(chunk)
                    size += len(chunk)
        except OSError as e:
            path.unlink(missing_ok=True)
            raise ApiError(f"couldn't save the clip: {e}", 500) from None
        if size == 0:
            path.unlink(missing_ok=True)
            raise ApiError("the upload was empty")
        return jsonify({"file": path.name, "bytes": size}), 201

    def recording(name: object) -> Path:
        if not isinstance(name, str) or not name or Path(name).name != name:
            raise ApiError("file must be a file name from the upload")
        path = recordings / name
        if not path.is_file():
            raise ApiError(f"{name} not found", 404)
        return path

    @app.post("/api/clip")
    def clip():
        b = body()
        params = call(Params.from_json, b.get("params"))
        call(controller.clip, recording(b.get("file")), params)
        return state()

    @app.post("/api/reanalyse")
    def reanalyse():
        pick = body().get("pick")
        if pick is not None:
            ok = isinstance(pick, dict) and all(
                isinstance(pick.get(k), (int, float)) for k in ("t", "x", "y")
            )
            if not ok or not (0 <= pick["x"] <= 1 and 0 <= pick["y"] <= 1):
                raise ApiError("pick must have t (seconds) and x, y (fractions of the picture)")
        call(controller.reanalyse, pick)
        return state()

    @app.get("/api/frame.jpg")
    def frame():
        """A frame of the session's clip or recording, for clicking the ball."""
        import cv2

        run = call(controller.current)
        if run.file is None or not run.file.is_file():
            raise ApiError("this session has no video file", 404)
        try:
            t = float(request.args.get("t", "0"))
        except ValueError:
            raise ApiError("t must be a number of seconds") from None
        img = frame_at(run.file, t)
        if img is None:
            raise ApiError("can't read that frame", 404)
        scale = min(1.0, 720 / img.shape[0])
        if scale < 1:
            img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        jpeg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()
        return Response(jpeg, mimetype="image/jpeg")

    @app.post("/api/close")
    def close():
        controller.close()
        return state()

    # --- review ------------------------------------------------------------------------

    def review():
        run = call(controller.current)
        if run.review is None:
            raise ApiError("there's nothing to review yet", 409)
        return run, run.review

    @app.post("/api/review/<int:seq>")
    def edit(seq: int):
        _, rev = review()
        call(rev.edit, seq, body())
        return state()

    @app.get("/api/drops/<int:seq>.jpg")
    def drop_image(seq: int):
        _, rev = review()
        jpeg = rev.image(seq)
        if jpeg is None:
            raise ApiError(f"no picture for drop {seq}", 404)
        return Response(jpeg, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/review.json")
    def download():
        run, rev = review()
        doc = rev.export(run.started_ts, run.file.name if run.file else "")
        name = f"bounce-test-{time.strftime('%Y%m%d-%H%M', time.localtime(run.started_ts))}.json"
        return Response(
            json.dumps(doc, indent=1),
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    # --- preview -----------------------------------------------------------------------

    @app.get("/preview.mjpg")
    def preview():
        """The current session's preview as MJPEG, until the session changes."""
        try:
            session = int(request.args.get("session", ""))
        except ValueError:
            raise ApiError("session must be a number") from None
        run = call(controller.current, session)

        def frames():
            last = 0
            while controller.run is run:
                got = run.wait_preview(last, timeout=1.0)
                if got is None:
                    if run.phase in ("review", "error"):
                        return
                    continue
                seq, jpeg = got
                if seq == last and run.phase in ("review", "error"):
                    return
                last = seq
                yield (
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(jpeg)).encode()
                    + b"\r\n\r\n"
                    + jpeg
                    + b"\r\n"
                )

        return Response(
            frames(),
            mimetype="multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-store"},
        )

    return app


def serve(
    port: int = DEFAULT_PORT,
    pi_url: str = DEFAULT_PI,
    recordings: Path = Path("recordings"),
    open_browser: bool = True,
    demo_camera: bool = False,
) -> int:
    """Run the app until Ctrl+C."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    open_camera = probe = None
    if demo_camera:
        from canister_monitor.video import synth

        def open_camera(key: str) -> FrameSource:
            return synth.demo_camera() if key == "demo" else CameraSource(int(key))

        def probe() -> list[dict]:
            cam = synth.demo_camera()
            cam.close()
            demo = {"key": "demo", "label": "Demo camera (synthetic)", "index": None}
            demo |= {"width": cam.width, "height": cam.height, "fps": cam.fps}
            return [demo, *webcams()]

    app = create_app(recordings, PiClient(pi_url), open_camera, probe)

    from werkzeug.serving import make_server

    try:
        server = make_server("127.0.0.1", port, app, threaded=True)
    except OSError as e:
        print(f"error: can't listen on port {port}: {e}")
        return 2
    url = f"http://127.0.0.1:{port}"
    print(f"canister-video: {url} (Ctrl+C to stop). Recordings go to {recordings.resolve()}")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.extensions["video"]["controller"].close()
    return 0
