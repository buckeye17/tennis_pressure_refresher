# Canister Pressure Monitor

Watches the air pressure in tennis-ball re-pressurizing canisters. Bluetooth tire-pressure
sensors (B-Qtech BLE TPMS valve caps) sit on the canisters' valves; a Raspberry Pi listens to
their broadcasts, stores every reading in SQLite, and serves a dashboard on your home network:
current pressure per canister, history charts, temperature-compensated pressure (so day/night
swings don't hide the real trend), notes, and CSV export.

[PLAN.md](PLAN.md) is the full design, including the sensors' decoded data format (§5.6).

## Quick start on the Pi

You need a Raspberry Pi 3 B+ (or newer) running **Raspberry Pi OS Lite (64-bit), Bookworm or
newer**, with hostname `canisters`, Wi-Fi or Ethernet, and SSH enabled (set these in Raspberry
Pi Imager).

```bash
sudo apt update && sudo apt full-upgrade -y && sudo apt install -y git
git clone https://github.com/buckeye17/tennis_pressure_refresher.git
cd tennis_pressure_refresher
sudo deploy/install.sh
```

The installer puts everything in `/opt/canister-monitor`, creates `config.toml` there from
[config.example.toml](config.example.toml), and starts two services that also start at every
boot:

| Service | Does |
|---|---|
| `canister-collector` | Scans for the sensors and stores readings |
| `canister-web` | Serves the dashboard |

Open **http://canisters.local:5000** from any device on your network.

### Name your sensors

Each sensor appears on the dashboard as soon as it broadcasts, shown as "Unassigned sensor" with
its MAC address. To give it a canister name, edit the config and restart:

```bash
sudo nano /opt/canister-monitor/config.toml
```

```toml
[[sensors]]
mac = "30:94:A8:11:11:11"
canister = "Canister 1"
```

```bash
sudo systemctl restart canister-collector canister-web
```

Sensors only broadcast while under pressure, so screw one onto a pressurized valve to bring it to
life. To find a new sensor's MAC, watch for it with the scanner (stop with Ctrl+C):

```bash
/opt/canister-monitor/.venv/bin/python tools/scan.py --tpms-only
```

### Everyday commands

```bash
journalctl -u canister-collector -f              # live log: one line per stored reading
systemctl status canister-collector canister-web
curl http://localhost:5000/healthz               # age of the newest reading
```

### Updating

```bash
cd ~/tennis_pressure_refresher && git pull && sudo deploy/install.sh
```

Re-running the installer is safe: it keeps your `config.toml` and data.

## How the sensors behave

- They **only broadcast under pressure**. An unscrewed or empty sensor goes silent, and its card
  turns amber after `stale_after_minutes` (default 90).
- When steady they broadcast every ~5 minutes; right after a change, every few seconds.
- Pressure resolution is about **0.46 psi** and accuracy is typical of tire sensors
  (±1–3 psi), so watch trends rather than exact values.
- The vendor's phone app can stay installed; it doesn't stop the Pi from hearing the sensors.

## Troubleshooting

**No readings at all.** Check Bluetooth is up: `bluetoothctl show` should say `Powered: yes`.
If not: `sudo rfkill unblock bluetooth && sudo systemctl restart bluetooth`. Then check the
collector log for errors.

**Collector log shows D-Bus "not authorized" / permission errors.** The service runs as your
user with the `bluetooth` group added (`SupplementaryGroups=bluetooth`). Check the group exists
(`getent group bluetooth`) and re-run `sudo deploy/install.sh`. For running `tools/scan.py` by
hand, log out and back in once after installing so your shell picks up the group.

**Some updates missing.** The Pi 3's Wi-Fi and Bluetooth share one radio chip. If the phone app
shows updates the dashboard misses, try Ethernet instead of Wi-Fi, or move the Pi closer.

**Wrong timestamps right after boot.** The Pi has no clock battery. The collector waits for
network time (`systemd-time-wait-sync`); check `timedatectl` shows `System clock synchronized:
yes`.

**Dashboard shows "Offline — retrying".** The web service is down or unreachable:
`systemctl status canister-web`. If `canisters.local` doesn't resolve, use the Pi's IP address.

## Development (any computer, no Bluetooth needed)

```bash
python3.11 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

(On Windows use `.venv\Scripts\...`.)

The simulator feeds four fake canisters (one leaking) through the real pipeline, into a separate
`data/canisters-sim.db`:

```bash
cp config.example.toml config.toml
.venv/bin/python -m canister_monitor.collector --simulate --start-days-ago 3 --speed 2000
.venv/bin/python -m canister_monitor.web --simulate    # in another terminal
```

`--start-days-ago 3 --speed 2000` fast-forwards three days of history in about two minutes, then
continues live. Open http://localhost:5000.
