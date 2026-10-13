import csv
import io
import sqlite3

import pytest

from canister_monitor import bounce, db
from canister_monitor.config import parse_config
from canister_monitor.web import create_app

NOW = 1_700_000_000.0
MAC1 = "30:94:A8:11:11:11"
CONFIG = parse_config({"sensors": [{"mac": MAC1, "canister": "Canister 1"}]})


# --- summaries ----------------------------------------------------------------------


def test_summarize_takes_median_per_ball_then_mean():
    drops = [(2, 140.0), (1, 130.0), (1, 150.0), (1, 131.0), (2, 142.0), (2, 141.0)]
    s = bounce.summarize(drops)
    assert s.medians == {1: 131.0, 2: 141.0}  # the 150 is shrugged off
    assert s.balls == 2
    assert s.mean_cm == pytest.approx(136.0)
    assert s.sd_cm == pytest.approx(7.0711, abs=1e-4)


def test_summarize_edge_cases():
    assert bounce.summarize([]) == bounce.TestSummary(0, None, None, {})
    one = bounce.summarize([(3, 120.0), (3, 122.0)])
    assert (one.balls, one.mean_cm, one.sd_cm) == (1, 121.0, None)


# --- storage --------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "bounce.db")
    yield c
    c.close()


def new_batch(conn, **kw):
    fields = {"brand": "Penn", "model": "Championship", "opened_ts": NOW, "n_balls": 24}
    fields.update(sample_size=10, **kw)
    return db.insert_batch(conn, **fields)


def test_migration_from_v2_keeps_events(tmp_path):
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    for version, sql in enumerate(db.MIGRATIONS[:2], start=1):
        c.executescript(sql)
        c.execute("INSERT INTO schema_version VALUES (?)", (version,))
    c.execute("INSERT INTO events (ts, mac, kind, note) VALUES (1, NULL, 'note', 'old')")
    c.commit()
    c.close()
    conn = db.connect(path)
    assert db.schema_version(conn) == db.SCHEMA_VERSION
    assert db.list_events(conn)[0] == db.Event(1, 1.0, None, "note", "old", None)
    conn.close()


def test_tests_round_trip_and_replace(conn):
    setup = db.insert_setup(conn, "garage, 100 in", 254.0, True)
    batch = new_batch(conn)
    drops = [db.BounceDrop(1, 140.0), db.BounceDrop(1, 141.0), db.BounceDrop(2, 139.0)]
    test_id = db.insert_test(conn, NOW, batch, setup, "new", 20.0, None, drops)
    [t] = db.list_tests(conn, batch)
    assert t.drops == tuple(drops) and t.context == "new" and t.temp_c == 20.0
    assert db.highest_ball_tested(conn, batch) == 2

    assert db.replace_test(conn, test_id, NOW, batch, setup, "after", None, "x", drops[:1])
    [t] = db.list_tests(conn)
    assert (t.context, t.note, t.drops) == ("after", "x", (drops[0],))
    assert not db.replace_test(conn, 999, NOW, batch, setup, "after", None, None, drops)


