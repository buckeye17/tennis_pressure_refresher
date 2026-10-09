"""Decide which adverts and readings to store (PLAN.md section 7, "Write policy").

Sensors may repeat the same packet many times. To keep the database small
without losing information:

- a raw advert is stored when its payload differs from the last stored one for
  that MAC, or when ``raw_heartbeat_s`` has passed since then;
- a reading is stored when its decoded values differ from the last stored
  reading for that MAC, or when ``reading_heartbeat_s`` has passed.

Time comes from the advert/reading timestamps, so tests can drive it directly.
If the clock jumps backwards (e.g. NTP correction), the next item is stored.
"""

import json
from dataclasses import dataclass

from canister_monitor.decoders.base import Advert, Reading


def advert_signature(adv: Advert) -> tuple:
    """Everything in an advert except RSSI and time."""
    return (
        adv.name,
        tuple(sorted((cid, bytes(p)) for cid, p in adv.manufacturer_data.items())),
        tuple(sorted(u.lower() for u in adv.service_uuids)),
        tuple(sorted((u.lower(), bytes(d)) for u, d in adv.service_data.items())),
    )


def reading_signature(reading: Reading) -> tuple:
    """The decoded values of a reading, excluding time."""
    return (
        reading.pressure_kpa_abs,
        reading.temp_c,
        reading.battery_pct,
        reading.battery_v,
        json.dumps(reading.flags, sort_keys=True),
        reading.decoder,
    )


@dataclass
class _Last:
    signature: tuple
    ts: float


class WritePolicy:
    def __init__(self, raw_heartbeat_s: float, reading_heartbeat_s: float) -> None:
        self.raw_heartbeat_s = raw_heartbeat_s
        self.reading_heartbeat_s = reading_heartbeat_s
        self._raw: dict[str, _Last] = {}
        self._raw_ids: dict[str, int] = {}
        self._reading: dict[str, _Last] = {}

    @staticmethod
    def _due(last: _Last | None, signature: tuple, ts: float, heartbeat_s: float) -> bool:
        if last is None or signature != last.signature or ts < last.ts:
            return True
        return ts - last.ts >= heartbeat_s

    def raw_due(self, adv: Advert) -> bool:
        return self._due(
            self._raw.get(adv.mac), advert_signature(adv), adv.ts, self.raw_heartbeat_s
        )

    def raw_stored(self, adv: Advert, raw_id: int) -> None:
        self._raw[adv.mac] = _Last(advert_signature(adv), adv.ts)
        self._raw_ids[adv.mac] = raw_id

    def last_raw_id(self, mac: str) -> int | None:
        """Row id of the last stored raw advert for this MAC.

        When a reading is stored on its heartbeat but the raw advert was not
        (unchanged payload), the reading links to this identical stored advert.
        """
        return self._raw_ids.get(mac)

    def reading_due(self, reading: Reading) -> bool:
        return self._due(
            self._reading.get(reading.mac),
            reading_signature(reading),
            reading.ts,
            self.reading_heartbeat_s,
        )

    def reading_stored(self, reading: Reading) -> None:
        self._reading[reading.mac] = _Last(reading_signature(reading), reading.ts)
