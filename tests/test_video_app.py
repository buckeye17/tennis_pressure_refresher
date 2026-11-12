"""The laptop web app for video bounce tests (PLAN.md 6.3), through Flask's test client
with a synthetic camera standing in for a webcam. Needs the laptop-only ``video``
extra; skipped where OpenCV isn't installed (e.g. on the Pi)."""

import json
import statistics
import time
from types import SimpleNamespace

import pytest

cv2 = pytest.importorskip("cv2")

import numpy as np  # noqa: E402

from canister_monitor.video import outbox, synth  # noqa: E402
from canister_monitor.video.app import create_app  # noqa: E402
from canister_monitor.video.measure import Measurer, Observation  # noqa: E402
from canister_monitor.video.pi import PiClient, PiError  # noqa: E402
from canister_monitor.video.session import Params, Review, ReviewDrop, Run  # noqa: E402
from canister_monitor.video.sources import rotated  # noqa: E402

H = 182.88  # 72 in
MID = 91.44  # 36 in
SMALL = {"width": 360, "height": 640}

PARAMS = {
    "drop_height_cm": H,
    "mid_mark_cm": MID,
    "drops_per_ball": 2,
    "batch_id": 7,
    "batch_label": "Penn Championship",
    "sample_size": 2,
    "setup_id": 1,
    "setup_name": "Garage door",
    "context": "before",
}


class FakePi:
    base_url = "http://pi.test:5000"

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok

    def bounce(self) -> dict:
        if not self.ok:
            raise PiError("can't reach http://pi.test:5000: refused")
        return {
            "settings": {"display_units": "in", "contexts": ["new", "before", "after"]},
            "setups": [{"id": 1, "name": "Garage door", "drop_cm": H, "is_itf": False}],
            "batches": [
                {
                    "id": 7,
                    "brand": "Penn",
                    "model": "Championship",
                    "opened_ts": 1.7e9,
                    "sample_size": 2,
                    "retired_ts": None,
                    "location": {"in_canister": True, "mac": "x"},
                    "tests": [{}],
                },
                {"id": 8, "brand": "Old", "model": "Box", "opened_ts": 1.6e9, "sample_size": 3}
                | {"retired_ts": 1.65e9, "location": None, "tests": []},
            ],
        }

    def canisters(self) -> dict:
        if not self.ok:
            raise PiError("can't reach http://pi.test:5000: refused")
        return {
            "canisters": [
                {"mac": "w", "canister": "Old one", "stale": True, "reading": {"temp_c": 30.0}},
                {"mac": "y", "canister": "Canister 2", "stale": False, "reading": {"temp_c": 19.0}},
                {"mac": "x", "canister": "Canister 1", "stale": False, "reading": {"temp_c": 21.0}},
                {"mac": "z", "canister": None, "stale": False, "reading": None},
            ]
        }


class PiApp:
    """The real dashboard app behind PiClient's interface, so a review's document goes
    through the Pi's real endpoint. ``down`` makes it unreachable."""

    base_url = "http://pi.test:5000"

    def __init__(self, tmp_path) -> None:
        from canister_monitor.config import parse_config
        from canister_monitor.web import create_app as create_pi_app

        app = create_pi_app(parse_config({}), db_path=tmp_path / "pi.db", clock=lambda: 1.7e9)
        self.client = app.test_client()
        self.down = False
        self.sent = 0
        setup = {"name": "Garage door", "drop_cm": H, "mid_mark_cm": MID}
        self.setup_id = self.client.post("/api/bounce/setups", json=setup).get_json()["id"]
        batch = {"brand": "Penn", "model": "Championship", "n_balls": 4}
        self.batch_id = self.client.post("/api/bounce/batches", json=batch).get_json()["id"]

    def bounce(self) -> dict:
        return self.client.get("/api/bounce").get_json()

    def canisters(self) -> dict:
        return self.client.get("/api/canisters").get_json()

    def save_video_test(self, doc: dict) -> tuple[int, dict]:
        if self.down:
            raise PiError("can't reach http://pi.test:5000: timed out")
        resp = self.client.post("/api/bounce/video-tests", json=doc)
        if resp.status_code >= 400:
            raise PiError(f"the Pi refused: {resp.get_json()['error']}", resp.status_code)
        self.sent += 1
        return resp.status_code, resp.get_json()

    def tests(self) -> list[dict]:
        return [t for b in self.bounce()["batches"] for t in b["tests"]]


def corrected(truth_drop: dict) -> float:
    return truth_drop["rebound_cm"] * H / truth_drop["release_cm"]