def test_deleting_a_batch_removes_its_tests_and_unlinks_events(conn):
    setup = db.insert_setup(conn, "s", 183.0, False)
    batch = new_batch(conn)
    db.insert_test(conn, NOW, batch, setup, "new", None, None, [db.BounceDrop(1, 100.0)])
    event = db.insert_event(conn, NOW, "balls_added", mac=MAC1, batch_id=batch)
    assert db.delete_batch(conn, batch)
    assert db.list_tests(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM bounce_drops").fetchone()[0] == 0
    assert db.list_events(conn)[0].id == event
    assert db.list_events(conn)[0].batch_id is None


def test_setup_in_use_cannot_be_deleted(conn):
    setup = db.insert_setup(conn, "s", 183.0, False)
    batch = new_batch(conn)
    assert [s.used for s in db.list_setups(conn)] == [False]
    db.insert_test(conn, NOW, batch, setup, "new", None, None, [db.BounceDrop(1, 100.0)])
    assert [s.used for s in db.list_setups(conn)] == [True]
    with pytest.raises(sqlite3.IntegrityError):
        db.delete_setup(conn, setup)
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_setup(conn, "S", 100.0, False)  # names are case-insensitive


def test_reference_upsert_is_case_insensitive(conn):
    setup = db.insert_setup(conn, "s", 183.0, False)
    a = db.upsert_reference(conn, "Penn", "Championship", setup, 120.0)
    b = db.upsert_reference(conn, "penn", "CHAMPIONSHIP", setup, 125.0)
    assert a == b
    assert [r.rebound_cm for r in db.list_references(conn)] == [125.0]


def test_batch_locations_follow_the_latest_event(conn):
    a, b = new_batch(conn), new_batch(conn)
    db.insert_event(conn, NOW, "balls_added", mac=MAC1, batch_id=a)
    db.insert_event(conn, NOW + 10, "balls_added", mac=MAC1, batch_id=b)
    db.insert_event(conn, NOW + 20, "balls_removed", mac=MAC1, batch_id=a)
    assert db.batch_locations(conn) == {a: ("balls_removed", MAC1), b: ("balls_added", MAC1)}


# --- API -------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    app = create_app(CONFIG, db_path=tmp_path / "web.db", clock=lambda: NOW)
    app.testing = True
    return app.test_client()


def error_of(resp, status=400) -> str:
    assert resp.status_code == status, resp.get_data(as_text=True)
    return resp.get_json()["error"]


def post(client, path, body, status=201):
    resp = client.post(path, json=body)
    assert resp.status_code == status, resp.get_data(as_text=True)
    return resp.get_json()


@pytest.fixture
def seeded(client):
    setup = post(client, "/api/bounce/setups", {"name": "Garage, 6 ft", "drop_cm": 182.88})
    batch = post(client, "/api/bounce/batches", {"brand": "Penn", "model": "Champ", "n_balls": 60})
    return client, setup, batch


def test_empty_bounce_state(client):
    body = client.get("/api/bounce").get_json()
    assert body["setups"] == body["batches"] == body["references"] == body["conversions"] == []
    s = body["settings"]
    assert s["display_units"] == "in" and s["like_new_tolerance_pct"] == 5.0
    assert s["itf_rebound_cm"] == pytest.approx([134.62, 147.32])
    assert "after_play" in s["contexts"]


def test_batch_defaults(client):
    big = post(client, "/api/bounce/batches", {"brand": "Penn", "model": "Champ", "n_balls": 60})
    small = post(client, "/api/bounce/batches", {"brand": "Penn", "model": "Champ", "n_balls": 8})
    assert (big["sample_size"], small["sample_size"]) == (10, 8)
    assert big["opened_ts"] == NOW
    assert big["retired_ts"] is None


@pytest.mark.parametrize(
    "body, message",
    [
        ({"brand": "", "model": "x", "n_balls": 4}, "brand must not be empty"),
        ({"brand": "a", "model": "x"}, "missing field(s): n_balls"),
        ({"brand": "a", "model": "x", "n_balls": 0}, "n_balls must be between"),
        ({"brand": "a", "model": "x", "n_balls": 4, "sample_size": 5}, "can't be more than"),
        ({"brand": "a", "model": "x", "n_balls": 4.5}, "whole number"),
        ({"brand": "a", "model": "x", "n_balls": 4, "colour": 1}, "unknown field"),
    ],
)
def test_batch_validation(client, body, message):
    assert message in error_of(client.post("/api/bounce/batches", json=body))


def test_patch_batch(seeded):
    client, _, batch = seeded
    resp = client.patch(
        f"/api/bounce/batches/{batch['id']}",
        json={"retired_ts": NOW + 5, "reference_override_cm": 135.0, "note": " box A "},
    )
    assert resp.status_code == 200
    assert resp.get_json()["retired_ts"] == NOW + 5
    assert resp.get_json()["note"] == "box A"
    cleared = client.patch(f"/api/bounce/batches/{batch['id']}", json={"retired_ts": None})
    assert cleared.get_json()["retired_ts"] is None
    assert "not found" in error_of(client.patch("/api/bounce/batches/99", json={}), 404)


def test_setup_validation_and_conflicts(seeded):
    client, setup, _ = seeded
    assert "already exists" in error_of(
        client.post("/api/bounce/setups", json={"name": "garage, 6 FT", "drop_cm": 100}), 409
    )
    assert "ITF setup drops from 254" in error_of(
        client.post("/api/bounce/setups", json={"name": "x", "drop_cm": 183, "is_itf": True})
    )
    assert client.delete(f"/api/bounce/setups/{setup['id']}").status_code == 204


def test_bounce_test_create_edit_delete(seeded):
    client, setup, batch = seeded
    body = {
        "batch_id": batch["id"],
        "setup_id": setup["id"],
        "context": "new",
        "temp_c": 19.5,
        "drops": [
            {"ball_no": 1, "rebound_cm": 120.0},
            {"ball_no": 1, "rebound_cm": 121.0},
            {"ball_no": 1, "rebound_cm": 140.0},
            {"ball_no": 2, "rebound_cm": 118.0},
        ],
    }
    test = post(client, "/api/bounce/tests", body)
    assert test["ts"] == NOW
    assert test["summary"]["balls"] == 2
    assert test["summary"]["mean_cm"] == pytest.approx((121.0 + 118.0) / 2)
    assert test["summary"]["medians"] == [
        {"ball_no": 1, "rebound_cm": 121.0},
        {"ball_no": 2, "rebound_cm": 118.0},
    ]

    body["context"] = "after"
    body["drops"] = body["drops"][:1]
    edited = client.put(f"/api/bounce/tests/{test['id']}", json=body).get_json()
    assert edited["context"] == "after" and edited["summary"]["balls"] == 1

    state = client.get("/api/bounce").get_json()
    assert state["setups"][0]["used"] is True
    assert [t["id"] for t in state["batches"][0]["tests"]] == [test["id"]]
    assert "has tests" in error_of(client.delete(f"/api/bounce/setups/{setup['id']}"), 409)
    shrink = {"n_balls": 1, "sample_size": 1}  # only ball 1 tested, so this is allowed
    assert client.patch(f"/api/bounce/batches/{batch['id']}", json=shrink).status_code == 200

    assert client.delete(f"/api/bounce/tests/{test['id']}").status_code == 204
    assert "not found" in error_of(client.delete(f"/api/bounce/tests/{test['id']}"), 404)


def test_cannot_shrink_batch_below_tested_ball(seeded):
    client, setup, batch = seeded
    post(
        client,
        "/api/bounce/tests",
        {
            "batch_id": batch["id"],
            "setup_id": setup["id"],
            "context": "new",
            "drops": [{"ball_no": 7, "rebound_cm": 120.0}],
        },
    )
    resp = client.patch(f"/api/bounce/batches/{batch['id']}", json={"n_balls": 5, "sample_size": 5})
    assert "ball 7 has been tested" in error_of(resp)


@pytest.mark.parametrize(
    "change, message",
    [
        ({"context": "later"}, "context must be one of"),
        ({"drops": []}, "drops must be a list"),
        ({"drops": [{"ball_no": 1}]}, "must be {ball_no, rebound_cm}"),
        ({"drops": [{"ball_no": 61, "rebound_cm": 100.0}]}, "ball_no must be between 1 and 60"),
        ({"drops": [{"ball_no": 1, "rebound_cm": 200.0}]}, "wrong units?"),  # above 182.88 cm
        ({"drops": [{"ball_no": 1, "rebound_cm": 0}]}, "must be above 0"),
        ({"temp_c": 99}, "temp_c must be between"),
        ({"setup_id": 99}, "setup 99 not found"),
        ({"batch_id": 99}, "batch 99 not found"),
    ],
)
def test_bounce_test_validation(seeded, change, message):
    client, setup, batch = seeded
    body = {
        "batch_id": batch["id"],
        "setup_id": setup["id"],
        "context": "new",
        "drops": [{"ball_no": 1, "rebound_cm": 120.0}],
        **change,
    }
    assert message in error_of(client.post("/api/bounce/tests", json=body))


def test_references_and_conversions(seeded):
    client, setup, _ = seeded
    ref = {"brand": "Penn", "model": "Champ", "setup_id": setup["id"], "rebound_cm": 120.0}
    first = post(client, "/api/bounce/references", ref)
    again = post(client, "/api/bounce/references", {**ref, "rebound_cm": 122.0})
    assert first["id"] == again["id"] and again["rebound_cm"] == 122.0
    assert client.delete(f"/api/bounce/references/{first['id']}").status_code == 204

    conv = {"setup_id": setup["id"], "rebound_cm": 100.0, "itf_rebound_cm": 140.0}
    made = post(client, "/api/bounce/conversions", conv)
    assert made["ts"] == NOW and made["itf_rebound_cm"] == 140.0
    assert "wrong units?" in error_of(
        client.post("/api/bounce/conversions", json={**conv, "itf_rebound_cm": 300.0})
    )
    itf = post(client, "/api/bounce/setups", {"name": "ITF", "drop_cm": 254, "is_itf": True})
    assert "already is the ITF method" in error_of(
        client.post("/api/bounce/conversions", json={**conv, "setup_id": itf["id"]})
    )
    assert client.delete(f"/api/bounce/conversions/{made['id']}").status_code == 204


def test_events_link_batches_and_show_location(seeded):
    client, _, batch = seeded
    post(client, "/api/events", {"kind": "balls_added", "mac": MAC1, "batch_id": batch["id"]})
    state = client.get("/api/bounce").get_json()
    assert state["batches"][0]["location"] == {"in_canister": True, "mac": MAC1}
    assert "batch 99 not found" in error_of(
        client.post("/api/events", json={"kind": "balls_added", "batch_id": 99})
    )
    assert "whole number" in error_of(
        client.post("/api/events", json={"kind": "balls_added", "batch_id": "1"})
    )


def test_bounce_csv(seeded):
    client, setup, batch = seeded
    post(
        client,
        "/api/bounce/tests",
        {
            "batch_id": batch["id"],
            "setup_id": setup["id"],
            "context": "before",
            "drops": [{"ball_no": 1, "rebound_cm": 127.0}, {"ball_no": 2, "rebound_cm": 120.0}],
        },
    )
    resp = client.get("/api/bounce.csv")
    assert resp.mimetype == "text/csv"
    rows = list(csv.DictReader(io.StringIO(resp.get_data(as_text=True))))
    assert [r["ball_no"] for r in rows] == ["1", "2"]
    assert rows[0]["rebound_in"] == "50.00"
    assert rows[0]["setup"] == "Garage, 6 ft" and rows[0]["context"] == "before"
