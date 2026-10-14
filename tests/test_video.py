"""Bounce-video measurement (PLAN.md 6.3). Needs the laptop-only ``video`` extra;
skipped where OpenCV isn't installed (e.g. on the Pi)."""

import json
import math
import statistics

import pytest

cv2 = pytest.importorskip("cv2")

from canister_monitor.video import cli, measure, pipeline, synth  # noqa: E402
from canister_monitor.video.measure import Measurer, Observation, fit_calibration  # noqa: E402

H = 182.88  # 72 in
MID = 91.44  # 36 in
SMALL = {"width": 360, "height": 640}  # renders faster; still ~6 px ball radius


def corrected(truth_drop: dict) -> float:
    """The rebound the tool should report: scaled to a release exactly at the mark."""
    return truth_drop["rebound_cm"] * H / truth_drop["release_cm"]


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


def observations(cam: synth.Camera, spec: synth.SessionSpec, fps: float = 30.0):
    """Perfect per-frame observations of a synthetic session, plus the truth."""
    tr, truth = synth.build(spec, cam)
    out = []
    for i in range(int(tr.end * fps)):
        t = i / fps
        x, y, hand = tr.at(t)
        u, v, ppm = cam.project(x, y + synth.BALL_RADIUS_M)
        r = synth.BALL_RADIUS_M * ppm
        visible = 0 <= u <= cam.width
        out.append((t, Observation(t, u, v, 2 * r, 2 * r, v + r) if visible else None))
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
    assert out["tool"].startswith("canister-video")
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
