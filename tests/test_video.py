"""Bounce-video measurement (PLAN.md 6.3). Needs the laptop-only ``video`` extra;
skipped where OpenCV isn't installed (e.g. on the Pi)."""

import json
import math
import statistics
from dataclasses import replace

import pytest

cv2 = pytest.importorskip("cv2")

import numpy as np  # noqa: E402

from canister_monitor.video import cli, measure, pipeline, synth  # noqa: E402
from canister_monitor.video.detect import BallDetector, check_area  # noqa: E402
from canister_monitor.video.measure import Measurer, Observation, fit_calibration  # noqa: E402

H = 182.88  # 72 in
MID = 91.44  # 36 in
SMALL = {"width": 360, "height": 640}  # renders faster; still ~6 px ball radius


def corrected(truth_drop: dict) -> float:
    """The rebound the tool should report: scaled to a release exactly at the mark."""
    return truth_drop["rebound_cm"] * H / truth_drop["release_cm"]


# --- finding the ball -------------------------------------------------------------------

BALL_R, SPECK_R = 20, 7  # in a 1280-row frame: 40 px wide passes the size limit, 14 px doesn't


def disc(x: int, y: int, r: int) -> np.ndarray:
    frame = np.full((1280, 720, 3), 120, np.uint8)
    cv2.circle(frame, (x, y), r, synth.BALL_BGR, -1, cv2.LINE_AA)
    return frame


def test_a_moving_speck_is_not_picked_up_but_a_ball_is():
    det = BallDetector()
    for i in range(30):
        assert det.detect(disc(100 + 8 * i, 300, SPECK_R), i / 30) is None
    seen = [det.detect(disc(100 + 8 * i, 700, BALL_R), (30 + i) / 30) for i in range(30)]
    assert any(o is not None for o in seen)


def test_following_does_not_slide_from_a_ball_onto_a_speck():
    det = BallDetector()
    i = 0
    for i in range(30):
        last = det.detect(disc(300, 200 + 8 * i, BALL_R), i / 30)
    assert last is not None
    # Each step is small enough to look like the same ball; the end result isn't one.
    for r in range(BALL_R, SPECK_R - 1, -1):
        i += 1
        last = det.detect(disc(300, 440, r), i / 30)
    assert last is None


def blob(x: int, y: int, rx: int, ry: int) -> np.ndarray:
    frame = np.full((1280, 720, 3), 120, np.uint8)
    cv2.ellipse(frame, (x, y), (rx, ry), 0, 0, 360, synth.BALL_BGR, -1, cv2.LINE_AA)
    return frame


def picked_up(det: BallDetector, y: int) -> int:
    """Carry a ball sideways at row ``y`` until the detector follows it; the frame count."""
    for i in range(30):
        if det.detect(disc(200 + 5 * i, y, BALL_R), i / 30) is not None and det.track:
            return i + 1
    raise AssertionError("never picked up")


def test_following_keeps_the_ball_through_a_fast_bounce():
    """Going on as it was, the ball would be a frame's fall below the floor; it's a
    frame's rise above it. On a real session that lost some drops at random, and a
    live session and its recording lost different ones."""
    det = BallDetector()
    n = picked_up(det, 200)
    x = 200 + 5 * (n - 1)
    fall = [230, 290, 380, 500, 650, 800, 950, 1100]  # 150 px a frame by the floor
    rise = [960, 840, 730, 630]
    for k, y in enumerate(fall + rise):
        o = det.detect(disc(x, y, BALL_R), (n + k) / 30)
        assert o is not None and o.cy == pytest.approx(y, abs=2), (k, y)


def test_one_narrow_frame_does_not_lose_the_ball():
    """Compressed video can lose a fast ball's blurred sides for a frame; the ball
    after it, wide again (here wider, as at the floor), is still the ball."""
    det = BallDetector()
    n = picked_up(det, 200)
    x = 200 + 5 * (n - 1)
    shapes = [(BALL_R, BALL_R), (17, 22), (13, 30), (22, 22), (BALL_R, BALL_R)]
    for k, (rx, ry) in enumerate(shapes):
        y = 230 + 30 * k
        o = det.detect(blob(x, y, rx, ry), (n + k) / 30)
        assert o is not None and o.cy == pytest.approx(y, abs=2), (k, rx)


