"""Load and validate ``config.toml`` into typed dataclasses (PLAN.md section 8)."""

import re
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, get_origin

from canister_monitor.decoders import REGISTRY

MAC_RE = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")
UNITS = ("psi", "kPa", "bar")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class ConfigError(ValueError):
    """Raised with a human-readable message when the config is invalid."""


@dataclass(frozen=True)
class SiteConfig:
    atmospheric_kpa: float = 101.325
    reference_temp_c: float = 20.0
    timezone_hint: str = "local"


@dataclass(frozen=True)
class CollectorConfig:
    db_path: Path = Path("data/canisters.db")
    decoders: list[str] = field(default_factory=lambda: ["fbb0_ac00"])
    raw_heartbeat_minutes: float = 10
    reading_heartbeat_minutes: float = 1
    scanner_restart_if_silent_minutes: float = 20
    log_level: str = "INFO"


@dataclass(frozen=True)
class WebConfig:
    host: str = "0.0.0.0"
    port: int = 5000
    stale_after_minutes: float = 90
    default_units: str = "psi"


@dataclass(frozen=True)
class SensorConfig:
    mac: str
    canister: str


@dataclass(frozen=True)
class Config:
    site: SiteConfig = field(default_factory=SiteConfig)
    collector: CollectorConfig = field(default_factory=CollectorConfig)
    web: WebConfig = field(default_factory=WebConfig)
    sensors: list[SensorConfig] = field(default_factory=list)

    def canister_for(self, mac: str) -> str | None:
        """Configured canister name for a MAC, or None if unassigned."""
        mac = mac.upper()
        return next((s.canister for s in self.sensors if s.mac == mac), None)


def _section(cls: type, raw: Any, where: str) -> dict[str, Any]:
    """Check a TOML table against a dataclass: known keys and basic types."""
    if not isinstance(raw, dict):
        raise ConfigError(f"[{where}] must be a table")
    known = {f.name: f for f in fields(cls)}
    unknown = sorted(set(raw) - set(known))
    if unknown:
        raise ConfigError(f"[{where}] unknown key(s): {', '.join(unknown)}")
    values = {}
    for key, value in raw.items():
        expected = known[key].type
        if isinstance(value, bool):  # bool is an int subclass; never valid here
            ok = False
        elif expected is float:
            ok = isinstance(value, (int, float))
            value = float(value) if ok else value
        elif expected is int:
            ok = isinstance(value, int)
        elif expected is Path:
            ok = isinstance(value, str)
            value = Path(value) if ok else value
        elif get_origin(expected) is list:
            ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
        else:
            ok = isinstance(value, str)
        if not ok:
            raise ConfigError(f"[{where}] {key} has the wrong type: {value!r}")
        values[key] = value
    return values


def _positive(where: str, **values: float) -> None:
    for key, value in values.items():
        if value <= 0:
            raise ConfigError(f"[{where}] {key} must be greater than 0, got {value}")


def parse_config(data: dict[str, Any], base_dir: Path | None = None) -> Config:
    """Build a validated Config from parsed TOML.

    A relative ``db_path`` is resolved against ``base_dir`` (the config file's
    directory) when given.
    """
    unknown = sorted(set(data) - {"site", "collector", "web", "sensors"})
    if unknown:
        raise ConfigError(f"unknown section(s): {', '.join(unknown)}")

    site = SiteConfig(**_section(SiteConfig, data.get("site", {}), "site"))
    _positive("site", atmospheric_kpa=site.atmospheric_kpa)

    collector = CollectorConfig(**_section(CollectorConfig, data.get("collector", {}), "collector"))
    if not collector.decoders:
        raise ConfigError("[collector] decoders must list at least one decoder")
    bad = [d for d in collector.decoders if d not in REGISTRY]
    if bad:
        raise ConfigError(f"[collector] unknown decoder(s) {bad}; available: {sorted(REGISTRY)}")
    _positive(
        "collector",
        raw_heartbeat_minutes=collector.raw_heartbeat_minutes,
        reading_heartbeat_minutes=collector.reading_heartbeat_minutes,
        scanner_restart_if_silent_minutes=collector.scanner_restart_if_silent_minutes,
    )
    if collector.log_level.upper() not in LOG_LEVELS:
        raise ConfigError(
            f"[collector] log_level must be one of {LOG_LEVELS}, got {collector.log_level!r}"
        )
    collector = replace(collector, log_level=collector.log_level.upper())
    if base_dir is not None and not collector.db_path.is_absolute():
        collector = replace(collector, db_path=base_dir / collector.db_path)

    web = WebConfig(**_section(WebConfig, data.get("web", {}), "web"))
    if not 1 <= web.port <= 65535:
        raise ConfigError(f"[web] port must be 1-65535, got {web.port}")
    if web.default_units not in UNITS:
        raise ConfigError(f"[web] default_units must be one of {UNITS}, got {web.default_units!r}")
    _positive("web", stale_after_minutes=web.stale_after_minutes)

    raw_sensors = data.get("sensors", [])
    if not isinstance(raw_sensors, list):
        raise ConfigError("sensors must be an array of tables: [[sensors]]")
    sensors = []
    for i, raw in enumerate(raw_sensors, start=1):
        where = f"sensors #{i}"
        values = _section(SensorConfig, raw, where)
        missing = {"mac", "canister"} - set(values)
        if missing:
            raise ConfigError(f"[{where}] missing key(s): {', '.join(sorted(missing))}")
        mac = values["mac"].upper()
        if not MAC_RE.match(mac):
            raise ConfigError(f"[{where}] mac must look like AA:BB:CC:DD:EE:FF, got {mac!r}")
        if any(s.mac == mac for s in sensors):
            raise ConfigError(f"[{where}] duplicate mac {mac}")
        sensors.append(SensorConfig(mac=mac, canister=values["canister"]))

    return Config(site=site, collector=collector, web=web, sensors=sensors)


def load_config(path: str | Path) -> Config:
    """Read and validate a TOML config file."""
    path = Path(path)
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(
            f"config file not found: {path} (copy config.example.toml to start)"
        ) from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}") from None
    try:
        return parse_config(data, base_dir=path.resolve().parent)
    except ConfigError as e:
        raise ConfigError(f"{path}: {e}") from None
