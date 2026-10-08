import pytest

from canister_monitor.compensation import (
    abs_kpa,
    bar_to_kpa,
    compensated_abs_kpa,
    compensated_gauge_kpa,
    gauge_kpa,
    kpa_to_bar,
    kpa_to_psi,
    psi_to_kpa,
)

ATM = 101.325


def test_unit_conversions():
    assert kpa_to_psi(6.894757) == pytest.approx(1.0)
    assert psi_to_kpa(30.0) == pytest.approx(206.84271)
    assert kpa_to_bar(250.0) == 2.5
    assert bar_to_kpa(2.5) == 250.0
    assert kpa_to_psi(psi_to_kpa(17.8)) == pytest.approx(17.8)


def test_gauge_absolute_round_trip():
    for gauge in (-10.0, 0.0, 122.6, 320.6):
        assert gauge_kpa(abs_kpa(gauge, ATM), ATM) == pytest.approx(gauge)
    assert abs_kpa(0.0, ATM) == ATM
    assert gauge_kpa(ATM, 95.0) == pytest.approx(6.325)


def test_identity_at_reference_temperature():
    assert compensated_abs_kpa(400.0, 20.0, 20.0) == pytest.approx(400.0)
    assert compensated_gauge_kpa(400.0, 20.0, ATM, 20.0) == pytest.approx(400.0 - ATM)


def test_hand_calculated_values():
    # 400 kPa abs at 30 C, normalized to 20 C: 400 * 293.15 / 303.15
    assert compensated_abs_kpa(400.0, 30.0, 20.0) == pytest.approx(386.80521, rel=1e-7)
    # 300 kPa abs at 0 C, normalized to 20 C: 300 * 293.15 / 273.15
    assert compensated_abs_kpa(300.0, 0.0, 20.0) == pytest.approx(321.96596, rel=1e-7)
    assert compensated_gauge_kpa(300.0, 0.0, ATM, 20.0) == pytest.approx(321.96596 - ATM)


def test_compensation_uses_absolute_pressure_not_gauge():
    # Warming a sealed canister from 13 C to 29 C: gauge rises more than proportionally,
    # but compensated gauge should be unchanged.
    gauge_cold = psi_to_kpa(17.8)
    abs_cold = abs_kpa(gauge_cold, ATM)
    abs_hot = abs_cold * (29 + 273.15) / (13 + 273.15)
    cold = compensated_gauge_kpa(abs_cold, 13.0, ATM, 20.0)
    hot = compensated_gauge_kpa(abs_hot, 29.0, ATM, 20.0)
    assert hot == pytest.approx(cold)


def test_missing_temperature_gives_none():
    assert compensated_abs_kpa(400.0, None, 20.0) is None
    assert compensated_gauge_kpa(400.0, None, ATM, 20.0) is None


def test_below_absolute_zero_rejected():
    with pytest.raises(ValueError):
        compensated_abs_kpa(400.0, -273.15, 20.0)
