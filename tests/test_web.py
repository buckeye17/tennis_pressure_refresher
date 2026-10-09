import csv
import io

import pytest

from canister_monitor import db
from canister_monitor.config import parse_config
from canister_monitor.decoders.base import Reading
from canister_monitor.web import create_app

NOW = 1_700_000_000.0
ATM = 101.325
MAC1 = "30:94:A8:11:11:11"  # configured, fresh readings
MAC2 = "30:94:A8:22:22:22"  # unassigned, stale
MAC3 = "30:94:A8:33:33:33"  # configured, never heard

CONFIG = parse_config(
    {
        "web": {"stale_after_minutes": 90},
        "sensors": [
            {"mac": MAC1, "canister": "Canister 1"},
            {"mac": MAC3, "canister": "Canister 3"},
        ],
    }
)


def reading(ts, mac=MAC1, gauge=200.0, temp=20.0, **kw) -> Reading:
    return Reading(ts, mac, ATM + gauge, temp, decoder="fbb0_ac00", **kw)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "web.db"
    conn = db.connect(path)
    db.upsert_sensor_seen(conn, MAC1, NOW - 3600, -60, "fbb0_ac00", "Canister 1")
    db.upsert_sensor_seen(conn, MAC1, NOW - 30, -58)
    for i, ts in enumerate(range(int(NOW - 3600), int(NOW - 59), 60)):  # 60 readings, 1/min
        db.insert_reading(conn, reading(float(ts), gauge=200.0 - i * 0.1), None)
    db.insert_reading(conn, reading(NOW - 60, gauge=150.0, temp=30.0, flags={"status": 0}), None)
    db.upsert_sensor_seen(conn, MAC2, NOW - 7200, -85, "fbb0_ac00")
    db.insert_reading(conn, reading(NOW - 7200, mac=MAC2, gauge=100.0, temp=None), None)
    db.insert_event(conn, NOW - 1800, "fill", "filled to 30 psi", MAC1)
    db.insert_event(conn, NOW - 600, "note", "added 3 balls")
    conn.close()
    return path


@pytest.fixture
def client(db_path):
    app = create_app(CONFIG, db_path=db_path, clock=lambda: NOW)
    app.testing = True
    return app.test_client()


def error_of(resp, status=400) -> str:
    assert resp.status_code == status, resp.get_data(as_text=True)
    return resp.get_json()["error"]


# --- index and health -------------------------------------------------------------