def make_client(tmp_path, cameras: dict | None = None, pi=None):
    cameras = cameras or {}

    def open_camera(key):
        if key not in cameras:
            raise ValueError(f"no camera {key!r}")
        return cameras[key]()

    def probe():
        return [{"key": k, "label": k, "width": 360, "height": 640, "fps": 30.0} for k in cameras]

    app = create_app(tmp_path / "recordings", pi or FakePi(), open_camera, probe)
    return app, app.test_client()


def wait_for(client, predicate, timeout=90.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = client.get("/api/state").get_json()
        if predicate(st):
            return st
        if st["phase"] == "error":
            raise AssertionError(st["error"])
        time.sleep(0.02)
    raise AssertionError(f"timed out; last state: {json.dumps(st)[:2000]}")


def first_mjpeg_frame(client, session) -> bytes:
    resp = client.get(f"/preview.mjpg?session={session}", buffered=False)
    assert resp.mimetype == "multipart/x-mixed-replace"
    chunk = next(iter(resp.response))
    resp.close()
    return chunk


# --- set-up and review, without threads ----------------------------------------------


def test_params_validation():
    p = Params.from_json(PARAMS)
    assert p.drops_per_ball == 2 and p.batch_id == 7
    assert Params.from_json({"drop_height_cm": 100}).drops_per_ball == 3
    for bad, msg in [
        ({}, "drop_height_cm"),
        ({"drop_height_cm": 100, "mid_mark_cm": 120}, "mid mark"),
        ({"drop_height_cm": 100, "drops_per_ball": 0}, "drops_per_ball"),
        ({"drop_height_cm": 100, "colour": "x"}, "unknown"),
        ({"drop_height_cm": True}, "drop_height_cm"),
    ]:
        with pytest.raises(ValueError, match=msg):
            Params.from_json(bad)


def test_review_drop_height_prefers_the_edit():
    d = ReviewDrop(0, 1, 100.0)
    assert d.height_cm == 100.0
    d.edited_cm = 98.0
    assert d.height_cm == 98.0


# The first real webcam session (2026-11-11): 9 balls x 3 drops from 200.66 cm. Drop 17
# was cut short by a bad frame; its track peaked near 111.5 cm.
REAL_SESSION_CM = [
    112.58, 111.75, 114.60, 111.98, 111.27, 113.16, 111.69, 111.33, 114.69,
    109.90, 109.98, 112.65, 109.96, 108.24, 112.36, 110.08, 101.84, 112.40,
    114.33, 111.82, 113.87, 107.62, 106.96, 110.34, 108.44, 107.99, 110.74,
]  # fmt: skip


def review_of(heights: list[float]) -> Review:
    """A Review over made-up drops, without an analysis thread behind it."""
    entries = [
        (
            i // 3 + 1,
            SimpleNamespace(
                seq=i, rebound_cm=h, release_cm=200.66, method="projective", warnings=()
            ),
        )
        for i, h in enumerate(heights)
    ]
    analyzer = SimpleNamespace(result=SimpleNamespace(entries=entries, images={}, closeups={}))
    return Review(analyzer, Params(drop_height_cm=200.66, mid_mark_cm=100.33))


def test_review_flags_the_drop_that_does_not_fit_the_others():
    rev = review_of(REAL_SESSION_CM)
    out = rev.to_json()
    flagged = {d["seq"]: d["outlier_z"] for d in out["drops"] if d["outlier_z"] is not None}
    assert list(flagged) == [16]
    assert flagged[16] == pytest.approx(-4.3, abs=0.1)
    fit = out["summary"]["drops_fit"]
    assert (fit["drops"], fit["outliers"], fit["tested"]) == (27, 1, True)
    assert fit["mean_cm"] == pytest.approx(111.18, abs=0.01)  # without the outlier

    rev.edit(16, {"edited_cm": 111.5})  # corrected to what its track showed
    assert all(d["outlier_z"] is None for d in rev.to_json()["drops"])
    rev.edit(16, {"edited_cm": None})
    rev.edit(16, {"kept": False})  # or left out: it no longer counts, so isn't flagged
    out = rev.to_json()
    assert all(d["outlier_z"] is None for d in out["drops"])
    assert out["summary"]["drops_fit"]["drops"] == 26


def test_review_does_not_look_for_outliers_in_a_few_drops():
    out = review_of([110.0, 111.0, 95.0]).to_json()
    assert all(d["outlier_z"] is None for d in out["drops"])
    assert out["summary"]["drops_fit"]["tested"] is False
    assert review_of([]).to_json()["summary"]["drops_fit"] is None


def test_pi_states(tmp_path):
    _, client = make_client(tmp_path)
    pi = client.get("/api/pi").get_json()
    assert pi["ok"] and [b["id"] for b in pi["batches"]] == [7]  # the retired batch is left out
    assert pi["batches"][0] == {
        "id": 7,
        "brand": "Penn",
        "model": "Championship",
        "opened_ts": 1.7e9,
        "sample_size": 2,
        "tests": 1,
        "in_canister": True,
        "mac": "x",
    }
    _, client = make_client(tmp_path, pi=FakePi(ok=False))
    pi = client.get("/api/pi").get_json()
    assert not pi["ok"] and "refused" in pi["error"]


def test_pi_client_reports_an_unreachable_pi():
    with pytest.raises(PiError, match="can't reach"):
        PiClient("http://127.0.0.1:9", timeout=1.0).bounce()


def test_page_and_idle_state(tmp_path):
    _, client = make_client(tmp_path)
    page = client.get("/")
    assert page.status_code == 200 and b"Ball Bounce Tracker" in page.data
    assert client.get("/static/video.js").status_code == 200
    assert client.get("/api/state").get_json() == {"session": None, "phase": "idle"}


# --- a live session through the synthetic camera ------------------------------------------


def test_live_session_end_to_end(tmp_path):
    cam = synth.Camera.framing(H, 8.0, **SMALL)
    spec = synth.SessionSpec(cor=(0.74, 0.70), drops_per_ball=2, seed=5)
    made: list[synth.SyntheticCamera] = []

    def camera():
        made.append(synth.SyntheticCamera(cam, spec, speed=8.0))
        return made[-1]

    _, client = make_client(tmp_path, {"synth": camera})
    assert client.get("/api/cameras").get_json()[0]["key"] == "synth"
    st = client.post("/api/camera", json={"key": "synth"}).get_json()
    assert st["phase"] == "framing" and st["source"]["width"] == 360
    session = st["session"]
    assert first_mjpeg_frame(client, session).startswith(b"--frame\r\nContent-Type: image/jpeg")

    # Commands that need a live session are refused while framing.
    assert client.post("/api/command/undo").status_code == 400
    bad = dict(PARAMS, mid_mark_cm=500)
    assert client.post("/api/start", json=bad).status_code == 400

    st = client.post("/api/start", json=PARAMS).get_json()
    assert st["phase"] == "live" and st["source"]["recording"].endswith(".mp4")
    assert st["prompt"]["text"] == "Hold the ball still on the floor"

    st = wait_for(client, lambda s: s["calibration"] is not None)
    assert st["calibration"]["method"] == "projective"
    st = wait_for(client, lambda s: len(s["drops"]) == 3)
    assert [d["ball_no"] for d in st["drops"]] == [1, 1, 2]
    st = client.post("/api/command/undo").get_json()  # ball 2's first drop was fumbled
    assert len(st["drops"]) == 2 and st["prompt"]["text"] == "Ball 2, drop 1"
    st = wait_for(client, lambda s: len(s["drops"]) == 3)
    st = client.post("/api/command/finish").get_json()
    assert st["phase"] == "review"
    assert client.post("/api/command/undo").status_code == 409  # the session has ended

    truth = made[0].truth.drops
    rev = st["review"]
    assert [d["ball_no"] for d in rev["drops"]] == [1, 1, 2]
    assert [d["seq"] for d in rev["drops"]] == [0, 1, 3]
    for d, t in zip(rev["drops"], [truth[0], truth[1], truth[3]], strict=True):
        assert d["measured_cm"] == pytest.approx(corrected(t), rel=0.005)
        assert d["image"] and d["closeup"]
    for url in ("/api/drops/0.jpg", "/api/drops/0/closeup.jpg"):
        img = client.get(url)
        assert img.mimetype == "image/jpeg" and img.data[:2] == b"\xff\xd8"
    assert client.get("/api/drops/42/closeup.jpg").status_code == 404

    # Review edits: type over one height, reject one drop, move one to another ball.
    assert client.post("/api/review/0", json={"edited_cm": 999}).status_code == 400
    client.post("/api/review/0", json={"edited_cm": 100.0})
    client.post("/api/review/1", json={"kept": False})
    st = client.post("/api/review/3", json={"ball_no": 3}).get_json()
    rev = st["review"]
    assert rev["drops"][0]["height_cm"] == 100.0 and rev["drops"][0]["edited_cm"] == 100.0
    assert [(m["ball_no"], m["drops"]) for m in rev["summary"]["medians"]] == [(1, 1), (3, 1)]
    assert rev["summary"]["medians"][0]["rebound_cm"] == 100.0
    assert client.post("/api/review/42", json={"kept": False}).status_code == 404

    doc = json.loads(client.get("/api/review.json").data)
    assert doc["test"]["batch_id"] == 7 and doc["test"]["source"] == "video"
    assert doc["test"]["drops"] == [
        {"ball_no": 1, "rebound_cm": 100.0},
        {"ball_no": 3, "rebound_cm": rev["drops"][2]["measured_cm"]},
    ]
    assert [(d["seq"], d["kept"], d["edited_cm"]) for d in doc["drops"]] == [
        (0, True, 100.0),
        (1, False, None),
        (3, True, None),
    ]
    assert doc["drops"][0]["detail"]["rows"]["apex"] > 0
    assert len(doc["drops"][0]["image_jpeg"]) > 1000
    assert len(doc["drops"][0]["closeup_jpeg"]) > 1000
    assert doc["analysis"]["video"]["calibration"]["method"] == "projective"
    assert doc["analysis"]["filename"] == st["file"]

    # The recording, with its capture times, re-analyses as a clip: all four drops.
    rec = tmp_path / "recordings" / st["file"]
    assert rec.stat().st_size > 10_000
    assert rec.with_suffix(".times.txt").is_file()
    st = client.post("/api/reanalyse", json={"pick": None}).get_json()
    assert st["phase"] == "analysing" and st["session"] != session
    st = wait_for(client, lambda s: s["phase"] == "review")
    drops = st["review"]["drops"]
    assert [d["ball_no"] for d in drops] == [1, 1, 2, 2]
    for d, t in zip(drops, truth, strict=True):
        assert d["measured_cm"] == pytest.approx(corrected(t), rel=0.005)


def test_pick_and_recalibrate_and_close(tmp_path):
    cam = synth.Camera.framing(H, **SMALL)
    spec = synth.SessionSpec(cor=(0.74,), drops_per_ball=1)
    _, client = make_client(tmp_path, {"s": lambda: synth.SyntheticCamera(cam, spec, speed=2.0)})
    st = client.post("/api/camera", json={"key": "s"}).get_json()
    first_mjpeg_frame(client, st["session"])
    assert client.post("/api/pick", json={"x": 2, "y": 0.5}).status_code == 400
    st = client.post("/api/pick", json={"x": 0.5, "y": 0.5}).get_json()
    assert st["color_picked"]
    client.post("/api/start", json=dict(PARAMS, drops_per_ball=1))
    st = client.post("/api/command/recalibrate").get_json()
    assert st["phase"] == "live" and st["calibration"] is None
    assert client.get(f"/preview.mjpg?session={st['session'] + 1}").status_code == 404
    st = client.post("/api/close").get_json()
    assert st == {"session": None, "phase": "idle"}
    assert client.post("/api/command/finish").status_code == 404


class SidewaysCamera:
    """A synthetic camera turned on its side, so real down is at the picture's left."""

    def __init__(self, inner: synth.SyntheticCamera) -> None:
        self.inner = inner
        self.name = "sideways"
        self.width, self.height, self.fps = inner.height, inner.width, inner.fps
        self.drops_when_late = inner.drops_when_late

    def start(self) -> None:
        self.inner.start()

    def read(self):
        t, frame = self.inner.read()
        return t, cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)

    def close(self) -> None:
        self.inner.close()


