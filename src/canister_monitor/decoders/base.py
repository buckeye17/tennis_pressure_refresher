"""Shared types for advertisement decoders (PLAN.md section 5.1)."""

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Advert:
    """One BLE advertisement, independent of bleak."""

    ts: float  # UTC epoch seconds
    mac: str  # "AA:BB:CC:DD:EE:FF", uppercase
    name: str | None
    rssi: int | None
    manufacturer_data: dict[int, bytes]  # as bleak provides it: {company_id: payload}
    service_uuids: list[str]
    service_data: dict[str, bytes]


@dataclass(frozen=True)
class Reading:
    """A decoded sensor reading in canonical units."""

    ts: float
    mac: str
    pressure_kpa_abs: float  # canonical: absolute pressure in kPa
    temp_c: float | None
    battery_pct: float | None = None
    battery_v: float | None = None
    flags: dict = field(default_factory=dict)
    decoder: str = ""


class Decoder(Protocol):
    """Interface every decoder module implements.

    Neither method may raise on malformed input: ``matches`` returns False and
    ``decode`` returns None instead.
    """

    name: str
    pressure_reference: str  # "gauge" or "absolute", as reported by the sensor

    def matches(self, adv: Advert) -> bool: ...

    def decode(self, adv: Advert, atmospheric_kpa: float) -> Reading | None: ...


def full_mfr_bytes(company_id: int, payload: bytes) -> bytes:
    """Rebuild the full manufacturer-data bytes that bleak split apart.

    Bleak takes the first two bytes as a little-endian company ID. Cheap
    sensors often put data there, so decoders index into the full bytes.
    """
    return company_id.to_bytes(2, "little") + bytes(payload)


def has_service_uuid(adv: Advert, uuid: str) -> bool:
    """Case-insensitive check for a service UUID in the advert."""
    uuid = uuid.lower()
    return any(u.lower() == uuid for u in adv.service_uuids)
