import base64
import csv
import io
import sqlite3
import statistics

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


# --- outliers -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "p, df, table",
    [(0.975, 10, 2.2281), (0.995, 6, 3.7074), (0.999, 5, 5.8934), (0.9995, 25, 3.7251)],
)
def test_t_quantile_matches_the_table(p, df, table):
    assert bounce.t_quantile(p, df) == pytest.approx(table, rel=0.004)


CLEAN = [140.2, 141.0, 139.1, 140.6, 138.8, 141.9, 140.0, 139.5, 140.9, 139.8, 141.3, 140.4]


def test_fit_normal_flags_nothing_in_a_clean_test():
    f = bounce.fit_normal(CLEAN)
    assert f.tested and f.outliers == ()
    assert f.mean == pytest.approx(statistics.fmean(CLEAN))
    assert f.sd == pytest.approx(statistics.stdev(CLEAN))
    assert max(abs(z) for z in f.z) < 2


def test_fit_normal_flags_a_bad_drop_and_fits_without_it():
    values = [*CLEAN[:5], 131.0, *CLEAN[5:]]
    f = bounce.fit_normal(values)
    assert f.outliers == (5,)
    assert f.mean == pytest.approx(statistics.fmean(CLEAN))  # the bad drop is left out
    assert f.z[5] < -5


def test_fit_normal_finds_two_outliers_that_would_hide_each_other():
    # Each alone inflates the spread enough that a one-at-a-time test misses the other.
    values = [*CLEAN, 132.0, 132.5]
    f = bounce.fit_normal(values)
    assert sorted(f.outliers) == [12, 13]


def test_fit_normal_with_few_or_equal_values():
    assert bounce.fit_normal([140.0]) is None
    few = bounce.fit_normal([140.0, 141.0, 120.0])
    assert not few.tested and few.outliers == ()  # too few to tell
    same = bounce.fit_normal([140.0] * 10)
    assert same.outliers == () and same.sd == 0


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


def test_migration_from_v4_keeps_video_drops_without_closeups(tmp_path):
    path = tmp_path / "v4.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    for version, sql in enumerate(db.MIGRATIONS[:4], start=1):
        c.executescript(sql)
        c.execute("INSERT INTO schema_version VALUES (?)", (version,))
    c.executescript(
        """
        INSERT INTO bounce_setups (id, name, drop_cm) VALUES (1, 'Hall', 200);
        INSERT INTO ball_batches (id, brand, model, opened_ts, n_balls, sample_size)
            VALUES (1, 'P', 'C', 1, 3, 3);
        INSERT INTO bounce_tests (id, ts, batch_id, setup_id, context, source)
            VALUES (1, 2, 1, 1, 'new', 'video');
        INSERT INTO bounce_video_analyses (id, test_id, save_key, filename, video_json,
            params_json) VALUES (1, 1, 'old-key-1', 'a.mp4', '{}', '{}');
        INSERT INTO bounce_video_drops (id, analysis_id, seq, ball_no, rebound_cm, kept,
            detail_json, image) VALUES (1, 1, 0, 1, 110.0, 1, '{}', X'FFD8FFD9');
        """
    )
    c.commit()
    c.close()
    conn = db.connect(path)
    assert db.schema_version(conn) == db.SCHEMA_VERSION
    [drop] = db.list_video_analyses(conn)[1].drops
    assert drop.has_image and not drop.has_closeup
    assert db.video_drop_image(conn, 1) == b"\xff\xd8\xff\xd9"
    assert db.video_drop_image(conn, 1, closeup=True) is None
    conn.close()


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
    assert rows[0]["outlier_z"] == ""


# Ten balls x 3 drops, one typed as 41.1 instead of 141.1.
TYPO_TEST = [(b, 140.0 + (b % 4) * 0.7 + i * 0.4) for b in range(1, 11) for i in range(3)]
TYPO_TEST[13] = (5, 41.1)


def test_a_test_flags_the_drop_that_does_not_fit(seeded):
    client, setup, batch = seeded
    drops = [{"ball_no": b, "rebound_cm": cm} for b, cm in TYPO_TEST]
    body = {"batch_id": batch["id"], "setup_id": setup["id"], "context": "new", "drops": drops}
    test = post(client, "/api/bounce/tests", body)
    flagged = [(d["ball_no"], d["rebound_cm"]) for d in test["drops"] if d["outlier_z"] is not None]
    assert flagged == [(5, 41.1)]
    fit = test["summary"]["drops_fit"]
    assert (fit["drops"], fit["outliers"], fit["tested"]) == (30, 1, True)
    assert fit["mean_cm"] > 140  # the typo is left out of the fit
    assert test["summary"]["medians"][4]["rebound_cm"] > 140  # the median shrugged it off

    rows = list(csv.DictReader(io.StringIO(client.get("/api/bounce.csv").get_data(as_text=True))))
    assert [r["rebound_cm"] for r in rows if r["outlier_z"]] == ["41.1"]
    assert float(next(r["outlier_z"] for r in rows if r["outlier_z"])) < -10