def test_rotated_turns_the_picture_clockwise():
    frame = np.zeros((2, 3), np.uint8)
    frame[1, 0] = 255  # bottom left
    assert rotated(frame, 0) is frame
    assert rotated(frame, 90)[0, 0] == 255 and rotated(frame, 90).shape == (3, 2)
    assert rotated(frame, 180)[0, 2] == 255
    assert rotated(frame, 270)[2, 1] == 255 and rotated(frame, 270).shape == (3, 2)


def test_sideways_camera_measures_once_rotated(tmp_path):
    cam = synth.Camera.framing(H, 8.0, **SMALL)
    spec = synth.SessionSpec(cor=(0.74,), drops_per_ball=2, seed=5)
    made: list[synth.SyntheticCamera] = []

    def camera():
        made.append(synth.SyntheticCamera(cam, spec, speed=8.0))
        return SidewaysCamera(made[-1])

    _, client = make_client(tmp_path, {"side": camera})
    st = client.post("/api/camera", json={"key": "side"}).get_json()
    assert (st["source"]["width"], st["source"]["height"]) == (640, 360)

    for bad in (45, True, "90", None):
        assert client.post("/api/rotate", json={"rotation": bad}).status_code == 400
    st = client.post("/api/rotate", json={"rotation": 270}).get_json()
    assert st["source"]["rotation"] == 270
    assert (st["source"]["width"], st["source"]["height"]) == (360, 640)

    st = client.post("/api/start", json=PARAMS).get_json()
    assert st["phase"] == "live"
    assert client.post("/api/rotate", json={"rotation": 0}).status_code == 400

    wait_for(client, lambda s: len(s["drops"]) == 2)
    st = client.post("/api/command/finish").get_json()
    drops = st["review"]["drops"]
    assert len(drops) == 2
    for d, t in zip(drops, made[0].truth.drops, strict=True):
        assert d["measured_cm"] == pytest.approx(corrected(t), rel=0.005)

    # The recording is upright, so it re-analyses without knowing about the rotation.
    video = cv2.VideoCapture(str(tmp_path / "recordings" / st["file"]))
    ok, frame = video.read()
    video.release()
    assert ok and frame.shape[:2] == (640, 360)
    doc = json.loads(client.get("/api/review.json").data)
    assert (doc["analysis"]["video"]["width"], doc["analysis"]["video"]["height"]) == (360, 640)


