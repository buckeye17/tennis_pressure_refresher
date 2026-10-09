import asyncio
import json
from types import SimpleNamespace

import pytest

from canister_monitor import collector as col
from canister_monitor import db
from canister_monitor.config import parse_config
from canister_monitor.decoders import Advert, get_decoders

MAC = "30:94:A8:11:11:11"
CONFIGURED_ONLY = "AA:BB:CC:00:00:01"  # in config, but no decoder matches it
STRANGER = "11:22:33:44:55:66"
FBB0_UUID = "0000fbb0-0000-1000-8000-00805f9b34fb"
P_45 = bytes.fromhex("8F6344000A2A008028111111A89430")  # 45.1 psi, 13 C
P_33 = bytes.fromhex("8F4944000A8200A328111111A89430")  # 33.3 psi, 13 C

CONFIG = parse_config(
    {
        "collector": {"raw_heartbeat_minutes": 10, "reading_heartbeat_minutes": 1},
        "sensors": [
            {"mac": MAC, "canister": "Canister 1"},
            {"mac": CONFIGURED_ONLY, "canister": "Mystery"},
        ],
    }
)


def sensor_adv(ts, payload=P_45, rssi=-60) -> Advert:
    return Advert(ts, MAC, None, rssi, {0x00AC: payload}, [FBB0_UUID], {})


def other_adv(ts, mac, mfr=None) -> Advert:
    return Advert(ts, mac, "thing", -70, mfr if mfr is not None else {0x004C: b"\x10\x05"}, [], {})


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "c.db")
    yield c
    c.close()


def make_pipeline(conn, discover=False) -> col.Pipeline:
    return col.Pipeline(conn, CONFIG, get_decoders(["fbb0_ac00"]), discover=discover)


def rows(conn, sql):
    return [tuple(r) for r in conn.execute(sql)]


# --- pipeline: scripted adverts -> exact rows ------------------------------------


def test_scripted_adverts_produce_exact_rows(conn, caplog):
    caplog.set_level("INFO", logger="canister_monitor.collector")
    p = make_pipeline(conn)
    script = [
        sensor_adv(1000.0, rssi=-60),  # raw 1 + reading 1
        sensor_adv(1009.0, rssi=-58),  # same burst: nothing stored, last_seen updates
        other_adv(1010.0, STRANGER),  # unknown device, not discover mode: ignored
        other_adv(1011.0, CONFIGURED_ONLY),  # configured MAC: raw 2, no reading
        sensor_adv(1060.0, rssi=-61),  # reading heartbeat: reading 2 -> raw 1
        sensor_adv(1100.0, P_33, rssi=-62),  # change: raw 3 + reading 3
        sensor_adv(1700.0, P_33, rssi=-63),  # raw heartbeat: raw 4 + reading 4
    ]
    for adv in script:
        p.handle(adv)
    p.flush_seen()

    assert rows(conn, "SELECT id, ts, mac, rssi FROM raw_adverts ORDER BY id") == [
        (1, 1000.0, MAC, -60),
        (2, 1011.0, CONFIGURED_ONLY, -70),
        (3, 1100.0, MAC, -62),
        (4, 1700.0, MAC, -63),
    ]
    readings = conn.execute(
        "SELECT ts, mac, pressure_kpa_abs, temp_c, decoder, raw_advert_id, flags_json "
        "FROM readings ORDER BY id"
    ).fetchall()
    assert [(r["ts"], r["raw_advert_id"]) for r in readings] == [
        (1000.0, 1),
        (1060.0, 1),
        (1100.0, 3),
        (1700.0, 4),
    ]
    assert {r["mac"] for r in readings} == {MAC}
    assert all(r["decoder"] == "fbb0_ac00" and r["temp_c"] == 13.0 for r in readings)
    assert readings[0]["pressure_kpa_abs"] == pytest.approx(101.325 + 99 * 0.4558 * 6.894757)
    assert json.loads(readings[0]["flags_json"])["status"] == 0

    assert rows(
        conn,
        "SELECT mac, canister, decoder, first_seen, last_seen, last_rssi FROM sensors ORDER BY mac",
    ) == [
        (MAC, "Canister 1", "fbb0_ac00", 1000.0, 1700.0, -63),
        (CONFIGURED_ONLY, "Mystery", None, 1011.0, 1011.0, -70),
    ]
    stored = [r.getMessage() for r in caplog.records if "psi" in r.getMessage()]
    assert stored[0] == f"Canister 1 ({MAC}): 45.1 psi, 13 °C, RSSI -60"
    assert len(stored) == 4


def test_discover_mode_stores_raw_from_unknown_devices_only(conn):
    p = make_pipeline(conn, discover=True)
    p.handle(other_adv(1.0, STRANGER))  # has manufacturer data: raw stored
    p.handle(other_adv(2.0, "22:22:22:22:22:22", mfr={}))  # no manufacturer data: ignored
    p.flush_seen()
    assert rows(conn, "SELECT mac FROM raw_adverts") == [(STRANGER,)]
    assert rows(conn, "SELECT COUNT(*) FROM readings") == [(0,)]
    assert rows(conn, "SELECT COUNT(*) FROM sensors") == [(0,)]  # strangers aren't sensors


