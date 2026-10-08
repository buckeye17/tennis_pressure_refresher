import json
import random
from pathlib import Path

import pytest

from canister_monitor.compensation import gauge_kpa, kpa_to_psi
from canister_monitor.decoders import (
    REGISTRY,
    Advert,
    decode_advert,
    find_decoder,
    get_decoders,
)
from canister_monitor.decoders.br_27a5 import Br27a5Decoder
from canister_monitor.decoders.fbb0_ac00 import Fbb0Ac00Decoder
from canister_monitor.decoders.tpms_0100 import Tpms0100Decoder

FIXTURES = Path(__file__).parent / "fixtures"
ATM = 101.325
FBB0_UUID = "0000fbb0-0000-1000-8000-00805f9b34fb"
BR_UUID = "000027a5-0000-1000-8000-00805f9b34fb"


def advert(mfr=None, uuids=(), name=None, mac="AA:BB:CC:DD:EE:FF") -> Advert:
    return Advert(
        ts=1_700_000_000.0,
        mac=mac,
        name=name,
        rssi=-60,
        manufacturer_data=mfr or {},
        service_uuids=list(uuids),
        service_data={},
    )


def split_full(full_hex: str) -> dict[int, bytes]:
    """Split full manufacturer bytes the way bleak does."""
    full = bytes.fromhex(full_hex)
    return {int.from_bytes(full[:2], "little"): full[2:]}


def load_captured() -> list[dict]:
    path = FIXTURES / "captured" / "fbb0_ac00_sensor1.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --- fbb0_ac00 (confirmed B-Qtech format) -------------------------------------


@pytest.mark.parametrize("cap", load_captured(), ids=lambda c: c["time"][11:19])
def test_fbb0_captured_fixtures_match_app(cap):
    adv = advert(
        mfr={int(k): bytes.fromhex(v) for k, v in cap["manufacturer_data"].items()},
        uuids=cap["service_uuids"],
        mac=cap["mac"],
    )
    decoder = Fbb0Ac00Decoder()
    assert decoder.matches(adv)
    reading = decoder.decode(adv, ATM)
    assert reading is not None
    psi = kpa_to_psi(gauge_kpa(reading.pressure_kpa_abs, ATM))
    assert psi == pytest.approx(cap["app_psi"], abs=0.05)
    if cap["app_temp_c"] is not None:
        assert reading.temp_c == cap["app_temp_c"]
    assert reading.flags["mac_match"] is True
    assert reading.decoder == "fbb0_ac00"
    assert reading.mac == cap["mac"]


def test_fbb0_flags():
    by_time = {c["time"][11:19]: c for c in load_captured()}
    decoder = Fbb0Ac00Decoder()

    def flags(t):
        cap = by_time[t]
        mfr = {int(k): bytes.fromhex(v) for k, v in cap["manufacturer_data"].items()}
        return decoder.decode(advert(mfr, cap["service_uuids"], mac=cap["mac"]), ATM).flags

    assert flags("03:20:41")["status"] == 0x00
    assert flags("03:20:41")["no_pressure"] is False
    assert flags("03:38:41")["status"] == 0x01
    assert flags("03:38:41")["no_pressure"] is True
    assert flags("03:54:30")["status"] == 0x02
    assert flags("03:54:30")["byte2"] == 0x9C


def test_fbb0_mac_mismatch_is_flagged_not_rejected():
    mfr = split_full("AC008F2744000A3F00B628111111A89430")
    reading = Fbb0Ac00Decoder().decode(advert(mfr, [FBB0_UUID], mac="11:22:33:44:55:66"), ATM)
    assert reading is not None
    assert reading.flags["mac_match"] is False


def test_fbb0_requires_service_uuid_and_length():
    decoder = Fbb0Ac00Decoder()
    good = split_full("AC008F2744000A3F00B628111111A89430")
    assert not decoder.matches(advert(good, uuids=[]))
    assert not decoder.matches(advert({0x00AC: good[0x00AC][:-1]}, [FBB0_UUID]))
    assert decoder.decode(advert({0x00AC: good[0x00AC][:-1]}, [FBB0_UUID]), ATM) is None
    assert decoder.matches(advert(good, [FBB0_UUID.upper()]))


# --- br_27a5 (hypothesis, forum fixtures) -------------------------------------


@pytest.mark.parametrize(
    "case", json.loads((FIXTURES / "br_27a5.json").read_text())["cases"], ids=lambda c: c["app"]
)
def test_br_27a5_forum_fixtures(case):
    adv = advert(split_full(case["full_hex"]), uuids=[BR_UUID], name="BR")
    decoder = Br27a5Decoder()
    assert decoder.matches(adv)
    reading = decoder.decode(adv, ATM)
    assert kpa_to_psi(reading.pressure_kpa_abs) == pytest.approx(case["expected_psi_abs"], abs=0.1)
    assert reading.temp_c == case["expected_temp_c"]
    assert reading.battery_v == case["expected_battery_v"]
    assert reading.flags["status"] == case["expected_status"]


