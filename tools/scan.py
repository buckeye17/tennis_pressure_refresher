"""Phase 0 BLE discovery scanner.

Listens for BLE advertisements and prints each device the first time it is
seen and again whenever its advertised payload changes. Devices that look like
the TPMS formats in PLAN.md section 5 are marked with ``***``. Devices that
first appear after the baseline period are marked ``NEW`` so a sensor that
wakes up mid-scan (e.g. when put under pressure) stands out from the
background devices.

Scan only; this never connects to or pairs with anything.

Usage (on the Pi):
    .venv/bin/python tools/scan.py                 # scan until Ctrl+C
    .venv/bin/python tools/scan.py --duration 120  # stop after 2 minutes
    .venv/bin/python tools/scan.py --tpms-only     # hide non-TPMS devices
    .venv/bin/python tools/scan.py --mac AA:BB:CC:DD:EE:FF --every
"""

from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import dataclass, field

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

BR_SERVICE_UUID = "000027a5-0000-1000-8000-00805f9b34fb"
TPMS_0100_COMPANY_ID = 0x0100
FBB0_SERVICE_UUID = "0000fbb0-0000-1000-8000-00805f9b34fb"  # B-Qtech, PLAN.md 5.6


@dataclass
class DeviceState:
    """What we have seen from one MAC so far."""

    mac: str
    first_seen: float
    is_new: bool
    count: int = 0
    rssi: int | None = None
    name: str | None = None
    signature: tuple = ()
    payload_changes: int = 0
    tpms_reasons: list[str] = field(default_factory=list)


def tpms_reasons(name: str | None, adv: AdvertisementData) -> list[str]:
    """Return why this advert looks like a known TPMS format (empty if not)."""
    reasons = []
    if name == "BR":
        reasons.append('name "BR"')
    if name and name.upper().startswith("TPMS"):
        reasons.append(f'name "{name}"')
    if BR_SERVICE_UUID in (u.lower() for u in adv.service_uuids):
        reasons.append("uuid 0x27a5")
    if TPMS_0100_COMPANY_ID in adv.manufacturer_data:
        reasons.append("company id 0x0100")
    if FBB0_SERVICE_UUID in (u.lower() for u in adv.service_uuids):
        reasons.append("uuid 0xfbb0")
    return reasons


def payload_signature(name: str | None, adv: AdvertisementData) -> tuple:
    """Everything except RSSI, so we can tell when the payload actually changes."""
    return (
        name,
        tuple(sorted((cid, bytes(p)) for cid, p in adv.manufacturer_data.items())),
        tuple(sorted(adv.service_uuids)),
        tuple(sorted((u, bytes(d)) for u, d in adv.service_data.items())),
    )


def format_advert(adv: AdvertisementData) -> list[str]:
    """Render the interesting parts of an advert as indented detail lines."""
    lines = []
    for cid, payload in adv.manufacturer_data.items():
        # Bleak splits off the first two bytes as a little-endian company ID;
        # cheap sensors often put data there, so also show the full bytes.
        full = cid.to_bytes(2, "little") + bytes(payload)
        lines.append(
            f"    mfr  cid=0x{cid:04X}  payload={bytes(payload).hex().upper()}"
            f"  full={full.hex().upper()} ({len(full)} B)"
        )
    for uuid, data in adv.service_data.items():
        lines.append(f"    svc-data {uuid}: {bytes(data).hex().upper()}")
    if adv.service_uuids:
        lines.append(f"    uuids {', '.join(adv.service_uuids)}")
    return lines


async def run(args: argparse.Namespace) -> None:
    devices: dict[str, DeviceState] = {}
    start = time.monotonic()
    mac_filter = {m.upper() for m in args.mac}

    def on_advert(device: BLEDevice, adv: AdvertisementData) -> None:
        mac = device.address.upper()
        if mac_filter and mac not in mac_filter:
            return
        if args.min_rssi is not None and adv.rssi < args.min_rssi:
            return

        now = time.monotonic()
        name = adv.local_name or device.name
        state = devices.get(mac)
        is_first = state is None
        if state is None:
            state = DeviceState(mac=mac, first_seen=now, is_new=(now - start) > args.baseline)
            devices[mac] = state

        state.count += 1
        state.rssi = adv.rssi
        state.name = name or state.name
        for reason in tpms_reasons(name, adv):
            if reason not in state.tpms_reasons:
                state.tpms_reasons.append(reason)

        sig = payload_signature(name, adv)
        changed = sig != state.signature
        if changed and not is_first:
            state.payload_changes += 1
        state.signature = sig

        if args.tpms_only and not state.tpms_reasons:
            return
        if not (is_first or changed or args.every):
            return

        tags = []
        if state.tpms_reasons:
            tags.append("*** TPMS? (" + ", ".join(state.tpms_reasons) + ")")
        if state.is_new:
            tags.append("NEW")
        event = "first seen" if is_first else ("changed" if changed else "repeat")
        stamp = time.strftime("%H:%M:%S")
        print(
            f"{stamp}  {mac}  rssi={adv.rssi:>4}  name={name!r:<20} "
            f"[{event}] {' '.join(tags)}".rstrip()
        )
        for line in format_advert(adv):
            print(line)

    scanner = BleakScanner(detection_callback=on_advert, scanning_mode="active")
    print(
        f"Scanning (active). Devices first seen after {args.baseline:.0f}s are "
        "marked NEW. Press Ctrl+C to stop.\n"
    )
    await scanner.start()
    try:
        if args.duration:
            await asyncio.sleep(args.duration)
        else:
            await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        await scanner.stop()
        print_summary(devices, start)


def print_summary(devices: dict[str, DeviceState], start: float) -> None:
    print(f"\n=== Summary: {len(devices)} device(s) seen ===")
    print(f"{'MAC':<17}  {'RSSI':>4}  {'adverts':>7}  {'changes':>7}  {'first@':>7}  name / notes")

    def sort_key(d: DeviceState) -> tuple:
        return (not d.tpms_reasons, not d.is_new, -(d.rssi or -999))

    for d in sorted(devices.values(), key=sort_key):
        notes = []
        if d.tpms_reasons:
            notes.append("TPMS? " + ", ".join(d.tpms_reasons))
        if d.is_new:
            notes.append("NEW")
        print(
            f"{d.mac:<17}  {d.rssi if d.rssi is not None else '':>4}  "
            f"{d.count:>7}  {d.payload_changes:>7}  "
            f"{d.first_seen - start:>6.0f}s  {d.name or '-'}"
            + (f"  [{'; '.join(notes)}]" if notes else "")
        )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--duration", type=float, default=0, help="seconds to scan (default: until Ctrl+C)"
    )
    p.add_argument(
        "--baseline",
        type=float,
        default=30,
        help="devices first seen after this many seconds are marked NEW",
    )
    p.add_argument("--mac", action="append", default=[], help="only show this MAC (repeatable)")
    p.add_argument(
        "--min-rssi", type=int, default=None, help="ignore adverts weaker than this, e.g. -80"
    )
    p.add_argument(
        "--tpms-only", action="store_true", help="only print devices matching a known TPMS format"
    )
    p.add_argument(
        "--every", action="store_true", help="print every advert, not just first-seen/changed ones"
    )
    args = p.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