def test_unassigned_sensor_is_recorded(conn):
    p = make_pipeline(conn)
    other = Advert(5.0, "30:94:A8:22:22:22", None, -70, {0x00AC: P_45}, [FBB0_UUID], {})
    assert p.handle(other) is not None
    p.flush_seen()
    assert rows(conn, "SELECT mac, canister FROM sensors") == [("30:94:A8:22:22:22", None)]
    assert rows(conn, "SELECT COUNT(*) FROM readings") == [(1,)]


def test_seen_batch_keeps_newest(conn):
    p = make_pipeline(conn)
    p.handle(sensor_adv(2000.0, rssi=-50))
    p.handle(sensor_adv(1500.0, rssi=-90))  # late, out-of-order advert
    p.flush_seen()
    assert rows(conn, "SELECT last_seen, last_rssi FROM sensors") == [(2000.0, -50)]


# --- watchdog with fake scanner and clock ----------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class FakeScanner:
    def __init__(self, log: list, fail: bool) -> None:
        self.log, self.fail = log, fail

    async def start(self) -> None:
        self.log.append("start")
        if self.fail:
            raise RuntimeError("adapter not ready")

    async def stop(self) -> None:
        self.log.append("stop")


def make_collector(conn, clock, start_results: list[bool], log: list) -> col.Collector:
    """Scanner factory whose Nth start() fails if start_results[N] is False."""
    results = iter(start_results)

    def factory(submit):
        return FakeScanner(log, fail=not next(results, True))

    return col.Collector(
        make_pipeline(conn), factory, silent_restart_s=1200, clock=clock, retry_delay_s=0
    )


def test_watchdog_restarts_after_silence(conn):
    async def scenario():
        clock, log = FakeClock(), []
        c = make_collector(conn, clock, [True, True], log)
        await c.start_scanner()
        clock.t = 600
        c.submit(sensor_adv(600.0))
        c.process_pending()  # a matching advert resets the silence timer
        clock.t = 1700
        assert await c.watchdog_tick() is False  # only 1100 s silent
        clock.t = 1800
        assert await c.watchdog_tick() is True
        assert log == ["start", "stop", "start"]
        assert c.restarts == 1
        clock.t = 2000
        assert await c.watchdog_tick() is False  # timer reset by the restart

    asyncio.run(scenario())


def test_non_matching_adverts_do_not_feed_the_watchdog(conn):
    async def scenario():
        clock, log = FakeClock(), []
        c = make_collector(conn, clock, [True, True], log)
        await c.start_scanner()
        clock.t = 1199
        c.submit(other_adv(1199.0, STRANGER))
        c.process_pending()
        clock.t = 1200
        assert await c.watchdog_tick() is True

    asyncio.run(scenario())


def test_restart_retries_then_succeeds(conn):
    async def scenario():
        clock, log = FakeClock(), []
        c = make_collector(conn, clock, [True, False, False, True], log)
        await c.start_scanner()
        clock.t = 5000
        assert await c.watchdog_tick() is True
        assert log.count("start") == 4
        assert c.scanner is not None

    asyncio.run(scenario())


def test_three_failed_restarts_are_fatal(conn):
    async def scenario():
        clock, log = FakeClock(), []
        c = make_collector(conn, clock, [True, False, False, False], log)
        await c.start_scanner()
        clock.t = 5000
        with pytest.raises(col.CollectorFatal):
            await c.watchdog_tick()

    asyncio.run(scenario())


# --- run loop: shutdown flushes everything ------------------------------------------


def test_run_processes_queue_and_flushes_on_stop(conn):
    async def scenario():
        log = []
        c = col.Collector(
            make_pipeline(conn),
            lambda submit: FakeScanner(log, fail=False),
            silent_restart_s=1200,
            tick_s=0.01,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(c.run(stop))
        await asyncio.sleep(0.05)
        c.submit(sensor_adv(1000.0))
        c.submit(sensor_adv(1100.0, P_33))
        stop.set()
        await task
        assert log == ["start", "stop"]

    asyncio.run(scenario())
    assert rows(conn, "SELECT COUNT(*) FROM readings") == [(2,)]
    assert rows(conn, "SELECT last_seen FROM sensors") == [(1100.0,)]


def test_advert_from_bleak():
    device = SimpleNamespace(address="30:94:a8:11:11:11")
    data = SimpleNamespace(
        local_name=None,
        rssi=-61,
        manufacturer_data={0x00AC: bytearray(P_45)},
        service_uuids=[FBB0_UUID],
        service_data={},
    )
    adv = col.advert_from_bleak(device, data, ts=12.5)
    assert adv == Advert(12.5, MAC, None, -61, {0x00AC: P_45}, [FBB0_UUID], {})
    assert type(adv.manufacturer_data[0x00AC]) is bytes


def test_windows_out_of_range_notifications_are_dropped():
    assert col.is_real_advert(SimpleNamespace(rssi=-90)) is True
    assert col.is_real_advert(SimpleNamespace(rssi=None)) is True
    assert col.is_real_advert(SimpleNamespace(rssi=-127)) is False


def test_main_reports_config_errors(tmp_path, capsys):
    assert col.main(["--config", str(tmp_path / "missing.toml")]) == 2
    assert "config.example.toml" in capsys.readouterr().err