class MonotonicClockCamera:
    """A webcam's clock (large, not starting at 0) and a yellow blob seen before Start
    that then leaves, as a passing false detection does. After Start, a ball moves
    somewhere else in the picture."""

    name = "clocked"
    width, height, fps = 360, 640, 30.0
    drops_when_late = False

    def __init__(self) -> None:
        self.i = 0
        self.began: int | None = None

    def start(self) -> None:
        self.began = self.i

    def read(self):
        time.sleep(0.002)
        self.i += 1
        frame = np.full((self.height, self.width, 3), 120, np.uint8)
        if self.began is None:
            centre = (40 + 4 * (self.i % 60), 120)
        else:
            centre = (300, 300 + 3 * ((self.i - self.began) % 60))
        cv2.circle(frame, centre, 14, synth.BALL_BGR, -1, cv2.LINE_AA)
        return 500000.0 + self.i / self.fps, frame

    def close(self) -> None:
        pass


def test_ball_is_found_after_start_when_something_was_tracked_before_it(tmp_path):
    _, client = make_client(tmp_path, {"c": MonotonicClockCamera})
    client.post("/api/camera", json={"key": "c"})
    wait_for(client, lambda s: s["ball_seen"])

    assert client.post("/api/start", json=PARAMS).get_json()["phase"] == "live"
    wait_for(client, lambda s: s["frames"]["ball_found"] > 10)
    client.post("/api/close")


