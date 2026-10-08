"""Unit conversions and temperature compensation (PLAN.md section 6).

Storage is always absolute kPa and degC. Compensation normalizes a reading
to ``reference_temp_c`` with the ideal gas law, which requires absolute
pressure and Kelvin.
"""

KPA_PER_PSI = 6.894757
KPA_PER_BAR = 100.0
ZERO_C_IN_K = 273.15


def kpa_to_psi(kpa: float) -> float:
    return kpa / KPA_PER_PSI


def psi_to_kpa(psi: float) -> float:
    return psi * KPA_PER_PSI


def kpa_to_bar(kpa: float) -> float:
    return kpa / KPA_PER_BAR


def bar_to_kpa(bar: float) -> float:
    return bar * KPA_PER_BAR


def gauge_kpa(abs_kpa: float, atmospheric_kpa: float) -> float:
    """Absolute to gauge pressure."""
    return abs_kpa - atmospheric_kpa


def abs_kpa(gauge: float, atmospheric_kpa: float) -> float:
    """Gauge to absolute pressure."""
    return gauge + atmospheric_kpa


def compensated_abs_kpa(
    abs_kpa: float, temp_c: float | None, reference_temp_c: float
) -> float | None:
    """Absolute pressure the gas would have at ``reference_temp_c``.

    Returns None when the temperature is unknown.
    """
    if temp_c is None:
        return None
    temp_k = temp_c + ZERO_C_IN_K
    if temp_k <= 0:
        raise ValueError(f"temperature {temp_c} degC is at or below absolute zero")
    return abs_kpa * (reference_temp_c + ZERO_C_IN_K) / temp_k


def compensated_gauge_kpa(
    abs_kpa: float, temp_c: float | None, atmospheric_kpa: float, reference_temp_c: float
) -> float | None:
    """Gauge pressure normalized to ``reference_temp_c``; None if temperature unknown."""
    comp = compensated_abs_kpa(abs_kpa, temp_c, reference_temp_c)
    return None if comp is None else comp - atmospheric_kpa
