import json
import sqlite3
import threading

import pytest

from canister_monitor import db
from canister_monitor.decoders.base import Advert, Reading

MAC1 = "30:94:A8:11:11:11"
MAC2 = "30:94:A8:22:22:22"
FBB0_UUID = "0000fbb0-0000-1000-8000-00805f9b34fb"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    yield c
    c.close()


def reading(ts, mac=MAC1, kpa=200.0, temp=20.0, **kw) -> Reading:
    return Reading(ts=ts, mac=mac, pressure_kpa_abs=kpa, temp_c=temp, decoder="fbb0_ac00", **kw)


def advert(ts, mac=MAC1, payload=b"\x8f\x27\x44", rssi=-60) -> Advert:
    return Advert(
        ts=ts,
        mac=mac,
        name=None,
        rssi=rssi,
        manufacturer_data={0x00AC: payload},
        service_uuids=[FBB0_UUID],
        service_data={"0000fe00-0000-1000-8000-00805f9b34fb": b"\x01"},
    )


# --- setup and migrations ---------------------------------------------------


def test_connect_sets_pragmas_and_creates_dirs(tmp_path):
    c = db.connect(tmp_path / "nested" / "dir" / "x.db")
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert db.schema_version(c) == db.SCHEMA_VERSION
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sensors", "raw_adverts", "readings", "events", "schema_version"} <= tables


def test_migrations_are_idempotent(tmp_path):
    path = tmp_path / "x.db"
    c = db.connect(path)
    db.insert_event(c, 1.0, "note", "keep me")
    assert db.migrate(c) == db.SCHEMA_VERSION
    assert db.migrate(c) == db.SCHEMA_VERSION
    c.close()
    c = db.connect(path)  # reopening re-runs migrate()
    assert c.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == db.SCHEMA_VERSION
    assert [e.note for e in db.list_events(c)] == ["keep me"]


def test_newer_schema_is_refused(conn):
    with conn:
        conn.execute("INSERT INTO schema_version VALUES (?)", (db.SCHEMA_VERSION + 1,))
    with pytest.raises(RuntimeError, match="newer"):
        db.migrate(conn)


# --- writes -----------------------------------------------------------------


def test_insert_raw_advert_round_trip(conn):
    raw_id = db.insert_raw_advert(conn, advert(10.0))
    row = conn.execute("SELECT * FROM raw_adverts WHERE id = ?", (raw_id,)).fetchone()
    assert row["mac"] == MAC1 and row["rssi"] == -60 and row["ts"] == 10.0
    assert json.loads(row["mfr_json"]) == {"172": "8F2744"}
    assert json.loads(row["service_uuids_json"]) == [FBB0_UUID]
    assert json.loads(row["service_data_json"]) == {"0000fe00-0000-1000-8000-00805f9b34fb": "01"}


def test_insert_reading_creates_sensor_and_links_raw(conn):
    raw_id = db.insert_raw_advert(conn, advert(10.0))
    rid = db.insert_reading(conn, reading(10.0, flags={"status": 2}), raw_id)
    row = conn.execute("SELECT * FROM readings WHERE id = ?", (rid,)).fetchone()
    assert row["raw_advert_id"] == raw_id
    assert json.loads(row["flags_json"]) == {"status": 2}
    sensor = conn.execute("SELECT * FROM sensors WHERE mac = ?", (MAC1,)).fetchone()
    assert sensor["first_seen"] == 10.0 and sensor["decoder"] == "fbb0_ac00"


def test_reading_foreign_key_to_raw_is_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_reading(conn, reading(10.0), raw_advert_id=999)


def test_upsert_sensor_seen_tracks_first_last_and_rssi(conn):
    db.upsert_sensor_seen(conn, MAC1, 100.0, -70, decoder="fbb0_ac00", canister="Canister 1")
    db.upsert_sensors_seen(
        conn,
        [
            db.SensorSeen(MAC1, 160.0, -55),
            db.SensorSeen(MAC1, 130.0, -90),  # out of order: must not win
            db.SensorSeen(MAC2, 150.0, -80),
        ],
    )
    rows = {r["mac"]: r for r in conn.execute("SELECT * FROM sensors")}
    s1 = rows[MAC1]
    assert (s1["first_seen"], s1["last_seen"], s1["last_rssi"]) == (100.0, 160.0, -55)
    assert s1["canister"] == "Canister 1"  # not cleared by updates without a canister
    assert s1["decoder"] == "fbb0_ac00"
    assert rows[MAC2]["first_seen"] == 150.0


# --- latest readings --------------------------------------------------------