def test_outlier_check_for_the_test_form(seeded):
    client, _, _ = seeded
    values = [cm for _, cm in TYPO_TEST]
    res = post(client, "/api/bounce/outliers", {"rebounds_cm": values}, 200)
    assert [i for i, z in enumerate(res["outlier_z"]) if z is not None] == [13]
    few = post(client, "/api/bounce/outliers", {"rebounds_cm": [140.0, 41.1]}, 200)
    assert few["outlier_z"] == [None, None] and few["drops_fit"]["tested"] is False
    assert post(client, "/api/bounce/outliers", {"rebounds_cm": []}, 200)["drops_fit"] is None
    assert "must be a number" in error_of(
        client.post("/api/bounce/outliers", json={"rebounds_cm": ["x"]})
    )
    assert "list" in error_of(client.post("/api/bounce/outliers", json={"rebounds_cm": 5}))


# --- video tests (PLAN.md 6.3) ---------------------------------------------------------

JPEG = base64.b64encode(b"\xff\xd8\xff\xe0 a tiny picture \xff\xd9").decode()
CLOSEUP = base64.b64encode(b"\xff\xd8\xff\xe0 the ball at its apex \xff\xd9").decode()


def video_doc(setup, batch, key="session-0001", **test):
    """A reviewed test as the laptop app exports it: 3 drops, one rejected, one typed over."""
    drops = [
        {"seq": 0, "ball_no": 1, "rebound_cm": 110.2, "kept": True, "edited_cm": None},
        {"seq": 1, "ball_no": 1, "rebound_cm": 101.8, "kept": False, "edited_cm": None},
        {"seq": 2, "ball_no": 2, "rebound_cm": 99.0, "kept": True, "edited_cm": 109.5},
    ]
    for d in drops:
        d |= {"outlier_z": None, "detail": {"warnings": ["release 2.2 cm high"]}}
        d["image_jpeg"] = JPEG
        d["closeup_jpeg"] = CLOSEUP if d["kept"] else None
    return {
        "key": key,
        "tool": "ball-bounce-tracker 0.1",
        "test": {
            "ts": NOW - 600,
            "batch_id": batch["id"],
            "setup_id": setup["id"],
            "context": "before",
            "source": "video",
            "drops": [{"ball_no": 1, "rebound_cm": 110.2}, {"ball_no": 2, "rebound_cm": 109.5}],
            **test,
        },
        "set_up": {"batch_label": "Penn Champ"},
        "summary": {},
        "analysis": {
            "filename": "20261111-101500.mp4",
            "video": {"fps": 30.0, "calibration": {"method": "projective"}},
            "params": {"tool": "ball-bounce-tracker 0.1"},
        },
        "drops": drops,
    }


def test_migration_from_v3_marks_old_tests_manual(tmp_path):
    path = tmp_path / "v3.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    for version, sql in enumerate(db.MIGRATIONS[:3], start=1):
        c.executescript(sql)
        c.execute("INSERT INTO schema_version VALUES (?)", (version,))
    c.execute("INSERT INTO bounce_setups (name, drop_cm) VALUES ('s', 183)")
    c.execute(
        "INSERT INTO ball_batches (brand, model, opened_ts, n_balls, sample_size) "
        "VALUES ('Penn', 'Champ', 1, 4, 4)"
    )
    c.execute("INSERT INTO bounce_tests (ts, batch_id, setup_id, context) VALUES (1, 1, 1, 'new')")
    c.commit()
    c.close()
    conn = db.connect(path)
    assert [t.source for t in db.list_tests(conn)] == ["manual"]
    assert db.list_setups(conn)[0].mid_mark_cm is None
    conn.close()


def test_setup_mid_mark(seeded):
    client, setup, _ = seeded
    assert setup["mid_mark_cm"] is None
    body = {"name": "Hall", "drop_cm": 182.88, "mid_mark_cm": 91.44}
    assert post(client, "/api/bounce/setups", body)["mid_mark_cm"] == 91.44
    body = {"name": "x", "drop_cm": 182.88, "mid_mark_cm": 182.88}
    assert "between the floor" in error_of(client.post("/api/bounce/setups", json=body))

    path = f"/api/bounce/setups/{setup['id']}"
    assert client.patch(path, json={"mid_mark_cm": 100.0}).get_json()["mid_mark_cm"] == 100.0
    assert "between the floor" in error_of(client.patch(path, json={"mid_mark_cm": 300.0}))
    assert "unknown" in error_of(client.patch(path, json={"drop_cm": 100.0}))
    assert client.patch(path, json={"mid_mark_cm": None}).get_json()["mid_mark_cm"] is None
    assert client.patch("/api/bounce/setups/99", json={}).status_code == 404