def test_camera_opens_with_a_saved_rotation(tmp_path):
    cam = synth.Camera.framing(H, **SMALL)
    spec = synth.SessionSpec(cor=(0.74,), drops_per_ball=1)
    _, client = make_client(tmp_path, {"s": lambda: synth.SyntheticCamera(cam, spec, speed=2.0)})
    assert client.post("/api/camera", json={"key": "s", "rotation": 45}).status_code == 400
    st = client.post("/api/camera", json={"key": "s", "rotation": 90}).get_json()
    assert st["source"]["rotation"] == 90
    assert (st["source"]["width"], st["source"]["height"]) == (640, 360)
    client.post("/api/close")


def test_tracking_area_on_a_camera(tmp_path):
    cam = synth.Camera.framing(H, **SMALL)
    spec = synth.SessionSpec(cor=(0.74,), drops_per_ball=1)
    _, client = make_client(tmp_path, {"s": lambda: synth.SyntheticCamera(cam, spec, speed=2.0)})
    area = [[0.25, 0.0], [0.75, 0.0], [0.5, 1.0]]
    assert client.post("/api/camera", json={"key": "s", "area": [[0, 0]]}).status_code == 400
    st = client.post("/api/camera", json={"key": "s", "area": area}).get_json()
    assert st["area"] == area
    assert client.post("/api/area", json={"area": "x"}).status_code == 400
    assert client.post("/api/area", json={"area": None}).get_json()["area"] is None
    assert client.post("/api/area", json={"area": area}).get_json()["area"] == area
    # The area was drawn on the unturned picture, so turning it drops the area,
    # unless the page sends the one saved for the new rotation.
    assert client.post("/api/rotate", json={"rotation": 90}).get_json()["area"] is None
    st = client.post("/api/rotate", json={"rotation": 0, "area": area}).get_json()
    assert st["area"] == area
    client.post("/api/close")


