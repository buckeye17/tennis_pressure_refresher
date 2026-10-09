import pytest

from canister_monitor import db
from canister_monitor.decoders.base import Advert, Reading
from canister_monitor.write_policy import WritePolicy

MAC = "30:94:A8:11:11:11"
OTHER = "30:94:A8:22:22:22"
RAW_HB = 600.0  # 10 min
READING_HB = 60.0  # 1 min


def adv(ts, payload=b"\x8f\x27\x44", mac=MAC, rssi=-60) -> Advert:
    return Advert(ts, mac, None, rssi, {0x00AC: payload}, ["fbb0"], {})


def rd(ts, kpa=224.0, temp=13.0, mac=MAC, flags=None) -> Reading:
    return Reading(ts, mac, kpa, temp, flags=flags or {}, decoder="fbb0_ac00")


@pytest.fixture
def policy():
    return WritePolicy(raw_heartbeat_s=RAW_HB, reading_heartbeat_s=READING_HB)


# --- raw adverts -------------------------------------------------------------


def test_first_raw_advert_is_stored(policy):
    assert policy.raw_due(adv(0.0))


def test_identical_raw_waits_for_heartbeat(policy):
    policy.raw_stored(adv(0.0), raw_id=1)
    assert not policy.raw_due(adv(1.0))
    assert not policy.raw_due(adv(RAW_HB - 0.1))
    assert policy.raw_due(adv(RAW_HB))


def test_rssi_change_alone_does_not_count(policy):
    policy.raw_stored(adv(0.0, rssi=-60), raw_id=1)
    assert not policy.raw_due(adv(5.0, rssi=-90))


def test_changed_payload_is_stored_immediately(policy):
    policy.raw_stored(adv(0.0), raw_id=1)
    assert policy.raw_due(adv(1.0, payload=b"\x8f\x26\x44"))


def test_raw_state_is_per_mac(policy):
    policy.raw_stored(adv(0.0), raw_id=1)
    assert policy.raw_due(adv(1.0, mac=OTHER))


def test_clock_going_backwards_stores(policy):
    policy.raw_stored(adv(1000.0), raw_id=1)
    assert policy.raw_due(adv(10.0))


def test_heartbeat_restarts_from_last_store(policy):
    policy.raw_stored(adv(0.0), raw_id=1)
    policy.raw_stored(adv(RAW_HB), raw_id=2)
    assert not policy.raw_due(adv(RAW_HB + 10))
    assert policy.last_raw_id(MAC) == 2


# --- readings ----------------------------------------------------------------


def test_identical_reading_waits_for_heartbeat(policy):
    policy.reading_stored(rd(0.0))
    assert not policy.reading_due(rd(30.0))
    assert policy.reading_due(rd(READING_HB))


def test_changed_values_or_flags_store_immediately(policy):
    policy.reading_stored(rd(0.0))
    assert policy.reading_due(rd(1.0, kpa=221.0))
    assert policy.reading_due(rd(1.0, temp=14.0))
    assert policy.reading_due(rd(1.0, flags={"status": 1}))


# --- policy driving the database (fake clock = advert timestamps) -----------------


def store(conn, policy, a: Advert, r: Reading | None) -> None:
    """What the collector does for each decoded advert."""
    if policy.raw_due(a):
        policy.raw_stored(a, db.insert_raw_advert(conn, a))
    if r is not None and policy.reading_due(r):
        db.insert_reading(conn, r, policy.last_raw_id(a.mac))
        policy.reading_stored(r)


def test_policy_with_database(tmp_path, policy):
    conn = db.connect(tmp_path / "p.db")
    p1, p2 = b"\x8f\x27\x44", b"\x8f\x26\x44"
    script = [
        (0.0, p1, 224.0),  # first: raw + reading
        (9.0, p1, 224.0),  # duplicate burst: nothing
        (30.0, p1, 224.0),  # nothing (inside both heartbeats)
        (60.0, p1, 224.0),  # reading heartbeat -> reading linked to raw #1
        (90.0, p2, 221.0),  # change -> raw #2 + reading
        (100.0, p2, 221.0),  # nothing
        (690.0, p2, 221.0),  # raw heartbeat (600 s after 90) + reading heartbeat
    ]
    for ts, payload, kpa in script:
        store(conn, policy, adv(ts, payload), rd(ts, kpa=kpa))

    raws = conn.execute("SELECT id, ts FROM raw_adverts ORDER BY id").fetchall()
    assert [tuple(r) for r in raws] == [(1, 0.0), (2, 90.0), (3, 690.0)]
    readings = conn.execute(
        "SELECT ts, pressure_kpa_abs, raw_advert_id FROM readings ORDER BY id"
    ).fetchall()
    assert [tuple(r) for r in readings] == [
        (0.0, 224.0, 1),
        (60.0, 224.0, 1),
        (90.0, 221.0, 2),
        (690.0, 221.0, 3),
    ]
    conn.close()