def test_video_test_saved_with_its_pictures_and_saved_again_in_place(seeded):
    client, setup, batch = seeded
    doc = video_doc(setup, batch, temp_c=21.5)
    test = post(client, "/api/bounce/video-tests", doc)
    assert test["source"] == "video" and test["temp_c"] == 21.5 and test["ts"] == NOW - 600
    assert [(d["ball_no"], d["rebound_cm"]) for d in test["drops"]] == [(1, 110.2), (2, 109.5)]
    video = test["video"]
    assert video["filename"] == "20261111-101500.mp4" and video["calibration"] == "projective"
    assert [(d["seq"], d["kept"], d["edited_cm"]) for d in video["drops"]] == [
        (0, True, None),
        (1, False, None),
        (2, True, 109.5),
    ]
    assert all(d["image"] for d in video["drops"])
    assert [d["closeup"] for d in video["drops"]] == [True, False, True]
    assert video["drops"][0]["warnings"] == ["release 2.2 cm high"]
    first, rejected = (d["id"] for d in video["drops"][:2])
    pic = client.get(f"/api/bounce/video-drops/{first}.jpg")
    assert pic.mimetype == "image/jpeg" and b"tiny picture" in pic.data
    closeup = client.get(f"/api/bounce/video-drops/{first}/closeup.jpg")
    assert closeup.mimetype == "image/jpeg" and b"at its apex" in closeup.data
    assert client.get(f"/api/bounce/video-drops/{rejected}/closeup.jpg").status_code == 404
    assert client.get("/api/bounce/video-drops/999.jpg").status_code == 404

    # The same review saved again (edited since): the same test, replaced.
    doc["drops"][1]["kept"] = True
    doc["test"]["drops"].insert(1, {"ball_no": 1, "rebound_cm": 101.8})
    again = post(client, "/api/bounce/video-tests", doc, status=200)
    assert again["id"] == test["id"] and len(again["drops"]) == 3
    assert [d["kept"] for d in again["video"]["drops"]] == [True, True, True]
    [listed] = client.get("/api/bounce").get_json()["batches"][0]["tests"]
    assert listed["source"] == "video" and listed["video"]["drops"][1]["kept"]

    rows = list(csv.DictReader(io.StringIO(client.get("/api/bounce.csv").get_data(as_text=True))))
    assert {r["source"] for r in rows} == {"video"}

    # Edited on the dashboard, a video test keeps its analysis as the record.
    manual = {k: doc["test"][k] for k in ("batch_id", "setup_id", "context")}
    manual["drops"] = [{"ball_no": 1, "rebound_cm": 111.0}]
    edited = client.put(f"/api/bounce/tests/{test['id']}", json=manual).get_json()
    assert edited["source"] == "video" and len(edited["video"]["drops"]) == 3

    # Once the test is deleted, its key makes a new one (201, not 200).
    assert client.delete(f"/api/bounce/tests/{test['id']}").status_code == 204
    assert client.get(f"/api/bounce/video-drops/{video['drops'][0]['id']}.jpg").status_code == 404
    post(client, "/api/bounce/video-tests", doc, status=201)


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda d: d.pop("key"), "missing"),
        (lambda d: d.update(key="x y"), "key must be"),
        (lambda d: d["test"].update(source="manual"), "source must be video"),
        (lambda d: d["test"].update(batch_id=99), "batch 99 not found"),
        (lambda d: d["test"].pop("context"), "missing"),
        (lambda d: d["drops"][0].update(kept=False), "match the kept drops"),
        (lambda d: d["drops"][0].update(ball_no=61), "ball_no must be between 1 and 60"),
        (lambda d: d["drops"][2].update(edited_cm=500.0), "drop height"),
        (lambda d: d["drops"][0].update(rebound_cm=190.0), "no higher than the drop height"),
        (lambda d: d["drops"][0].update(image_jpeg="bm90IGEganBlZw=="), "base64 JPEG"),
        (lambda d: d["drops"][0].update(image_jpeg="%%%"), "base64 JPEG"),
        (lambda d: d["drops"][0].update(closeup_jpeg="bm90IGEganBlZw=="), "closeup_jpeg must be"),
        (lambda d: d["drops"][0].update(colour="red"), "unknown"),
        (lambda d: d["drops"][2].update(seq=0), "unique"),
        (lambda d: [x.update(kept=False) for x in d["drops"]], "no drops are kept"),
        (lambda d: d["analysis"].update(extra=1), "analysis must be"),
        (lambda d: d["analysis"].update(filename=""), "filename"),
    ],
)
def test_video_test_validation(seeded, change, message):
    client, setup, batch = seeded
    doc = video_doc(setup, batch)
    change(doc)
    assert message in error_of(client.post("/api/bounce/video-tests", json=doc))
    assert client.get("/api/bounce").get_json()["batches"][0]["tests"] == []


def test_a_rejected_drop_may_sit_on_a_ball_beyond_the_batch(seeded):
    client, setup, batch = seeded
    doc = video_doc(setup, batch)
    doc["drops"][1]["ball_no"] = 99  # a stray catch, numbered past the batch, then rejected
    assert post(client, "/api/bounce/video-tests", doc)["video"]["drops"][1]["ball_no"] == 99