def test_br_27a5_identified_by_name_or_uuid():
    mfr = split_full("281E11014D8511")
    decoder = Br27a5Decoder()
    assert decoder.matches(advert(mfr, name="BR"))
    assert decoder.matches(advert(mfr, uuids=[BR_UUID]))
    assert not decoder.matches(advert(mfr))


# --- tpms_0100 (hypothesis, synthetic fixtures) -------------------------------


@pytest.mark.parametrize(
    "case", json.loads((FIXTURES / "tpms_0100.json").read_text())["cases"], ids=lambda c: c["note"]
)
def test_tpms_0100_synthetic_fixtures(case):
    # TODO: replace with captured fixture
    adv = advert({0x0100: bytes.fromhex(case["payload_hex"])}, name="TPMS1_10CA8F")
    decoder = Tpms0100Decoder()
    assert decoder.matches(adv)
    reading = decoder.decode(adv, ATM)
    assert gauge_kpa(reading.pressure_kpa_abs, ATM) == pytest.approx(case["expected_gauge_kpa"])
    assert reading.temp_c == pytest.approx(case["expected_temp_c"])
    assert reading.battery_pct == case["expected_battery_pct"]
    assert reading.flags["flat"] is case["expected_flat"]


# --- malformed input: no decoder may raise -------------------------------------

MALFORMED = [
    {},
    {0x00AC: b""},
    {0x00AC: b"\x00" * 14},
    {0x00AC: b"\xff" * 16},
    {0x0100: b"\x00" * 15},
    {0x0100: b"\xff" * 17},
    {0x1E28: b"\x11\x01"},
    {0x1E28: b""},
    {0xFFFF: b"\xff" * 40},
]


@pytest.mark.parametrize("name", sorted(REGISTRY))
@pytest.mark.parametrize("mfr", MALFORMED)
def test_malformed_payloads_return_none(name, mfr):
    decoder = REGISTRY[name]
    adv = advert(mfr, uuids=[FBB0_UUID, BR_UUID], name="BR")
    assert decoder.matches(adv) is False
    assert decoder.decode(adv, ATM) is None


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_random_garbage_never_raises(name):
    decoder = REGISTRY[name]
    rng = random.Random(1234)
    for _ in range(2000):
        cid = rng.choice([0x00AC, 0x0100, rng.randrange(0x10000)])
        mfr = {cid: rng.randbytes(rng.randrange(0, 32))}
        adv = advert(mfr, uuids=[FBB0_UUID, BR_UUID], name=rng.choice([None, "BR", "TPMS"]))
        if decoder.matches(adv):
            assert decoder.decode(adv, ATM) is not None
        else:
            decoder.decode(adv, ATM)  # must not raise either way


# --- registry ------------------------------------------------------------------


def test_registry_names_match_decoders():
    assert set(REGISTRY) == {"fbb0_ac00", "br_27a5", "tpms_0100"}
    for name, decoder in REGISTRY.items():
        assert decoder.name == name
        assert decoder.pressure_reference in ("gauge", "absolute")


def test_get_decoders_preserves_order_and_rejects_unknown():
    assert [d.name for d in get_decoders(["tpms_0100", "fbb0_ac00"])] == [
        "tpms_0100",
        "fbb0_ac00",
    ]
    with pytest.raises(ValueError, match="nope"):
        get_decoders(["fbb0_ac00", "nope"])


def test_first_match_wins_and_unmatched_returns_none():
    adv = advert(split_full("AC008F2744000A3F00B628111111A89430"), [FBB0_UUID])
    decoders = get_decoders(["br_27a5", "fbb0_ac00"])
    assert find_decoder(adv, decoders).name == "fbb0_ac00"
    assert decode_advert(adv, decoders, ATM).decoder == "fbb0_ac00"
    assert decode_advert(advert({0x004C: b"\x02\x15"}), decoders, ATM) is None


def test_decode_advert_contains_a_raising_decoder():
    class Broken:
        name = "broken"
        pressure_reference = "gauge"

        def matches(self, adv):
            raise RuntimeError("boom")

        def decode(self, adv, atmospheric_kpa):
            raise RuntimeError("boom")

    adv = advert(split_full("AC008F2744000A3F00B628111111A89430"), [FBB0_UUID])
    assert decode_advert(adv, [Broken(), REGISTRY["fbb0_ac00"]], ATM).decoder == "fbb0_ac00"