def test_camera_that_fails_to_open(tmp_path):
    _, client = make_client(tmp_path)
    resp = client.post("/api/camera", json={"key": "3"})
    assert resp.status_code == 400 and "no camera" in resp.get_json()["error"]


# --- clips ------------------------------------------------------------------------------------


def test_clip_upload_and_analysis(tmp_path):
    cam = synth.Camera.framing(H, 10.0, **SMALL)
    spec = synth.SessionSpec(cor=(0.73,), drops_per_ball=2, distractor=False)
    clip = tmp_path / "phone.mp4"
    truth = synth.write_clip(clip, cam, spec)
    _, client = make_client(tmp_path)

    up = client.post("/api/upload?name=../My Clip.mp4", data=clip.read_bytes())
    assert up.status_code == 201
    name = up.get_json()["file"]
    assert name.endswith("-My_Clip.mp4") and "/" not in name
    assert (
        client.post("/api/clip", json={"file": "../phone.mp4", "params": PARAMS}).status_code == 400
    )
    assert client.post("/api/clip", json={"file": "nope.mp4", "params": PARAMS}).status_code == 404

    st = client.post("/api/clip", json={"file": name, "params": dict(PARAMS, drops_per_ball=2)})
    assert st.get_json()["phase"] == "analysing"
    st = wait_for(client, lambda s: s["phase"] == "review")
    drops = st["review"]["drops"]
    assert len(drops) == 2
    for d, t in zip(drops, truth.drops, strict=True):
        assert d["measured_cm"] == pytest.approx(corrected(t), rel=0.005)

    frame = client.get("/api/frame.jpg?t=1.0")
    assert frame.mimetype == "image/jpeg"
    # Analyse again with the colour clicked on a frame where the ball rests on the floor.
    t = 2.5
    u, v, _ = cam.project(0.0, synth.BALL_RADIUS_M)
    pick = {"t": t, "x": u / cam.width, "y": v / cam.height}
    st = client.post("/api/reanalyse", json={"pick": pick}).get_json()
    assert st["color_picked"]
    st = wait_for(client, lambda s: s["phase"] == "review")
    assert len(st["review"]["drops"]) == 2

    # Analyse again inside a tracking area around the drop line; the area is kept for
    # the next analysis unless one is given, and saved with the analysis.
    area = [[0.2, 0.0], [0.8, 0.0], [0.8, 1.0], [0.2, 1.0]]
    bad = client.post("/api/reanalyse", json={"pick": None, "area": [[0, 0]]})
    assert bad.status_code == 400
    st = client.post("/api/reanalyse", json={"pick": None, "area": area}).get_json()
    assert st["area"] == area
    assert client.post("/api/area", json={"area": None}).status_code == 400  # not for a clip
    st = wait_for(client, lambda s: s["phase"] == "review")
    assert len(st["review"]["drops"]) == 2
    st = client.post("/api/reanalyse", json={"pick": None}).get_json()
    assert st["area"] == area
    wait_for(client, lambda s: s["phase"] == "review")
    doc = json.loads(client.get("/api/review.json").data)
    assert doc["analysis"]["params"]["area"] == area
    st = client.post("/api/reanalyse", json={"pick": None, "area": None}).get_json()
    assert st["area"] is None
    wait_for(client, lambda s: s["phase"] == "review")


# --- saving to the Pi ------------------------------------------------------------------------


