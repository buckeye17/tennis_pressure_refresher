"""Synthetic TPMS sensors for development without BLE hardware (PLAN.md section 9.2).

Produces ``fbb0_ac00`` adverts (the B-Qtech format, PLAN.md 5.6) so the whole
pipeline runs exactly as it would with real sensors.

Physics per canister, at the 20 degC reference: gauge pressure decays
exponentially from a fill pressure toward an equilibrium as air diffuses into
the balls; one canister also leaks steadily and never levels off. Actual
pressure follows a daily temperature cycle via the ideal gas law, plus a
little noise.

Broadcasts mimic what we observed: a short burst when the encoded value
changes, otherwise one every ~4.5-6.5 minutes, and silence at 0 psi.
"""

import asyncio
import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass

from canister_monitor.compensation import ZERO_C_IN_K, abs_kpa, psi_to_kpa
from canister_monitor.decoders import Advert
from canister_monitor.decoders import fbb0_ac00 as fmt

REFERENCE_TEMP_C = 20.0
DAY_S = 86_400.0
HOUR_S = 3_600.0
STEP_S = 9.0  # sensors repeat about every 9 s during a burst
BURST_PACKETS = 3
HEARTBEAT_RANGE_S = (270.0, 390.0)


@dataclass(frozen=True)
class SimSensor:
    mac: str
    canister: str
    fill_psi: float  # gauge at reference temperature when filled
    equilibrium_psi: float  # level it settles to as balls absorb air
    tau_h: float  # time constant of that settling, hours
    leak_psi_per_day: float = 0.0
    temp_offset_c: float = 0.0  # each canister sits somewhere slightly different

    def gauge_psi(self, elapsed_s: float, temp_c: float, atmospheric_kpa: float) -> float:
        """Gauge pressure at ``elapsed_s`` after fill and the given gas temperature."""
        ref = self.equilibrium_psi + (self.fill_psi - self.equilibrium_psi) * math.exp(
            -elapsed_s / (self.tau_h * 3600)
        )
        ref -= self.leak_psi_per_day * elapsed_s / DAY_S
        ref_abs = abs_kpa(psi_to_kpa(max(ref, 0.0)), atmospheric_kpa)
        actual_abs = ref_abs * (temp_c + ZERO_C_IN_K) / (REFERENCE_TEMP_C + ZERO_C_IN_K)
        return max(0.0, (actual_abs - atmospheric_kpa) / psi_to_kpa(1.0))


DEFAULT_SENSORS = [
    SimSensor("5E:00:00:00:00:01", "Sim canister 1", 30.0, 22.0, 14.0, temp_offset_c=0.0),
    SimSensor("5E:00:00:00:00:02", "Sim canister 2", 28.0, 21.0, 20.0, temp_offset_c=0.4),
    SimSensor("5E:00:00:00:00:03", "Sim canister 3", 25.0, 19.5, 10.0, temp_offset_c=-0.3),
    SimSensor(
        "5E:00:00:00:00:04", "Sim canister 4 (leak)", 27.0, 21.0, 16.0, 1.5, temp_offset_c=0.2
    ),
]


def ambient_temp_c(ts: float) -> float:
    """Daily cycle of +/-5 degC around 20 degC, warmest mid-afternoon UTC."""
    return REFERENCE_TEMP_C + 5.0 * math.sin(2 * math.pi * ((ts % DAY_S) / DAY_S - 0.375))


def encode_fbb0(
    mac: str, gauge_psi: float, temp_c: float, counter: int, check: int
) -> dict[int, bytes]:
    """Build manufacturer data exactly as bleak would report it for a real sensor."""
    count = max(0, min(255, round(gauge_psi / fmt.PSI_PER_COUNT)))
    temp_byte = max(0, min(255, round(temp_c) + fmt.TEMP_OFFSET_C))
    byte2 = max(0, min(255, round(143 + 0.8 * (temp_c - 13))))
    status = 0x01 if count == 0 else 0x00
    mac_bytes = bytes.fromhex(mac.replace(":", ""))[::-1]
    full = (
        bytes([0xAC, 0x00, byte2, count, temp_byte, status, 0x0A, counter & 0xFF, 0x00])
        + bytes([check & 0xFF, 0x28])
        + mac_bytes
    )
    return {fmt.COMPANY_ID: full[2:]}


