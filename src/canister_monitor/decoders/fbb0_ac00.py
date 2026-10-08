"""Decoder for the B-Qtech sensors' "fbb0_ac00" format (PLAN.md section 5.6).

Confirmed against the vendor app in Phase 0. Indices are into the full 17
manufacturer-data bytes (company ID bytes included):

    0-1   AC 00 header (bleak company ID 0x00AC)
    2     unknown, tracks temperature (battery voltage?) - kept in flags
    3     gauge pressure, uint8, ~0.4558 psi per count
    4     temperature, uint8, degC + 55
    5     status flags (00 normal, 01 no pressure, 02 just pressurized)
    6-10  unknown / counter / check byte
    11-16 sensor MAC, byte-reversed
"""

from canister_monitor.compensation import KPA_PER_PSI
from canister_monitor.decoders.base import Advert, Reading, full_mfr_bytes, has_service_uuid

SERVICE_UUID = "0000fbb0-0000-1000-8000-00805f9b34fb"
COMPANY_ID = 0x00AC
FULL_LENGTH = 17

PSI_PER_COUNT = 0.4558  # fitted to four app readings; bounds 0.4555-0.4561
KPA_PER_COUNT = PSI_PER_COUNT * KPA_PER_PSI
TEMP_OFFSET_C = 55


class Fbb0Ac00Decoder:
    name = "fbb0_ac00"
    pressure_reference = "gauge"

    def matches(self, adv: Advert) -> bool:
        payload = adv.manufacturer_data.get(COMPANY_ID)
        return (
            payload is not None
            and len(payload) == FULL_LENGTH - 2
            and has_service_uuid(adv, SERVICE_UUID)
        )

    def decode(self, adv: Advert, atmospheric_kpa: float) -> Reading | None:
        payload = adv.manufacturer_data.get(COMPANY_ID)
        if payload is None or len(payload) != FULL_LENGTH - 2:
            return None
        full = full_mfr_bytes(COMPANY_ID, payload)
        embedded_mac = ":".join(f"{b:02X}" for b in reversed(full[11:17]))
        return Reading(
            ts=adv.ts,
            mac=adv.mac,
            pressure_kpa_abs=full[3] * KPA_PER_COUNT + atmospheric_kpa,
            temp_c=float(full[4] - TEMP_OFFSET_C),
            flags={
                "status": full[5],
                "no_pressure": bool(full[5] & 0x01),
                "byte2": full[2],
                "mac_match": embedded_mac == adv.mac.upper(),
            },
            decoder=self.name,
        )