def test_a_clicked_ball_may_be_small():
    det = BallDetector(start_point=(300.0, 400.0))
    seen = [det.detect(disc(300, 400, SPECK_R), i / 30) for i in range(10)]
    assert all(o is not None for o in seen)


RIGHT_HALF = ((0.5, 0.0), (1.0, 0.0), (1.0, 1.0), (0.5, 1.0))  # x past 360 of 720


def two_balls(left_y: int, right_y: int) -> np.ndarray:
    frame = disc(150, left_y, BALL_R)
    cv2.circle(frame, (550, right_y), BALL_R - 2, synth.BALL_BGR, -1, cv2.LINE_AA)
    return frame


def test_a_ball_outside_the_tracking_area_is_ignored():
    # The left ball is bigger, so without an area it is the one picked up.
    frames = [two_balls(300 + 8 * i, 900 - 8 * i) for i in range(30)]
    plain = BallDetector()
    free = [plain.detect(f, i / 30) for i, f in enumerate(frames)]
    assert any(o is not None and o.cx < 360 for o in free)
    det = BallDetector(area=RIGHT_HALF)
    seen = [det.detect(f, i / 30) for i, f in enumerate(frames)]
    assert any(o is not None for o in seen)
    assert all(o.cx > 360 for o in seen if o is not None)


def test_drawing_an_area_lets_go_of_a_ball_outside_it():
    det = BallDetector()
    for i in range(30):
        last = det.detect(disc(150, 300 + 8 * i, BALL_R), i / 30)
    assert last is not None and det.track
    det.set_area(RIGHT_HALF)
    assert not det.track and det.last_seen is None
    assert det.detect(disc(150, 300 + 8 * 30, BALL_R), 1.0) is None
    det.set_area(None)
    assert det.area is None


@pytest.mark.parametrize(
    "bad",
    [
        "x",
        [[0, 0], [1, 0]],  # two points
        [[0, 0], [1, 0], [1, 1.5]],  # off the picture
        [[0, 0], [1, 0], [True, 1]],
        [[0, 0], [1, 0], [1]],
        [[0, 0], [0.5, 0.5], [1, 1]],  # a line
        [[0, 0], [1, 0]] * 40,  # too many
    ],
)
def test_check_area_refuses_what_is_not_a_polygon(bad):
    with pytest.raises(ValueError):
        check_area(bad)


def test_check_area():
    assert check_area(None) is None
    assert check_area([[0, 0], [1, 0], [0.5, 1]]) == ((0.0, 0.0), (1.0, 0.0), (0.5, 1.0))


# --- calibration maths ------------------------------------------------------------------


def test_projective_calibration_inverts_a_tilted_camera():
    cam = synth.Camera.framing(H, 15.0)

    def row(cm):
        return cam.project(0, cm / 100)[1]

    cal = fit_calibration([row(0), row(MID), row(H)], [0.0, MID, H])
    assert cal.method == "projective"
    for cm in (10.0, 55.0, 100.0, 150.0):
        assert cal.height_cm(row(cm)) == pytest.approx(cm, abs=1e-6)
        assert cal.row_for(cm) == pytest.approx(row(cm), abs=1e-6)
    # A straight line through the floor and the drop mark is badly off when tilted:
    lin = fit_calibration([row(0), row(H)], [0.0, H])
    assert lin.method == "linear"
    assert abs(lin.height_cm(row(100.0)) - 100.0) > 3


def test_calibration_rejects_rows_out_of_order():
    assert fit_calibration([500.0, 600.0, 300.0], [0.0, MID, H]) is None
    assert fit_calibration([500.0], [0.0]) is None


# --- the state machine, on exact observations (no rendering) ----------------------------


