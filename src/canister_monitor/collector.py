"""BLE collector: scan, decode, store (PLAN.md section 9.1).

    python -m canister_monitor.collector [--config PATH] [--discover] [--simulate]

Structure:
- ``Pipeline`` is the synchronous core: one Advert in, rows written. No
  asyncio or bleak, so it is easy to test.
- ``Collector`` owns the scanner, a queue between the scanner callback and
  the pipeline, the periodic flush of "last seen" updates, and the watchdog.
- Scanners are created by a factory taking a ``submit(Advert)`` callback, so
  the real bleak scanner, the simulator and test fakes are interchangeable.
"""

import argparse
import asyncio
import logging
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from canister_monitor import db
from canister_monitor.compensation import gauge_kpa, kpa_to_psi
from canister_monitor.config import Config, ConfigError, load_config
from canister_monitor.decoders import Advert, Decoder, Reading, find_decoder, get_decoders
from canister_monitor.write_policy import WritePolicy

log = logging.getLogger("canister_monitor.collector")


class Scanner(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...


ScannerFactory = Callable[[Callable[[Advert], None]], Scanner]


class CollectorFatal(RuntimeError):
    """The scanner could not be (re)started; exit so systemd restarts us."""


# --- pipeline -----------------------------------------------------------------


class Pipeline:
    """Turn adverts into stored rows, applying the section 7 write policy."""

    def __init__(
        self,
        conn,
        config: Config,
        decoders: list[Decoder],
        discover: bool = False,
        canister_for: Callable[[str], str | None] | None = None,
    ) -> None:
        self.conn = conn
        self.config = config
        self.decoders = decoders
        self.discover = discover
        self.canister_for = canister_for or config.canister_for
        self.policy = WritePolicy(
            raw_heartbeat_s=config.collector.raw_heartbeat_minutes * 60,
            reading_heartbeat_s=config.collector.reading_heartbeat_minutes * 60,
        )
        self._seen: dict[str, db.SensorSeen] = {}

    def handle(self, adv: Advert) -> Reading | None:
        """Store what the policy allows; return the decoded reading, if any."""
        decoder = find_decoder(adv, self.decoders)
        canister = self.canister_for(adv.mac)
        tracked = decoder is not None or canister is not None
        if not (tracked or (self.discover and adv.manufacturer_data)):
            return None

        if tracked:
            prev = self._seen.get(adv.mac)
            if prev is None or adv.ts >= prev.ts:
                self._seen[adv.mac] = db.SensorSeen(
                    adv.mac, adv.ts, adv.rssi, decoder.name if decoder else None, canister
                )

        # Raw first: never lose bytes because a decoder is wrong.
        if self.policy.raw_due(adv):
            self.policy.raw_stored(adv, db.insert_raw_advert(self.conn, adv))

        if decoder is None:
            return None
        try:
            reading = decoder.decode(adv, self.config.site.atmospheric_kpa)
        except Exception:
            log.exception("decoder %s raised for %s", decoder.name, adv.mac)
            return None
        if reading is None:
            log.warning("decoder %s matched but could not decode %s", decoder.name, adv.mac)
            return None
        if self.policy.reading_due(reading):
            db.insert_reading(self.conn, reading, self.policy.last_raw_id(adv.mac))
            self.policy.reading_stored(reading)
            self._log_reading(reading, canister, adv.rssi)
        return reading

    def flush_seen(self) -> None:
        """Write batched sensors.last_seen / last_rssi updates."""
        if self._seen:
            db.upsert_sensors_seen(self.conn, list(self._seen.values()))
            self._seen.clear()

    def _log_reading(self, reading: Reading, canister: str | None, rssi: int | None) -> None:
        psi = kpa_to_psi(gauge_kpa(reading.pressure_kpa_abs, self.config.site.atmospheric_kpa))
        temp = "?" if reading.temp_c is None else f"{reading.temp_c:g}"
        log.info(
            "%s (%s): %.1f psi, %s °C, RSSI %s",
            canister or "Unassigned",
            reading.mac,
            psi,
            temp,
            rssi,
        )


# --- async collector ------------------------------------------------------------


class Collector:
    def __init__(
        self,
        pipeline: Pipeline,
        scanner_factory: ScannerFactory,
        silent_restart_s: float,
        clock: Callable[[], float] = time.monotonic,
        tick_s: float = 5.0,
        max_start_attempts: int = 3,
        retry_delay_s: float = 5.0,
    ) -> None:
        self.pipeline = pipeline
        self.scanner_factory = scanner_factory
        self.silent_restart_s = silent_restart_s
        self.clock = clock
        self.tick_s = tick_s  # flush + watchdog interval; must be <= 10 s per PLAN.md
        self.max_start_attempts = max_start_attempts
        self.retry_delay_s = retry_delay_s
        self.queue: asyncio.Queue[Advert] = asyncio.Queue()
        self.scanner: Scanner | None = None
        self.last_match = clock()
        self.restarts = 0

    def submit(self, adv: Advert) -> None:
        """Scanner callback: enqueue only, never block."""
        self.queue.put_nowait(adv)

    def process_pending(self) -> int:
        """Run every queued advert through the pipeline; returns how many."""
        n = 0
        while not self.queue.empty():
            self._process(self.queue.get_nowait())
            n += 1
        return n

    def _process(self, adv: Advert) -> None:
        try:
            if self.pipeline.handle(adv) is not None:
                self.last_match = self.clock()
        except Exception:
            log.exception("failed to store advert from %s", adv.mac)

    async def _consume(self) -> None:
        while True:
            self._process(await self.queue.get())

    async def start_scanner(self) -> None:
        """Start a fresh scanner, retrying; raise CollectorFatal after repeated failures."""
        for attempt in range(1, self.max_start_attempts + 1):
            await self.stop_scanner()
            try:
                self.scanner = self.scanner_factory(self.submit)
                await self.scanner.start()
                self.last_match = self.clock()
                return
            except Exception as e:
                self.scanner = None
                log.error(
                    "scanner start failed (attempt %d/%d): %s", attempt, self.max_start_attempts, e
                )
                if attempt < self.max_start_attempts:
                    await asyncio.sleep(self.retry_delay_s)
        raise CollectorFatal(f"scanner failed to start {self.max_start_attempts} times")

    async def stop_scanner(self) -> None:
        if self.scanner is not None:
            try:
                await self.scanner.stop()
            except Exception as e:
                log.warning("scanner stop failed: %s", e)
            self.scanner = None

    async def watchdog_tick(self) -> bool:
        """Restart the scanner if nothing matched for too long. Returns True if restarted."""
        silent = self.clock() - self.last_match
        if silent < self.silent_restart_s:
            return False
        log.warning("no matching adverts for %.0f min; restarting scanner", silent / 60)
        self.restarts += 1
        await self.start_scanner()
        return True

    async def run(self, stop: asyncio.Event) -> None:
        await self.start_scanner()
        consumer = asyncio.create_task(self._consume())
        try:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.tick_s)
                except TimeoutError:
                    pass
                self.pipeline.flush_seen()
                if not stop.is_set():
                    await self.watchdog_tick()
        finally:
            await self.stop_scanner()
            consumer.cancel()
            self.process_pending()
            self.pipeline.flush_seen()