def test_save_to_pi_end_to_end_with_the_pi_away_at_first(tmp_path):
    """A clip's review goes through the Pi's real endpoint: kept on the laptop while the
    Pi is away, sent later, then saved again in place after edits and Analyse again."""
    cam = synth.Camera.framing(H, 10.0, **SMALL)
    spec = synth.SessionSpec(cor=(0.73, 0.70), drops_per_ball=2, distractor=False)
    clip = tmp_path / "phone.mp4"
    synth.write_clip(clip, cam, spec)
    pi = PiApp(tmp_path)
    app, client = make_client(tmp_path, pi=pi)
    recordings = tmp_path / "recordings"
    params = dict(PARAMS, batch_id=pi.batch_id, setup_id=pi.setup_id, context="after")

    name = client.post("/api/upload?name=phone.mp4", data=clip.read_bytes()).get_json()["file"]
    client.post("/api/clip", json={"file": name, "params": params})
    st = wait_for(client, lambda s: s["phase"] == "review")
    assert st["saved"] is None and len(st["review"]["drops"]) == 4

    assert client.post("/api/save", json={"temp_c": 99}).status_code == 400
    pi.down = True
    st = client.post("/api/save", json={"temp_c": 21.5, "note": " garage "}).get_json()
    assert st["saved"]["status"] == "queued" and "timed out" in st["saved"]["error"]
    kept = recordings / st["saved"]["file"]
    assert kept.name == name.removesuffix(".mp4") + ".unsent.json"
    [item] = client.get("/api/outbox").get_json()["items"]
    assert (item["batch"], item["context"], item["drops"]) == ("Penn Championship", "after", 4)
    res = client.post("/api/outbox/send").get_json()
    assert res["sent"] == 0 and len(res["items"]) == 1 and "timed out" in res["failed"][0]["error"]

    pi.down = False
    res = client.post("/api/outbox/send").get_json()
    assert (res["sent"], res["items"], res["failed"]) == (1, [], [])
    assert not kept.exists() and kept.with_name(kept.name.replace(".unsent", ".sent")).is_file()
    st = client.get("/api/state").get_json()
    assert st["saved"]["status"] == "saved" and st["saved"]["created"]
    [test] = pi.tests()
    assert test["id"] == st["saved"]["test_id"]
    assert (test["source"], test["context"], test["temp_c"], test["note"]) == (
        "video",
        "after",
        21.5,
        "garage",
    )
    measured = [d["measured_cm"] for d in st["review"]["drops"]]
    assert [d["rebound_cm"] for d in test["drops"]] == pytest.approx(measured)
    assert [d["ball_no"] for d in test["drops"]] == [1, 1, 2, 2]
    assert test["video"]["filename"] == name and test["video"]["calibration"] == "projective"
    pic = pi.client.get(f"/api/bounce/video-drops/{test['video']['drops'][0]['id']}.jpg")
    assert pic.mimetype == "image/jpeg" and len(pic.data) > 1000

    # Edited after saving: Save again updates the same test.
    st = client.post("/api/review/1", json={"kept": False}).get_json()
    assert st["saved"]["version"] != st["review"]["version"]
    st = client.post("/api/save", json={"temp_c": 21.5}).get_json()
    assert st["saved"]["status"] == "saved" and not st["saved"]["created"]
    [test] = pi.tests()
    assert len(test["drops"]) == 3 and [d["kept"] for d in test["video"]["drops"]].count(False) == 1

    # Analyse again re-measures the same session: still the same test on the Pi.
    client.post("/api/reanalyse", json={"pick": None})
    st = wait_for(client, lambda s: s["phase"] == "review")
    assert st["saved"]["status"] == "saved" and st["saved"]["version"] is None
    st = client.post("/api/save", json={"temp_c": None}).get_json()
    assert not st["saved"]["created"]
    [test] = pi.tests()
    assert len(test["drops"]) == 4 and test["temp_c"] is None

    # A test the Pi refuses isn't kept to send later: the page shows why.
    pi.client.delete(f"/api/bounce/batches/{pi.batch_id}")
    resp = client.post("/api/save", json={})
    assert resp.status_code == 400 and "not found" in resp.get_json()["error"]
    assert client.get("/api/outbox").get_json()["items"] == []
    assert pi.sent == 3


def test_save_needs_a_session_set_up_from_the_pi(tmp_path):
    app, client = make_client(tmp_path)
    assert client.post("/api/save", json={}).status_code == 404  # no session
    run = Run(1, tmp_path)
    run.params = Params(drop_height_cm=200.66)  # measured with the Pi away at set-up
    run.review = review_of([110.0, 111.0])
    run.phase = "review"
    app.extensions["video"]["controller"].run = run
    resp = client.post("/api/save", json={"temp_c": 20.0})
    assert resp.status_code == 409 and "batch and setup" in resp.get_json()["error"]


def test_room_temperatures_from_the_pi(tmp_path):
    _, client = make_client(tmp_path)
    temps = client.get("/api/pi/temps").get_json()
    assert temps["ok"] and [t["mac"] for t in temps["temps"]] == ["y", "x"]  # fresh only
    _, client = make_client(tmp_path, pi=FakePi(ok=False))
    temps = client.get("/api/pi/temps").get_json()
    assert not temps["ok"] and temps["temps"] == []