def observations(
    cam: synth.Camera, spec: synth.SessionSpec, fps: float = 30.0, held_size: float = 1.0
):
    """Perfect per-frame observations of a synthetic session, plus the truth. With
    ``held_size``, a ball in the hand looks that much smaller (fingers hide its edges
    and top; its bottom stays put)."""
    tr, truth = synth.build(spec, cam)
    out = []
    for i in range(int(tr.end * fps)):
        t = i / fps
        x, y, hand = tr.at(t)
        u, v, ppm = cam.project(x, y + synth.BALL_RADIUS_M)
        r = synth.BALL_RADIUS_M * ppm
        k = held_size if hand else 1.0
        visible = 0 <= u <= cam.width
        out.append((t, Observation(t, u, v, 2 * r * k, 2 * r * k, v + r) if visible else None))
    return out, truth


def run(obs, spec):
    m = Measurer(spec.drop_height_cm, spec.mid_mark_cm)
    events = []
    for t, o in obs:
        events += m.feed(t, o)
    events += m.finish()
    return m, events


@pytest.mark.parametrize("tilt", [0.0, 15.0])
def test_measurer_on_exact_observations(tilt):
    spec = synth.SessionSpec(release_sd_cm=0.0, cor=(0.75,), drops_per_ball=3)
    obs, truth = observations(synth.Camera.framing(H, tilt), spec)
    m, events = run(obs, spec)
    assert m.calibration is not None and m.calibration.method == "projective"
    assert [type(e).__name__ for e in events].count("HoldEvent") == 3
    assert len(m.drops) == 3
    for d, t in zip(m.drops, truth.drops, strict=True):
        assert d.rebound_cm == pytest.approx(corrected(t), rel=0.002)
        assert d.floor_cm == pytest.approx(0, abs=1.0)
        assert d.warnings == ()


def test_holds_survive_compression_jitter():
    """Compressed video stores colour at half resolution, so the ball's edge jumps
    about a pixel for a frame or two now and then (seen on the app's recordings).
    That mustn't break a calibration hold, even for a small ball."""
    spec = synth.SessionSpec(release_sd_cm=0.0, cor=(0.75,), drops_per_ball=2)
    cam = synth.Camera.framing(H, 6.0, **SMALL)  # ~6 px radius: the 1 px tolerance floor
    obs, truth = observations(cam, spec)
    jittered, prev = [], None
    for i, (t, o) in enumerate(obs):
        if (
            o is not None
            and prev is not None
            and abs(o.bottom - prev.bottom) < 1e-6
            and i % 8 in (3, 4)
        ):
            o = replace(o, bottom=o.bottom + 1.3)
        jittered.append((t, o))
        prev = obs[i][1]
    m, events = run(jittered, spec)
    assert m.calibration is not None and m.calibration.method == "projective"
    assert len(m.drops) == 2
    for d, t in zip(m.drops, truth.drops, strict=True):
        assert d.rebound_cm == pytest.approx(corrected(t), rel=0.003)


def test_one_bad_frame_does_not_end_the_rise():
    """A single frame whose edge lands well below the ball (a mark on the wall behind
    it) once ended a live rise early and cut a drop 10 cm short."""
    spec = synth.SessionSpec(release_sd_cm=0.0, cor=(0.75,), drops_per_ball=3)
    obs, truth = observations(synth.Camera.framing(H, 10.0), spec)
    clean, _ = run(obs, spec)
    times = [t for t, _ in obs]
    bad = {min(times, key=lambda t, d=d: abs(t - (d.t_apex - 0.1))) for d in clean.drops}
    glitched = [
        (t, replace(o, bottom=o.bottom + 1.25 * o.width) if o is not None and t in bad else o)
        for t, o in obs
    ]
    m, _ = run(glitched, spec)
    assert len(m.drops) == 3
    for d, t in zip(m.drops, truth.drops, strict=True):
        assert d.rebound_cm == pytest.approx(corrected(t), rel=0.003)