@dataclass
class _State:
    last_key: tuple[int, int] | None = None
    last_broadcast: float = -math.inf
    next_heartbeat: float = 0.0
    burst_left: int = 0
    payload: dict[int, bytes] | None = None
    counter: int = 0


class Simulator:
    def __init__(
        self,
        sensors: list[SimSensor],
        start_ts: float,
        speed: float = 1.0,
        atmospheric_kpa: float = 101.325,
        seed: int = 1,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if speed <= 0:
            raise ValueError("speed must be positive")
        self.sensors = sensors
        self.fill_ts = start_ts
        self.speed = speed
        self.atmospheric_kpa = atmospheric_kpa
        self.rng = random.Random(seed)
        self.clock = clock
        self.real_start = clock()
        self.sim_ts = start_ts  # simulated time already emitted up to
        self._state = {s.mac: _State() for s in sensors}

    @classmethod
    def from_args(cls, args, config) -> "Simulator":
        now = time.time()
        return cls(
            DEFAULT_SENSORS,
            start_ts=now - args.start_days_ago * DAY_S,
            speed=args.speed,
            atmospheric_kpa=config.site.atmospheric_kpa,
            seed=args.seed,
        )

    def canister_for(self, mac: str) -> str | None:
        return next((s.canister for s in self.sensors if s.mac == mac), None)

    def target_ts(self) -> float:
        """Simulated 'now': runs at ``speed``; if it started in the past, never passes real now."""
        real_now = self.clock()
        target = self.fill_ts + (real_now - self.real_start) * self.speed
        if self.fill_ts < self.real_start:
            target = min(target, real_now)
        return target

    def step(self, ts: float) -> list[Advert]:
        """Adverts all sensors emit at simulated time ``ts``."""
        adverts = []
        temp_air = ambient_temp_c(ts)
        for sensor in self.sensors:
            st = self._state[sensor.mac]
            # Changes are detected on the smooth physical value; noise is only added to
            # the measurement that gets sent. Otherwise values sitting near a rounding
            # boundary would flicker and trigger a burst every few seconds.
            temp = temp_air + sensor.temp_offset_c
            psi = sensor.gauge_psi(ts - self.fill_ts, temp, self.atmospheric_kpa)
            key = (round(psi / fmt.PSI_PER_COUNT), round(temp))
            if key[0] == 0:
                continue  # real sensors go silent at 0 psi
            if key != st.last_key or ts >= st.next_heartbeat:
                st.counter += 1
                st.payload = encode_fbb0(
                    sensor.mac,
                    max(0.0, psi + self.rng.gauss(0, 0.03)),
                    temp + self.rng.gauss(0, 0.2),
                    st.counter,
                    self.rng.randrange(256),
                )
                st.burst_left = BURST_PACKETS if key != st.last_key else 1
                st.last_key = key
                st.next_heartbeat = ts + self.rng.uniform(*HEARTBEAT_RANGE_S)
            if st.burst_left > 0:
                st.burst_left -= 1
                st.last_broadcast = ts
                adverts.append(
                    Advert(
                        ts=ts,
                        mac=sensor.mac,
                        name=None,
                        rssi=self.rng.randint(-80, -55),
                        manufacturer_data=st.payload,
                        service_uuids=[fmt.SERVICE_UUID],
                        service_data={},
                    )
                )
        return adverts

    def advance(self, submit: Callable[[Advert], None]) -> int:
        """Emit everything due up to the target time; returns the number of adverts."""
        n = 0
        target = self.target_ts()
        while self.sim_ts + STEP_S <= target:
            self.sim_ts += STEP_S
            for adv in self.step(self.sim_ts):
                submit(adv)
                n += 1
        return n

    def scanner_factory(self, submit: Callable[[Advert], None]) -> "SimScanner":
        return SimScanner(self, submit)


# --- leak tests (offline, for checking leak.py) ----------------------------------


@dataclass(frozen=True)
class LeakTest:
    """An empty canister that only leaks.

    The leak rate (fraction of gauge pressure per day) is ``rate_per_day`` at
    ``ref_gauge_psi`` and scales as (gauge / ref) ** beta, so gauge pressure
    obeys dP/dt = -k * P ** (1 + beta).
    """

    fill_psi: float
    rate_per_day: float
    beta: float = 0.0
    ref_gauge_psi: float = 30.0

    def ref_gauge_psi_at(self, elapsed_s: float) -> float:
        """Gauge pressure at the reference temperature, ``elapsed_s`` after the fill."""
        days = elapsed_s / DAY_S
        k = self.rate_per_day / self.ref_gauge_psi**self.beta
        if self.beta == 0:
            return self.fill_psi * math.exp(-k * days)
        base = self.fill_psi ** (-self.beta) + self.beta * k * days
        return base ** (-1 / self.beta) if base > 0 else 0.0


@dataclass(frozen=True)
class Room:
    """What makes real readings messier than the leak alone.

    Weather: atmospheric pressure wanders (the sensor reads gauge pressure, but
    the database adds a fixed atmospheric pressure to it), as a random process
    with standard deviation ``weather_sd_kpa`` and correlation time
    ``weather_tau_h``. The day's mean temperature wanders the same way. The gas
    follows the air temperature ``gas_lag_h`` behind the sensor.
    """

    weather_sd_kpa: float = 0.8
    weather_tau_h: float = 36.0
    temp_sd_c: float = 1.5
    temp_tau_h: float = 48.0
    gas_lag_h: float = 1.0


TYPICAL_ROOM = Room()
QUIET_ROOM = Room(weather_sd_kpa=0.0, temp_sd_c=0.0, gas_lag_h=0.0)


def _ou_step(x: float, dt_h: float, sd: float, tau_h: float, rng: random.Random) -> float:
    """Advance an Ornstein-Uhlenbeck process (mean 0, stationary sd ``sd``)."""
    if sd == 0:
        return 0.0
    a = math.exp(-dt_h / tau_h)
    return a * x + sd * math.sqrt(1 - a * a) * rng.gauss(0, 1)


def leak_test_readings(
    test: LeakTest,
    start_ts: float,
    duration_s: float,
    rng: random.Random,
    room: Room = TYPICAL_ROOM,
    atmospheric_kpa: float = 101.325,
    mac: str = "5E:00:00:00:00:10",
) -> list[tuple[float, float, float]]:
    """Readings (ts, abs kPa, degC) of one leak test, as the database would store them.

    One reading per heartbeat (every 4.5-6.5 min); each goes through the real
    encoder and decoder, so pressure arrives in 0.46 psi steps and temperature
    in whole degrees.
    """
    decoder = fmt.Fbb0Ac00Decoder()
    out = []
    step_s = 60.0
    weather = rng.gauss(0, room.weather_sd_kpa) if room.weather_sd_kpa else 0.0
    temp_drift = rng.gauss(0, room.temp_sd_c) if room.temp_sd_c else 0.0
    t, gas_temp = start_ts, None
    next_reading = start_ts
    while t <= start_ts + duration_s:
        dt_h = step_s / HOUR_S
        weather = _ou_step(weather, dt_h, room.weather_sd_kpa, room.weather_tau_h, rng)
        temp_drift = _ou_step(temp_drift, dt_h, room.temp_sd_c, room.temp_tau_h, rng)
        air = ambient_temp_c(t) + temp_drift
        if gas_temp is None or room.gas_lag_h == 0:
            gas_temp = air
        else:
            gas_temp += (air - gas_temp) * (1 - math.exp(-dt_h / room.gas_lag_h))
        if t >= next_reading:
            ref_abs = abs_kpa(psi_to_kpa(test.ref_gauge_psi_at(t - start_ts)), atmospheric_kpa)
            actual_abs = ref_abs * (gas_temp + ZERO_C_IN_K) / (REFERENCE_TEMP_C + ZERO_C_IN_K)
            gauge = (actual_abs - (atmospheric_kpa + weather)) / psi_to_kpa(1.0)
            mfr = encode_fbb0(
                mac, max(0.0, gauge + rng.gauss(0, 0.03)), air + rng.gauss(0, 0.2), 0, 0
            )
            adv = Advert(t, mac, None, -60, mfr, [fmt.SERVICE_UUID], {})
            reading = decoder.decode(adv, atmospheric_kpa)
            if reading is not None and reading.pressure_kpa_abs > atmospheric_kpa:
                out.append((t, reading.pressure_kpa_abs, reading.temp_c))
            next_reading = t + rng.uniform(*HEARTBEAT_RANGE_S)
        t += step_s
    return out


class SimScanner:
    """Looks like a BLE scanner to the collector; drives the shared Simulator."""

    def __init__(self, sim: Simulator, submit: Callable[[Advert], None], poll_s: float = 0.05):
        self.sim = sim
        self.submit = submit
        self.poll_s = poll_s
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            self.sim.advance(self.submit)
            await asyncio.sleep(self.poll_s)