def test_latest_readings(conn):
    db.upsert_sensor_seen(conn, MAC2, 50.0, -80)  # seen but never decoded
    db.insert_reading(conn, reading(10.0, kpa=300.0), None)
    db.insert_reading(conn, reading(30.0, kpa=280.0, flags={"status": 0}), None)
    db.insert_reading(conn, reading(20.0, kpa=290.0), None)
    latest = {s.mac: s for s in db.latest_readings(conn)}
    assert latest[MAC1].reading_ts == 30.0
    assert latest[MAC1].pressure_kpa_abs == 280.0
    assert latest[MAC1].flags == {"status": 0}
    assert latest[MAC2].reading_ts is None and latest[MAC2].flags == {}
    assert db.newest_reading_ts(conn) == 30.0


# --- readings_between and downsampling ----------------------------------------


def test_readings_between_filters_without_downsampling(conn):
    for t in range(10):
        db.insert_reading(conn, reading(float(t), kpa=200.0 + t), None)
        db.insert_reading(conn, reading(float(t), mac=MAC2, kpa=100.0 + t), None)
    pts = db.readings_between(conn, mac=MAC1, start=2.0, end=5.0)
    assert [p.ts for p in pts] == [2.0, 3.0, 4.0, 5.0]
    assert all(p.count == 1 and p.mac == MAC1 for p in pts)
    both = db.readings_between(conn)
    assert [p.mac for p in both] == [MAC1] * 10 + [MAC2] * 10


def test_readings_between_downsamples_per_sensor(conn):
    with conn:  # bulk insert directly for speed
        conn.execute(
            "INSERT INTO sensors (mac, first_seen, last_seen) VALUES (?, 0, 0), (?, 0, 0)",
            (MAC1, MAC2),
        )
        conn.executemany(
            "INSERT INTO readings (ts, mac, pressure_kpa_abs, temp_c, decoder) "
            "VALUES (?, ?, ?, ?, 'x')",
            [(float(t), MAC1, 100.0 + t, None if t % 2 else 20.0) for t in range(10_000)]
            + [(float(t), MAC2, 5.0, 10.0) for t in range(0, 10_000, 1000)],
        )
    pts = db.readings_between(conn, max_points=100)
    p1 = [p for p in pts if p.mac == MAC1]
    p2 = [p for p in pts if p.mac == MAC2]
    assert len(p1) == 100
    assert sum(p.count for p in p1) == 10_000
    assert len(p2) == 10  # under the limit: untouched
    # Linear data: each bucket average equals pressure at the bucket's average time.
    for p in p1:
        assert p.pressure_kpa_abs == pytest.approx(100.0 + p.ts)
    assert [p.ts for p in p1] == sorted(p.ts for p in p1)
    assert all(p.temp_c == 20.0 for p in p1)  # NULL temps ignored in the average


def test_readings_between_downsamples_inside_range(conn):
    for t in range(100):
        db.insert_reading(conn, reading(float(t), kpa=float(t)), None)
    pts = db.readings_between(conn, start=50.0, end=99.0, max_points=5)
    assert len(pts) == 5
    assert sum(p.count for p in pts) == 50
    assert pts[0].ts >= 50.0


def test_readings_between_rejects_bad_max_points(conn):
    with pytest.raises(ValueError):
        db.readings_between(conn, max_points=0)


# --- events -----------------------------------------------------------------


def test_events_crud(conn):
    a = db.insert_event(conn, 100.0, "fill", "filled to 30 psi", mac=MAC1)
    b = db.insert_event(conn, 200.0, "note", "added 3 balls")
    assert [e.id for e in db.list_events(conn)] == [a, b]
    assert [e.kind for e in db.list_events(conn, start=150.0)] == ["note"]
    assert [e.kind for e in db.list_events(conn, end=150.0)] == ["fill"]
    assert db.list_events(conn)[0].mac == MAC1
    assert db.list_events(conn)[1].mac is None
    assert db.delete_event(conn, a) is True
    assert db.delete_event(conn, a) is False
    assert [e.id for e in db.list_events(conn)] == [b]


# --- concurrency (WAL) ------------------------------------------------------


def test_reader_works_while_writer_writes(tmp_path):
    path = tmp_path / "wal.db"
    db.connect(path).close()
    n_writes = 300
    errors: list[BaseException] = []
    counts: list[int] = []
    done = threading.Event()

    def writer():
        try:
            c = db.connect(path)
            for t in range(n_writes):
                db.insert_reading(c, reading(float(t)), None)
            c.close()
        except BaseException as e:
            errors.append(e)
        finally:
            done.set()

    def reader():
        try:
            c = db.connect(path)
            while not done.is_set():
                counts.append(c.execute("SELECT COUNT(*) FROM readings").fetchone()[0])
                db.latest_readings(c)
                db.readings_between(c, max_points=50)
            counts.append(c.execute("SELECT COUNT(*) FROM readings").fetchone()[0])
            c.close()
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors
    assert counts == sorted(counts)  # reader never sees rows disappear
    assert counts[-1] == n_writes
