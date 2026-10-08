"""Decoder for the generic "BR" / 0x27a5 TPMS format (PLAN.md section 5.3).

A hypothesis from public forum captures; not used by the B-Qtech sensors.
Full manufacturer data is 7 bytes:

    0    status/flags
    1    battery, volts x 10
    2    temperature, degC (unsigned)
    3-4  absolute pressure, big-endian uint16, 0.1 psi
    5-6  checksum (algorithm unknown, not validated)
"""

from canister_monitor.compensation import KPA_PER_PSI
from canister_monitor.decoders.base import Advert, Reading, full_mfr_bytes, has_service_uuid

SERVICE_UUID = "000027a5-0000-1000-8000-00805f9b34fb"
LOCAL_NAME = "BR"
FULL_LENGTH = 7


def _find_full(adv: Advert) -> bytes | None:
    for cid, payload in adv.manufacturer_data.items():
        if len(payload) == FULL_LENGTH - 2 and 0 <= cid <= 0xFFFF:
            return full_mfr_bytes(cid, payload)
    return None


class Br27a5Decoder:
    name = "br_27a5"
    pressure_reference = "absolute"

    def matches(self, adv: Advert) -> bool:
        identified = adv.name == LOCAL_NAME or has_service_uuid(adv, SERVICE_UUID)
        return identified and _find_full(adv) is not None

    def decode(self, adv: Advert, atmospheric_kpa: float) -> Reading | None:
        full = _find_full(adv)
        if full is None:
            return None
        raw_pressure = int.from_bytes(full[3:5], "big")
        return Reading(
            ts=adv.ts,
            mac=adv.mac,
            pressure_kpa_abs=raw_pressure * 0.1 * KPA_PER_PSI,
            temp_c=float(full[2]),
            battery_v=full[1] / 10,
            flags={"status": full[0], "checksum": full[5:7].hex().upper()},
            decoder=self.name,
        )