def test_a_hand_over_the_held_ball_does_not_make_it_look_small():
    """On a real session the held ball looked ~20% narrower than in flight (fingers
    over its edges), and a ball size taken over the last 2 s, mostly the hold before
    the release, flagged good drops as "ball looks 28 px wide; the scale predicts 35"."""
    spec = synth.SessionSpec(release_sd_cm=0.0, cor=(0.75,), drops_per_ball=3)
    obs, truth = observations(synth.Camera.framing(H, 8.0), spec, held_size=0.78)
    m, _ = run(obs, spec)
    assert len(m.drops) == 3
    for d, t in zip(m.drops, truth.drops, strict=True):
        assert d.rebound_cm == pytest.approx(corrected(t), rel=0.002)
        assert d.warnings == ()


def test_a_ball_that_bounces_off_at_an_angle_is_flagged():
    """The second drop bounces off sideways, 15 cm by the top. The camera is also
    rolled ~4.6 degrees, as on a real session where every fall looked 15 cm slanted:
    that alone mustn't flag anything."""
    spec = synth.SessionSpec(release_sd_cm=0.0, cor=(0.75,), drops_per_ball=3)
    obs, truth = observations(synth.Camera.framing(H, 8.0), spec)
    rolled = [(t, o if o is None else replace(o, cx=o.cx + 0.08 * o.cy)) for t, o in obs]
    clean, _ = run(rolled, spec)
    assert [d.warnings for d in clean.drops] == [()] * 3
    assert all(abs(d.side_cm) < 0.5 for d in clean.drops)

    d1, d2 = clean.drops[1], clean.drops[2]

    def kicked(t, o):
        if o is None or not d1.t_impact < t < d2.t_release:
            return o
        px_per_cm = o.width / measure.BALL_DIAMETER_CM
        frac = (t - d1.t_impact) / (d1.t_apex - d1.t_impact)
        return replace(o, cx=o.cx - 15.0 * px_per_cm * frac)  # to the left

    m, _ = run([(t, kicked(t, o)) for t, o in rolled], spec)
    assert len(m.drops) == 3
    assert m.drops[1].side_cm == pytest.approx(-15.0, abs=1.0)
    assert any("15 cm to the left" in w for w in m.drops[1].warnings)
    for i in (0, 2):
        assert m.drops[i].warnings == ()
    for d, t in zip(m.drops, truth.drops, strict=True):  # sideways alone: same heights
        assert d.rebound_cm == pytest.approx(corrected(t), rel=0.002)


@pytest.mark.parametrize(
    "cam",
    [synth.Camera.framing(H, 8.0), synth.Camera.framing(H, 30.0, vfov_deg=90, distance_m=2.5)],
    ids=["level-ish", "low-wide-angle-looking-up"],
)
def test_coarse_frame_times_and_a_steep_camera_raise_no_warnings(cam):
    """A webcam session timed by a 15.6 ms clock tick (Python's monotonic on Windows),
    and a wide-angle camera near the floor looking up: the heights are fine, so no
    warnings. Seen on a real session, where every drop was flagged."""
    spec = synth.SessionSpec(release_sd_cm=0.0, cor=(0.75,), drops_per_ball=3)
    obs, truth = observations(cam, spec)
    tick = 1 / 64
    coarse = [
        (math.floor(t / tick) * tick, o if o is None else replace(o, t=math.floor(t / tick) * tick))
        for t, o in obs
    ]
    m, _ = run(coarse, spec)
    assert len(m.drops) == 3
    for d, t in zip(m.drops, truth.drops, strict=True):
        assert d.rebound_cm == pytest.approx(corrected(t), rel=0.005)
        assert d.floor_cm == 0.0
        assert d.warnings == ()


