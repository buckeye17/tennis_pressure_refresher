"""Decoder for the "TPMS" / company 0x0100 format (PLAN.md section 5.4).

A hypothesis from forum posts; not used by the B-Qtech sensors and not yet
verified against real hardware. Bleak payload (after the company ID) is 16
bytes:

    0-5    sensor address (byte 0 often the position: 0x80, 0x81, ...)
    6-9    gauge pressure, little-endian uint32, Pa
    10-13  temperature, little-endian int32, degC x 100
    14     battery, percent
    15     flat/alarm flag
"""

from canister_monitor.decoders.base import Advert, Reading

COMPANY_ID = 0x0100
PAYLOAD_LENGTH = 16


class Tpms0100Decoder:
    name = "tpms_0100"
    pressure_reference = "gauge"

    def matches(self, adv: Advert) -> bool:
        payload = adv.manufacturer_data.get(COMPANY_ID)
        return payload is not None and len(payload) == PAYLOAD_LENGTH

    def decode(self, adv: Advert, atmospheric_kpa: float) -> Reading | None:
        payload = adv.manufacturer_data.get(COMPANY_ID)
        if payload is None or len(payload) != PAYLOAD_LENGTH:
            return None
        pressure_pa = int.from_bytes(payload[6:10], "little")
        temp_centi_c = int.from_bytes(payload[10:14], "little", signed=True)
        return Reading(
            ts=adv.ts,
            mac=adv.mac,
            pressure_kpa_abs=pressure_pa / 1000 + atmospheric_kpa,
            temp_c=temp_centi_c / 100,
            battery_pct=float(payload[14]),
            flags={"flat": bool(payload[15]), "sensor_address": payload[0:6].hex().upper()},
            decoder=self.name,
        )