def test_index(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Canister Monitor" in resp.data


def test_healthz(client):
    body = client.get("/healthz").get_json()
    assert body["ok"] is True
    assert body["newest_reading_age_s"] == pytest.approx(60.0)
    assert body["db"].endswith("web.db")


def test_healthz_empty_db(tmp_path):
    app = create_app(CONFIG, db_path=tmp_path / "empty.db", clock=lambda: NOW)
    body = app.test_client().get("/healthz").get_json()
    assert body == {"ok": True, "db": str(tmp_path / "empty.db"), "newest_reading_age_s": None}


# --- /api/canisters --------------------------------------------------------------------


def test_canisters(client):
    body = client.get("/api/canisters").get_json()
    assert body["now"] == NOW
    assert body["settings"] == {
        "atmospheric_kpa": ATM,
        "reference_temp_c": 20.0,
        "stale_after_s": 5400.0,
        "default_units": "psi",
    }
    by_mac = {c["mac"]: c for c in body["canisters"]}
    # Configured sensors first (config order), then unassigned ones.
    assert [c["mac"] for c in body["canisters"]] == [MAC1, MAC3, MAC2]

    c1 = by_mac[MAC1]
    assert (c1["canister"], c1["assigned"], c1["stale"], c1["rssi"]) == (
        "Canister 1",
        True,
        False,
        -58,
    )
    r = c1["reading"]
    assert r["ts"] == NOW - 60 and r["age_s"] == 60
    assert r["gauge_kpa"] == pytest.approx(150.0)  # kPa, unit-neutral
    assert r["pressure_kpa_abs"] == pytest.approx(ATM + 150.0)
    expected_comp = (ATM + 150.0) * 293.15 / 303.15  # normalized from 30 C to 20 C
    assert r["compensated_kpa_abs"] == pytest.approx(expected_comp)
    assert r["compensated_gauge_kpa"] == pytest.approx(expected_comp - ATM)
    assert r["temp_c"] == 30.0 and r["flags"] == {"status": 0}
    assert c1["trend"] is None

    c2 = by_mac[MAC2]
    assert (c2["canister"], c2["assigned"], c2["stale"]) == (None, False, True)
    assert c2["reading"]["compensated_gauge_kpa"] is None  # no temperature

    c3 = by_mac[MAC3]
    assert (c3["canister"], c3["stale"], c3["reading"], c3["last_seen"]) == (
        "Canister 3",
        True,
        None,
        None,
    )


def test_config_name_overrides_stored_name(db_path):
    renamed = parse_config({"sensors": [{"mac": MAC1, "canister": "Big tube"}]})
    client = create_app(renamed, db_path=db_path, clock=lambda: NOW).test_client()
    names = {c["mac"]: c["canister"] for c in client.get("/api/canisters").get_json()["canisters"]}
    assert names[MAC1] == "Big tube"


# --- /api/readings ---------------------------------------------------------------------


def test_readings_all_series(client):
    body = client.get("/api/readings").get_json()
    series = {s["mac"]: s for s in body["series"]}
    assert set(series) == {MAC1, MAC2}
    s1 = series[MAC1]
    assert s1["canister"] == "Canister 1"
    assert len(s1["ts"]) == len(s1["gauge_kpa"]) == len(s1["temp_c"]) == 61
    assert s1["gauge_kpa"][0] == pytest.approx(200.0)
    assert s1["compensated_gauge_kpa"][0] == pytest.approx(200.0)  # at reference temp
    assert s1["ts"] == sorted(s1["ts"])
    assert series[MAC2]["compensated_gauge_kpa"] == [None]


def test_readings_filters(client):
    body = client.get(f"/api/readings?mac={MAC1.lower()}&from={NOW - 600}&to={NOW}").get_json()
    assert [s["mac"] for s in body["series"]] == [MAC1]
    assert all(NOW - 600 <= t <= NOW for t in body["series"][0]["ts"])
    assert len(body["series"][0]["ts"]) == 11  # 10 per-minute + the final reading
    assert body["from"] == NOW - 600 and body["to"] == NOW


def test_readings_downsampling(client):
    body = client.get(f"/api/readings?mac={MAC1}&max_points=10").get_json()
    s = body["series"][0]
    assert len(s["ts"]) == 10
    assert sum(s["count"]) == 61


@pytest.mark.parametrize(
    "query, message",
    [
        ("from=yesterday", "from must be a number"),
        ("to=nan", "to must be finite"),
        (f"from={NOW}&to={NOW - 1}", "from must not be after to"),
        ("max_points=0", "max_points must be between"),
        ("max_points=abc", "max_points must be an integer"),
        ("mac=not-a-mac", "mac must look like"),
    ],
)
def test_readings_bad_params(client, query, message):
    assert message in error_of(client.get(f"/api/readings?{query}"))


# --- events ---------------------------------------------------------------------------


def test_events_list_and_filter(client):
    events = client.get("/api/events").get_json()["events"]
    assert [(e["kind"], e["mac"]) for e in events] == [("fill", MAC1), ("note", None)]
    later = client.get(f"/api/events?from={NOW - 1000}").get_json()["events"]
    assert [e["kind"] for e in later] == ["note"]
    assert "from must be a number" in error_of(client.get("/api/events?from=x"))


def test_create_event_defaults_ts_to_now(client):
    resp = client.post("/api/events", json={"kind": "vent", "note": "let out 2 psi"})
    assert resp.status_code == 201
    event = resp.get_json()
    assert event["ts"] == NOW and event["mac"] is None and event["kind"] == "vent"
    assert event["id"] in [e["id"] for e in client.get("/api/events").get_json()["events"]]


def test_create_event_with_mac_and_ts(client):
    resp = client.post("/api/events", json={"kind": "balls_added", "mac": MAC1.lower(), "ts": 5})
    assert resp.status_code == 201
    assert resp.get_json() == {"id": 3, "ts": 5.0, "mac": MAC1, "kind": "balls_added", "note": None}


@pytest.mark.parametrize(
    "body, message",
    [
        ({}, "kind must be"),
        ({"kind": "Has Spaces"}, "kind must be"),
        ({"kind": "note", "ts": "noon"}, "ts must be a number"),
        ({"kind": "note", "ts": True}, "ts must be a number"),
        ({"kind": "note", "note": 5}, "note must be text"),
        ({"kind": "note", "note": "x" * 1001}, "note must be text"),
        ({"kind": "note", "mac": "nope"}, "mac must look like"),
        ({"kind": "note", "colour": "red"}, "unknown field"),
        ([1, 2], "JSON object"),
    ],
)
def test_create_event_validation(client, body, message):
    assert message in error_of(client.post("/api/events", json=body))


def test_create_event_requires_json(client):
    assert "JSON object" in error_of(client.post("/api/events", data="kind=note"))


def test_delete_event(client):
    first = client.get("/api/events").get_json()["events"][0]["id"]
    assert client.delete(f"/api/events/{first}").status_code == 204
    assert "not found" in error_of(client.delete(f"/api/events/{first}"), 404)
    assert len(client.get("/api/events").get_json()["events"]) == 1


# --- CSV export -------------------------------------------------------------------------


def read_csv(resp) -> list[dict]:
    assert resp.status_code == 200
    assert resp.mimetype == "text/csv"
    return list(csv.DictReader(io.StringIO(resp.get_data(as_text=True))))


def test_export_csv(client):
    resp = client.get("/api/export.csv")
    assert "attachment" in resp.headers["Content-Disposition"]
    assert resp.headers["Content-Disposition"].endswith('.csv"')
    header = resp.get_data(as_text=True).splitlines()[0]
    assert header == (
        "local_time,utc_epoch,canister,mac,gauge_psi,abs_psi,compensated_gauge_psi,"
        "temp_c,battery_pct,battery_v"
    )
    rows = read_csv(resp)
    assert len(rows) == 62  # every reading, no downsampling
    assert [r["mac"] for r in rows][0] == MAC2  # ordered by time
    first1 = next(r for r in rows if r["mac"] == MAC1)
    assert first1["canister"] == "Canister 1"
    assert float(first1["gauge_psi"]) == pytest.approx(200.0 / 6.894757, abs=0.01)
    assert float(first1["abs_psi"]) == pytest.approx((ATM + 200.0) / 6.894757, abs=0.01)
    assert first1["temp_c"] == "20.0"
    assert first1["local_time"][:4] == "2023"
    stale = next(r for r in rows if r["mac"] == MAC2)
    assert (
        stale["canister"] == "" and stale["compensated_gauge_psi"] == "" and stale["temp_c"] == ""
    )


def test_export_csv_filters_and_validation(client):
    rows = read_csv(client.get(f"/api/export.csv?mac={MAC2}"))
    assert [r["mac"] for r in rows] == [MAC2]
    rows = read_csv(client.get(f"/api/export.csv?from={NOW - 120}"))
    assert len(rows) == 3
    assert "mac must look like" in error_of(client.get("/api/export.csv?mac=zzz"))