@pytest.mark.parametrize("shift_cm", [-15.0, 15.0])
def test_a_camera_that_moved_after_the_holds_is_flagged(shift_cm):
    spec = synth.SessionSpec(release_sd_cm=0.0, cor=(0.75,), drops_per_ball=3)
    obs, _ = observations(synth.Camera.framing(H, 8.0), spec)
    clean, _ = run(obs, spec)
    cal, after = clean.calibration, clean.drops[0].t_apex + 0.4  # bumped after the 1st drop
    px = shift_cm * cal.px_per_cm(cal.rows[0])  # rows grow downward: + is lower in the picture
    moved = [
        (t, o if o is None or t < after else replace(o, cy=o.cy + px, bottom=o.bottom + px))
        for t, o in obs
    ]
    m, _ = run(moved, spec)
    assert len(m.drops) == 3
    assert not any("did the camera move" in w for w in m.drops[0].warnings)
    for d in m.drops[1:]:
        assert any("did the camera move" in w for w in d.warnings)
        assert d.floor_cm * shift_cm < 0  # lower in the picture reads as below the floor


def test_second_bounce_is_not_a_drop():
    spec = synth.SessionSpec(release_sd_cm=0.0, cor=(0.75,), drops_per_ball=2, catch_bounce=2)
    obs, truth = observations(synth.Camera.framing(H), spec)
    m, _ = run(obs, spec)
    assert len(m.drops) == 2
    for d, t in zip(m.drops, truth.drops, strict=True):
        assert d.rebound_cm == pytest.approx(corrected(t), rel=0.002)


def test_release_off_the_mark_is_corrected_and_flagged():
    spec = synth.SessionSpec(release_sd_cm=2.5, cor=(0.75,), drops_per_ball=5, seed=4)
    obs, truth = observations(synth.Camera.framing(H, 8.0), spec)
    m, events = run(obs, spec)
    assert type(events[-1]).__name__ == "RefinedEvent"
    # The only reference for the mark is where the user holds the ball: the
    # median release (the first drop's hold counts twice: it is also a release).
    releases = [t["release_cm"] for t in truth.drops]
    typical = statistics.median([releases[0], *releases])
    for d, t in zip(m.drops, truth.drops, strict=True):
        assert d.release_cm - H == pytest.approx(t["release_cm"] - typical, abs=0.3)
        assert d.rebound_cm == pytest.approx(t["rebound_cm"] * typical / t["release_cm"], rel=0.003)
        off = abs(d.release_cm - H) > 2.0
        assert any("released at" in w for w in d.warnings) == off


def test_without_calibration_holds_it_falls_back_and_says_so():
    spec = synth.SessionSpec(release_sd_cm=0.0, cor=(0.75,), drops_per_ball=2, calibrate=False)
    m, _ = run(observations(synth.Camera.framing(H), spec)[0], spec)
    assert m.calibration is None
    assert len(m.drops) == 2
    assert all(d.method == "self" for d in m.drops)
    assert all("no calibration holds" in " ".join(d.warnings) for d in m.drops)


def test_session_numbering_undo_and_skip():
    s = measure.Session(drops_per_ball=2)
    d = object()
    assert [s.add(d) for _ in range(3)] == [1, 1, 2]
    assert s.undo()[0] == 2
    assert (s.next_ball, s.next_drop) == (2, 1)
    s.undo()
    assert (s.next_ball, s.next_drop) == (1, 2)
    s.skip_ball()
    assert s.add(d) == 2


# --- rendered clips through the real detector --------------------------------------------


def write(tmp_path, cam, spec, name="clip.mp4"):
    path = tmp_path / name
    return path, synth.write_clip(path, cam, spec)


