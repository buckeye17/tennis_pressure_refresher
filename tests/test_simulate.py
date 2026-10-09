import asyncio

import pytest

from canister_monitor import collector as col
from canister_monitor import db, simulate
from canister_monitor.compensation import gauge_kpa, kpa_to_psi
from canister_monitor.config import parse_config
from canister_monitor.decoders import Advert, get_decoders
from canister_monitor.decoders.fbb0_ac00 import Fbb0Ac00Decoder

ATM = 101.325
DAY = simulate.DAY_S


class FakeClock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def decode(adv: Advert):
    reading = Fbb0Ac00Decoder().decode(adv, ATM)
    assert reading is not None
    return kpa_to_psi(gauge_kpa(reading.pressure_kpa_abs, ATM)), reading


@pytest.mark.parametrize("psi, temp", [(0.5, 13.0), (17.8, 13.0), (45.1, 14.0), (28.3, -2.0)])
def test_encoding_round_trips_through_real_decoder(psi, temp):
    mac = "5E:00:00:00:00:01"
    mfr = simulate.encode_fbb0(mac, psi, temp, counter=7, check=0x5A)
    adv = Advert(0.0, mac, None, -60, mfr, [simulate.fmt.SERVICE_UUID], {})
    decoded_psi, reading = decode(adv)
    assert decoded_psi == pytest.approx(psi, abs=0.46 / 2 + 1e-9)  # within half a count
    assert reading.temp_c == round(temp)
    assert reading.flags["mac_match"] is True


def test_physics_plateau_vs_leak():
    healthy, leaky = simulate.DEFAULT_SENSORS[0], simulate.DEFAULT_SENSORS[3]
    assert leaky.leak_psi_per_day > 0 and healthy.leak_psi_per_day == 0

    def at(sensor, days):
        return sensor.gauge_psi(days * DAY, 20.0, ATM)

    assert at(healthy, 0) == pytest.approx(healthy.fill_psi)
    assert at(healthy, 5) == pytest.approx(healthy.equilibrium_psi, abs=0.01)  # levelled off
    assert at(healthy, 6) - at(healthy, 5) == pytest.approx(0.0, abs=0.01)
    assert at(leaky, 6) - at(leaky, 5) == pytest.approx(-leaky.leak_psi_per_day, abs=0.05)


def test_temperature_follows_ideal_gas_law():
    s = simulate.DEFAULT_SENSORS[0]
    cold = s.gauge_psi(0.0, 15.0, ATM)
    warm = s.gauge_psi(0.0, 25.0, ATM)
    abs_cold = cold * 6.894757 + ATM
    abs_warm = warm * 6.894757 + ATM
    assert abs_warm / abs_cold == pytest.approx(298.15 / 288.15)


def test_ambient_cycle():
    temps = [simulate.ambient_temp_c(t) for t in range(0, int(DAY), 600)]
    assert max(temps) == pytest.approx(25.0, abs=0.01)
    assert min(temps) == pytest.approx(15.0, abs=0.01)


def test_broadcasts_on_change_plus_heartbeat_and_silent_at_zero(monkeypatch):
    sensor = simulate.SimSensor("5E:00:00:00:00:09", "x", 20.0, 20.0, 1.0)
    sim = simulate.Simulator([sensor], start_ts=0.0, seed=3, clock=FakeClock(0.0))
    # Freeze temperature and noise so the encoded value never changes.
    monkeypatch.setattr(simulate, "ambient_temp_c", lambda ts: 20.0)
    monkeypatch.setattr(sim.rng, "gauss", lambda mu, sigma: 0.0)
    times = [t * simulate.STEP_S for t in range(1, 400)]  # ~1 hour
    adverts = [a for t in times for a in sim.step(t)]

    ts = [a.ts for a in adverts]
    assert ts[:3] == times[:3]  # initial burst: same packet three times
    new_reading_ts = [ts[0], *ts[3:]]  # after the burst, every broadcast is a heartbeat
    gaps = [b - a for a, b in zip(new_reading_ts, new_reading_ts[1:], strict=False)]
    assert len(gaps) >= 8
    assert all(270 <= g <= 390 + simulate.STEP_S for g in gaps)
    payloads = {a.manufacturer_data[0x00AC] for a in adverts}
    assert len(payloads) == len(adverts) - 2  # burst repeats one payload; heartbeats differ

    empty = simulate.SimSensor("5E:00:00:00:00:0A", "y", 0.0, 0.0, 1.0)
    sim0 = simulate.Simulator([empty], start_ts=0.0, clock=FakeClock(0.0))
    assert [a for t in times for a in sim0.step(t)] == []


def test_speed_and_catch_up_to_real_time():
    clock = FakeClock(10_000.0)
    sim = simulate.Simulator(
        simulate.DEFAULT_SENSORS, start_ts=10_000.0 - DAY, speed=600, clock=clock
    )
    clock.t += 60  # one real minute = 10 simulated hours
    assert sim.target_ts() == pytest.approx(10_000.0 - DAY + 36_000)
    clock.t += 600  # would be 100 h ahead: capped at real now
    assert sim.target_ts() == clock.t

    ahead = simulate.Simulator(simulate.DEFAULT_SENSORS, start_ts=clock.t, speed=10, clock=clock)
    clock.t += 1
    assert ahead.target_ts() == pytest.approx(clock.t - 1 + 10)  # started now: runs ahead


def test_simulator_feeds_collector_end_to_end(tmp_path):
    """A simulated day through the real pipeline produces sane readings."""
    config = parse_config({})
    conn = db.connect(tmp_path / "sim.db")
    clock = FakeClock(1_000_000.0)
    sim = simulate.Simulator(
        simulate.DEFAULT_SENSORS, start_ts=clock.t - DAY, speed=1_000_000, clock=clock
    )
    pipeline = col.Pipeline(
        conn, config, get_decoders(["fbb0_ac00"]), canister_for=sim.canister_for
    )
    clock.t += 1  # target = real now (one simulated day)
    collector = col.Collector(pipeline, sim.scanner_factory, silent_restart_s=1200)

    async def scenario():
        await collector.start_scanner()
        await asyncio.sleep(0.2)
        await collector.stop_scanner()
        collector.process_pending()
        pipeline.flush_seen()

    asyncio.run(scenario())

    sensors = conn.execute("SELECT mac, canister FROM sensors ORDER BY mac").fetchall()
    assert [s["canister"] for s in sensors] == [s.canister for s in simulate.DEFAULT_SENSORS]
    per_sensor = conn.execute(
        "SELECT mac, COUNT(*) n, MIN(ts) t0, MAX(ts) t1 FROM readings GROUP BY mac"
    ).fetchall()
    assert len(per_sensor) == 4
    for row in per_sensor:
        assert row["n"] > 200  # at least one every ~6.5 min for a day
        assert row["t1"] - row["t0"] > 0.99 * DAY
    raw = conn.execute("SELECT COUNT(*) FROM raw_adverts").fetchone()[0]
    total_adverts = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    assert raw >= total_adverts  # every stored reading has its raw advert
    conn.close()
