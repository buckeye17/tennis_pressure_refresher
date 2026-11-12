"""tools/import_review.py: a review downloaded without the Pi, sent to the Pi's real
endpoint over HTTP."""

import base64
import importlib.util
import json
import threading
from pathlib import Path

import pytest
from werkzeug.serving import make_server

from canister_monitor.config import parse_config
from canister_monitor.web import create_app

spec = importlib.util.spec_from_file_location(
    "import_review", Path(__file__).resolve().parents[1] / "tools" / "import_review.py"
)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)

DROP = 200.66
JPEG = base64.b64encode(b"\xff\xd8\xff\xe0 picture \xff\xd9").decode()


def offline_review() -> dict:
    """What Download JSON gave for a session set up with the Pi away: no batch, setup,
    stage or key. 2 balls x 2 drops, the last one rejected."""
    heights = [110.2, 111.0, 112.4, 101.8]
    drops = [
        {
            "seq": i,
            "ball_no": i // 2 + 1,
            "rebound_cm": h,
            "kept": i != 3,
            "edited_cm": None,
            "outlier_z": None,
            "detail": {"warnings": []},
            "image_jpeg": JPEG,
        }
        for i, h in enumerate(heights)
    ]
    return {
        "tool": "ball-bounce-tracker 0.1",
        "test": {
            "ts": 1_794_400_000.0,
            "batch_id": None,
            "setup_id": None,
            "context": None,
            "source": "video",
            "drops": [{"ball_no": d["ball_no"], "rebound_cm": d["rebound_cm"]} for d in drops[:3]],
        },
        "set_up": {"drop_height_cm": DROP, "mid_mark_cm": 100.33, "batch_id": None},
        "summary": {"balls": 2, "mean_cm": 111.3},
        "analysis": {"filename": "20261111-195119.mp4", "video": {}, "params": {}},
        "drops": drops,
    }


@pytest.fixture
def pi(tmp_path):
    app = create_app(parse_config({}), db_path=tmp_path / "pi.db", clock=lambda: 1.8e9)
    c = app.test_client()
    c.post("/api/bounce/setups", json={"name": "Hall, 79 in", "drop_cm": DROP})
    c.post("/api/bounce/setups", json={"name": "Garage, 6 ft", "drop_cm": 182.88})
    c.post("/api/bounce/batches", json={"brand": "Penn", "model": "Champ", "n_balls": 9})
    c.post("/api/bounce/batches", json={"brand": "Wilson", "model": "US Open", "n_balls": 1})
    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.port}", c
    server.shutdown()


def test_parse_temp():
    assert tool.parse_temp("68F") == pytest.approx(20.0)
    assert tool.parse_temp("20.5 c") == 20.5
    with pytest.raises(Exception, match="unit"):
        tool.parse_temp("20")


def test_import_then_import_again_updates_the_same_test(pi, tmp_path, capsys):
    url, client = pi
    review = tmp_path / "bounce-test-20261111-1951.json"
    review.write_text(json.dumps(offline_review()))
    args = [str(review), "--pi", url, "--batch", "1", "--setup", "1", "--context", "after"]

    assert tool.main([*args, "--dry-run"]) == 0
    assert "Dry run" in capsys.readouterr().out
    assert client.get("/api/bounce").get_json()["batches"][1]["tests"] == []

    assert tool.main([*args, "--temp", "68F", "--note", "first webcam session"]) == 0
    assert "Saved on the Pi as test #1" in capsys.readouterr().out
    [test] = client.get("/api/bounce").get_json()["batches"][1]["tests"]  # newest batch first
    assert (test["source"], test["context"], test["ts"]) == ("video", "after", 1_794_400_000.0)
    assert test["temp_c"] == pytest.approx(20.0) and test["note"] == "first webcam session"
    assert [d["rebound_cm"] for d in test["drops"]] == [110.2, 111.0, 112.4]
    assert [d["kept"] for d in test["video"]["drops"]] == [True, True, True, False]

    assert tool.main([*args, "--context", "before"]) == 0  # fixing the stage
    assert "Updated on the Pi as test #1" in capsys.readouterr().out
    [test] = client.get("/api/bounce").get_json()["batches"][1]["tests"]
    assert test["context"] == "before"


@pytest.mark.parametrize(
    "extra, message",
    [
        (["--batch", "9", "--setup", "1", "--context", "after"], "no batch 9"),
        (["--batch", "1", "--setup", "2", "--context", "after"], "drops from 182.88 cm"),
        (["--batch", "2", "--setup", "1", "--context", "after"], "batch 2 has 1 balls"),
        (["--batch", "1", "--setup", "1", "--context", "later"], "--context must be one of"),
    ],
)
def test_import_refuses_what_the_pi_would(pi, tmp_path, capsys, extra, message):
    url, client = pi
    review = tmp_path / "r.json"
    review.write_text(json.dumps(offline_review()))
    assert tool.main([str(review), "--pi", url, *extra]) == 1
    assert message in capsys.readouterr().err
    assert all(b["tests"] == [] for b in client.get("/api/bounce").get_json()["batches"])


def test_list_and_an_unreachable_pi(pi, capsys):
    url, _ = pi
    assert tool.main(["--list", "--pi", url]) == 0
    out = capsys.readouterr().out
    assert "Hall, 79 in: drops from 200.66 cm" in out and "Penn Champ" in out
    assert tool.main(["--list", "--pi", "http://127.0.0.1:9"]) == 1
    assert "can't reach" in capsys.readouterr().err