@pytest.mark.parametrize(
    "cam",
    [
        synth.Camera.framing(H, 12.0, **SMALL),  # phone leaning back
        synth.Camera.framing(H, 10.0, width=640, height=360, vfov_deg=45, distance_m=3.2),
    ],
    ids=["portrait-tilted", "landscape-webcam"],
)
def test_clip_end_to_end(tmp_path, cam):
    spec = synth.SessionSpec(cor=(0.74, 0.70), drops_per_ball=2)  # with a static yellow distractor
    path, truth = write(tmp_path, cam, spec)
    result = pipeline.analyze_clip(path, H, MID, drops_per_ball=2)
    assert result.calibration is not None and result.calibration.method == "projective"
    assert [ball for ball, _ in result.entries] == [1, 1, 2, 2]
    for (_, d), t in zip(result.entries, truth.drops, strict=True):
        assert d.rebound_cm == pytest.approx(corrected(t), rel=0.005)
    assert set(result.images) == {0, 1, 2, 3}
    assert set(result.closeups) == {0, 1, 2, 3}
    for img in result.closeups.values():
        # Enlarged, with the measured apex drawn along the bottom of the ball.
        assert img.shape[0] == img.shape[1] >= pipeline.CLOSEUP_PX // 2
        green = (img[:, :, 1] > 200) & (img[:, :, 0] < 90) & (img[:, :, 2] < 90)
        line = int(np.argmax(green.mean(axis=1)))
        assert green[line].mean() > 0.5
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        yellow = np.where(cv2.inRange(hsv, (22, 70, 80), (48, 255, 255)).any(axis=1))[0]
        assert yellow.size and abs(int(yellow.max()) - line) <= 0.05 * img.shape[0]


def test_live_path_with_dropped_frames_matches(tmp_path):
    """Frames straight from the renderer, as a webcam delivers them: uneven timing."""
    cam = synth.Camera.framing(H, 6.0, **SMALL)
    spec = synth.SessionSpec(cor=(0.74,), drops_per_ball=3)
    tr, truth = synth.build(spec, cam)
    times = [i / cam.fps for i in range(int(tr.end * cam.fps)) if i % 17 != 5]  # ~6% lost
    info = pipeline.VideoInfo("camera", "synthetic", cam.width, cam.height, cam.fps, 0)
    analyzer = pipeline.Analyzer(info, H, MID, drops_per_ball=3)
    for t, frame in synth.render(cam, spec, tr, times):
        analyzer.process(t, frame)
    analyzer.finish()
    drops = [d for _, d in analyzer.result.entries]
    assert len(drops) == 3
    for d, t in zip(drops, truth.drops, strict=True):
        assert d.rebound_cm == pytest.approx(corrected(t), rel=0.005)


# --- command line ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, cm",
    [("72in", 182.88), ('72"', 182.88), ("6ft", 182.88), ("182.88cm", 182.88), ("1.5m", 150.0)],
)
def test_parse_length(text, cm):
    assert cli.parse_length(text) == pytest.approx(cm)


def test_parse_length_needs_a_unit():
    with pytest.raises(Exception, match="with a unit"):
        cli.parse_length("72")


def test_cli_json_and_debug(tmp_path, capsys):
    cam = synth.Camera.framing(H, **SMALL)
    spec = synth.SessionSpec(cor=(0.74,), drops_per_ball=2, distractor=False)
    path, truth = write(tmp_path, cam, spec)
    debug = tmp_path / "debug"
    code = cli.main(
        [str(path), "--drop-height", "72in", "--mid-mark", "36in", "--drops-per-ball", "2"]
        + ["--json", "--debug", str(debug)]
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["tool"].startswith("ball-bounce-tracker")
    assert out["calibration"]["method"] == "projective"
    assert [d["ball_no"] for d in out["drops"]] == [1, 1]
    for d, t in zip(out["drops"], truth.drops, strict=True):
        assert math.isclose(d["rebound_cm"], corrected(t), rel_tol=0.005)
    assert (debug / "trace.csv").read_text().startswith("t,found,cx")
    assert (debug / "drop-01.jpg").stat().st_size > 1000
    assert json.loads((debug / "result.json").read_text())["drops"][0]["seq"] == 0


def test_cli_table_and_errors(tmp_path, capsys):
    cam = synth.Camera.framing(H, **SMALL)
    spec = synth.SessionSpec(cor=(0.74,), drops_per_ball=1, distractor=False)
    path, _ = write(tmp_path, cam, spec)
    assert cli.main([str(path), "--drop-height", "72in", "--mid-mark", "36in", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Calibrated (projective)" in out and "ball 1:" in out
    with pytest.raises(SystemExit):
        cli.main([str(path), "--drop-height", "72in", "--mid-mark", "80in"])
    assert "between the floor" in capsys.readouterr().err
