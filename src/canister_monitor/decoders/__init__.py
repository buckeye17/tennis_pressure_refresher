"""Decoder registry. Decoders are enabled by name in config."""

import logging

from canister_monitor.decoders.base import Advert, Decoder, Reading
from canister_monitor.decoders.br_27a5 import Br27a5Decoder
from canister_monitor.decoders.fbb0_ac00 import Fbb0Ac00Decoder
from canister_monitor.decoders.tpms_0100 import Tpms0100Decoder

__all__ = [
    "REGISTRY",
    "Advert",
    "Decoder",
    "Reading",
    "decode_advert",
    "find_decoder",
    "get_decoders",
]

log = logging.getLogger(__name__)

REGISTRY: dict[str, Decoder] = {
    d.name: d for d in (Fbb0Ac00Decoder(), Br27a5Decoder(), Tpms0100Decoder())
}


def get_decoders(names: list[str]) -> list[Decoder]:
    """Look up decoders by name, preserving order. Raises ValueError on unknown names."""
    unknown = [n for n in names if n not in REGISTRY]
    if unknown:
        raise ValueError(f"unknown decoder(s) {unknown}; available: {sorted(REGISTRY)}")
    return [REGISTRY[n] for n in names]


def find_decoder(adv: Advert, decoders: list[Decoder]) -> Decoder | None:
    """Return the first decoder that claims this advert, or None."""
    for decoder in decoders:
        try:
            if decoder.matches(adv):
                return decoder
        except Exception:  # decoders must not raise; never let one kill the scan
            log.exception("decoder %s raised in matches() for %s", decoder.name, adv.mac)
    return None


def decode_advert(adv: Advert, decoders: list[Decoder], atmospheric_kpa: float) -> Reading | None:
    """Decode with the first matching decoder (first match wins)."""
    decoder = find_decoder(adv, decoders)
    if decoder is None:
        return None
    try:
        return decoder.decode(adv, atmospheric_kpa)
    except Exception:
        log.exception("decoder %s raised in decode() for %s", decoder.name, adv.mac)
        return None