def test_outbox_skips_unreadable_files(tmp_path):
    (tmp_path / "a.unsent.json").write_text("{not json")
    outbox.keep(tmp_path / "b.unsent.json", {"key": "k1", "test": {"drops": [{}]}})
    assert [p.name for p, _ in outbox.pending(tmp_path)] == ["b.unsent.json"]
    outbox.mark_sent_by_key(tmp_path, "k1")
    assert outbox.pending(tmp_path) == [] and (tmp_path / "b.sent.json").is_file()


def test_pi_client_saves_over_http_and_keeps_the_pis_reason(tmp_path):
    import threading

    from werkzeug.serving import make_server

    pi = PiApp(tmp_path)
    server = make_server("127.0.0.1", 0, pi.client.application, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = PiClient(f"http://127.0.0.1:{server.port}")
        drop = {"seq": 0, "ball_no": 1, "rebound_cm": 120.0, "kept": True, "edited_cm": None}
        doc = {
            "key": "http-test-1",
            "test": {"batch_id": pi.batch_id, "setup_id": pi.setup_id, "context": "new"},
            "analysis": {"filename": "x.mp4", "video": {}, "params": {}},
            "drops": [drop],
        }
        status, test = client.save_video_test(doc)
        assert status == 201 and test["source"] == "video"
        assert client.save_video_test(doc)[0] == 200  # the same key again: replaced
        with pytest.raises(PiError, match="the Pi refused: .*batch 99 not found") as e:
            client.save_video_test({**doc, "test": {**doc["test"], "batch_id": 99}})
        assert e.value.status == 400 and not e.value.retry_later
    finally:
        server.shutdown()
    assert PiError("x").retry_later and PiError("x", 503).retry_later


def test_not_a_video(tmp_path):
    _, client = make_client(tmp_path)
    name = client.post("/api/upload?name=notes.txt", data=b"hello").get_json()["file"]
    client.post("/api/clip", json={"file": name, "params": PARAMS})
    end = time.monotonic() + 10
    while (st := client.get("/api/state").get_json())["phase"] == "analysing":
        assert time.monotonic() < end
        time.sleep(0.02)
    assert st["phase"] == "error" and st["error"] == f"can't open {name} as a video"
    assert client.post("/api/upload?name=empty.mp4", data=b"").status_code == 400


# --- recalibrating part way (the measurer) -------------------------------------------------


def observations(cam, spec, t0=0.0, fps=30.0):
    tr, truth = synth.build(spec, cam)
    out = []
    for i in range(int(tr.end * fps)):
        t = i / fps
        x, y, _ = tr.at(t)
        u, v, ppm = cam.project(x, y + synth.BALL_RADIUS_M)
        r = synth.BALL_RADIUS_M * ppm
        seen = 0 <= u <= cam.width
        out.append((t0 + t, Observation(t0 + t, u, v, 2 * r, 2 * r, v + r) if seen else None))
    return out, truth


def test_recalibrating_after_the_camera_moved():
    """Drops keep the calibration they were measured with; each is refined on its own."""
    spec = synth.SessionSpec(release_sd_cm=1.5, cor=(0.75,), drops_per_ball=3, seed=2)
    before, truth_a = observations(synth.Camera.framing(H, 0.0), spec)
    after, truth_b = observations(synth.Camera.framing(H, 12.0), spec, t0=before[-1][0] + 1)
    m = Measurer(H, MID)
    for t, o in before:
        m.feed(t, o)
    first = m.calibration
    m.reset_calibration()
    for t, o in after:
        m.feed(t, o)
    events = m.finish()
    assert m.calibration is not None and m.calibration.p != first.p
    assert type(events[-1]).__name__ == "RefinedEvent"
    assert len(m.drops) == 6
    for drops, truth in ((m.drops[:3], truth_a), (m.drops[3:], truth_b)):
        releases = [t["release_cm"] for t in truth.drops]
        typical = statistics.median([releases[0], *releases])
        for d, t in zip(drops, truth.drops, strict=True):
            assert d.rebound_cm == pytest.approx(
                t["rebound_cm"] * typical / t["release_cm"], rel=0.003
            )


def test_undone_drop_leaves_the_refinement():
    spec = synth.SessionSpec(release_sd_cm=0.0, cor=(0.75,), drops_per_ball=3)
    obs, _ = observations(synth.Camera.framing(H), spec)
    m = Measurer(H, MID)
    for t, o in obs:
        m.feed(t, o)
    assert m.discard(1) and not m.discard(1)
    m.finish()
    assert [d.seq for d in m.drops] == [0, 2]