# --- bleak --------------------------------------------------------------------


def advert_from_bleak(device, data, ts: float | None = None) -> Advert:
    """Convert bleak's (BLEDevice, AdvertisementData) to an Advert."""
    return Advert(
        ts=time.time() if ts is None else ts,
        mac=device.address.upper(),
        name=data.local_name,
        rssi=data.rssi,
        manufacturer_data={cid: bytes(p) for cid, p in data.manufacturer_data.items()},
        service_uuids=list(data.service_uuids),
        service_data={u: bytes(d) for u, d in data.service_data.items()},
    )


OUT_OF_RANGE_RSSI = -127  # Windows reports "device lost" as an advert with this RSSI


def is_real_advert(data) -> bool:
    """False for Windows' out-of-range notifications, which repeat the last payload."""
    return data.rssi is None or data.rssi > OUT_OF_RANGE_RSSI


def bleak_scanner_factory(submit: Callable[[Advert], None]) -> Scanner:
    from bleak import BleakScanner  # imported lazily: tests and --simulate don't need BLE

    def callback(device, data) -> None:
        if is_real_advert(data):
            submit(advert_from_bleak(device, data))

    return BleakScanner(detection_callback=callback, scanning_mode="active")


# --- entry point --------------------------------------------------------------


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows: fall back to a plain handler
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))


async def _main_async(args: argparse.Namespace, config: Config) -> None:
    stop = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop)
    if args.duration:
        asyncio.get_running_loop().call_later(args.duration, stop.set)

    db_path = Path(args.db) if args.db else config.collector.db_path
    canister_for = None
    if args.simulate:
        from canister_monitor import simulate

        if not args.db:
            db_path = db_path.with_name(f"{db_path.stem}-sim{db_path.suffix}")
        sim = simulate.Simulator.from_args(args, config)
        factory = sim.scanner_factory
        canister_for = sim.canister_for
    else:
        factory = bleak_scanner_factory

    conn = db.connect(db_path)
    log.info("database: %s", db_path)
    pipeline = Pipeline(
        conn,
        config,
        get_decoders(config.collector.decoders),
        discover=args.discover,
        canister_for=canister_for,
    )
    collector = Collector(
        pipeline,
        factory,
        silent_restart_s=config.collector.scanner_restart_if_silent_minutes * 60,
    )
    try:
        await collector.run(stop)
    finally:
        conn.close()
        log.info("stopped")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m canister_monitor.collector", description=__doc__)
    p.add_argument("--config", default="config.toml", help="config file (default: config.toml)")
    p.add_argument("--db", help="override the database path from config")
    p.add_argument(
        "--discover", action="store_true", help="also store raw adverts from unknown devices"
    )
    p.add_argument("--duration", type=float, help="stop after this many seconds")
    sim = p.add_argument_group("simulator")
    sim.add_argument(
        "--simulate", action="store_true", help="feed synthetic sensors instead of BLE"
    )
    sim.add_argument("--speed", type=float, default=1.0, help="simulated time runs N x faster")
    sim.add_argument(
        "--start-days-ago",
        type=float,
        default=0.0,
        help="start the simulated clock this many days back; it fast-forwards at --speed "
        "and continues in real time once it reaches now",
    )
    sim.add_argument("--seed", type=int, default=1, help="random seed for the simulator")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    logging.basicConfig(
        level=config.collector.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(_main_async(args, config))
    except CollectorFatal as e:
        log.critical("%s", e)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
