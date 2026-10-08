from pathlib import Path

import pytest

from canister_monitor.config import Config, ConfigError, load_config, parse_config

REPO = Path(__file__).parent.parent


def test_example_config_loads():
    cfg = load_config(REPO / "config.example.toml")
    assert cfg.site.atmospheric_kpa == 101.325
    assert cfg.collector.decoders == ["fbb0_ac00"]
    assert cfg.collector.db_path == REPO.resolve() / "data" / "canisters.db"
    assert cfg.web.port == 5000
    assert cfg.canister_for("30:94:a8:11:11:11") == "Canister 1"
    assert cfg.canister_for("AA:BB:CC:DD:EE:FF") is None


def test_empty_config_uses_defaults():
    cfg = parse_config({})
    assert cfg == Config()
    assert cfg.collector.db_path == Path("data/canisters.db")
    assert cfg.web.default_units == "psi"


def test_ints_accepted_for_floats():
    cfg = parse_config({"site": {"atmospheric_kpa": 101}})
    assert cfg.site.atmospheric_kpa == 101.0
    assert isinstance(cfg.site.atmospheric_kpa, float)


def test_mac_normalized_to_uppercase():
    cfg = parse_config({"sensors": [{"mac": "30:94:a8:11:11:11", "canister": "A"}]})
    assert cfg.sensors[0].mac == "30:94:A8:11:11:11"


def test_missing_file_message(tmp_path):
    with pytest.raises(ConfigError, match="config.example.toml"):
        load_config(tmp_path / "config.toml")


def test_invalid_toml_message(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[site\n")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(path)


@pytest.mark.parametrize(
    "data, message",
    [
        ({"sitee": {}}, "unknown section"),
        ({"site": {"atmospheric_kpa": "high"}}, "atmospheric_kpa has the wrong type"),
        ({"site": {"atmospheric_kpa": 0}}, "greater than 0"),
        ({"site": {"altitude": 200}}, "unknown key"),
        ({"collector": {"decoders": ["fbb0_ac00", "nope"]}}, "unknown decoder"),
        ({"collector": {"decoders": []}}, "at least one decoder"),
        ({"collector": {"reading_heartbeat_minutes": -1}}, "greater than 0"),
        ({"collector": {"db_path": 5}}, "db_path has the wrong type"),
        ({"web": {"port": 70000}}, "port"),
        ({"web": {"port": True}}, "port has the wrong type"),
        ({"web": {"default_units": "atm"}}, "default_units"),
        ({"sensors": {"mac": "x"}}, "array of tables"),
        ({"sensors": [{"mac": "30:94:A8:11:11:11"}]}, "missing key"),
        ({"sensors": [{"mac": "30-94-A8-11-11-11", "canister": "A"}]}, "AA:BB:CC:DD:EE:FF"),
        (
            {
                "sensors": [
                    {"mac": "30:94:A8:11:11:11", "canister": "A"},
                    {"mac": "30:94:a8:11:11:11", "canister": "B"},
                ]
            },
            "duplicate mac",
        ),
    ],
)
def test_validation_errors(data, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(data)


def test_errors_name_the_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[web]\ndefault_units = "atm"\n')
    with pytest.raises(ConfigError, match="config.toml"):
        load_config(path)
