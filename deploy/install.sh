#!/usr/bin/env bash
# Install or update the canister monitor on a Raspberry Pi (PLAN.md Phase 6).
#
#   cd ~/tennis_pressure_refresher && sudo deploy/install.sh [--user NAME] [--app-dir DIR]
#
# Safe to re-run: after `git pull`, run it again to install the new code and
# restart the services. It never overwrites an existing config.toml or data/.
#
# Layout:  APP_DIR/.venv        Python environment with the package installed
#          APP_DIR/config.toml  your settings (copied from config.example.toml once)
#          APP_DIR/data/        the SQLite database
set -euo pipefail

APP_DIR=/opt/canister-monitor
SERVICE_USER=${SUDO_USER:-}
REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
UNITS=(canister-collector canister-web)

usage() {
    awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --user) SERVICE_USER=${2:?--user needs a name}; shift 2 ;;
        --app-dir) APP_DIR=${2:?--app-dir needs a path}; shift 2 ;;
        -h|--help) usage 0 ;;
        *) echo "unknown option: $1" >&2; usage 2 ;;
    esac
done

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run with sudo: sudo $0 $*"
[[ -n $SERVICE_USER ]] || die "could not tell which user should run the services; pass --user NAME"
[[ $SERVICE_USER != root ]] || die "the services should not run as root; pass --user NAME"
id "$SERVICE_USER" >/dev/null 2>&1 || die "user '$SERVICE_USER' does not exist"
[[ -f $REPO_DIR/pyproject.toml ]] || die "run this from a clone of the repository ($REPO_DIR has no pyproject.toml)"
[[ $APP_DIR == /* ]] || die "--app-dir must be an absolute path"

say "Checking system packages"
missing=()
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null \
    || die "Python 3.11+ is required (Raspberry Pi OS Bookworm or newer)"
# Debian ships venv without ensurepip unless python3-venv is installed.
python3 -c 'import ensurepip' >/dev/null 2>&1 || missing+=(python3-venv)
command -v bluetoothctl >/dev/null 2>&1 || missing+=(bluez)
if [[ ${#missing[@]} -gt 0 ]]; then
    echo "installing: ${missing[*]}"
    apt-get update -qq
    apt-get install -y -qq "${missing[@]}"
fi
getent group bluetooth >/dev/null || die "the 'bluetooth' group is missing; is bluez installed?"

say "Granting $SERVICE_USER Bluetooth access (bluetooth group)"
# The service gets the group via SupplementaryGroups=; this is for running
# tools/scan.py by hand. Takes effect at the user's next login.
usermod -aG bluetooth "$SERVICE_USER"

say "Installing into $APP_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$APP_DIR" "$APP_DIR/data"
if [[ ! -x $APP_DIR/.venv/bin/python ]]; then
    sudo -u "$SERVICE_USER" python3 -m venv "$APP_DIR/.venv"
fi
# Install from a temporary copy so the build doesn't write into the clone as root.
build_dir=$(mktemp -d)
trap 'rm -rf "$build_dir"' EXIT
cp -r "$REPO_DIR/pyproject.toml" "$REPO_DIR/PLAN.md" "$REPO_DIR/src" "$build_dir/"
chown -R "$SERVICE_USER" "$build_dir"
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install --quiet --upgrade "$build_dir"
install -m 644 -o "$SERVICE_USER" -g "$SERVICE_USER" "$REPO_DIR/README.md" "$APP_DIR/README.md"

if [[ -f $APP_DIR/config.toml ]]; then
    echo "keeping existing $APP_DIR/config.toml"
else
    install -m 644 -o "$SERVICE_USER" -g "$SERVICE_USER" \
        "$REPO_DIR/config.example.toml" "$APP_DIR/config.toml"
    echo "created $APP_DIR/config.toml from config.example.toml - edit it to name your sensors"
fi
# Fail now, with a readable message, rather than in a restart loop.
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/python" -c \
    'import sys; from canister_monitor.config import load_config; load_config(sys.argv[1])' \
    "$APP_DIR/config.toml" || die "$APP_DIR/config.toml is invalid (see above)"

say "Installing systemd units"
for unit in "${UNITS[@]}"; do
    sed -e "s|@USER@|$SERVICE_USER|g" -e "s|@APP_DIR@|$APP_DIR|g" \
        "$REPO_DIR/deploy/$unit.service" >"/etc/systemd/system/$unit.service"
    chmod 644 "/etc/systemd/system/$unit.service"
done
# Provides time-sync.target only once NTP has set the clock (the Pi has no RTC).
if systemctl cat systemd-time-wait-sync.service >/dev/null 2>&1; then
    systemctl enable systemd-time-wait-sync.service
else
    echo "warning: systemd-time-wait-sync.service not found (not using systemd-timesyncd?);" >&2
    echo "         readings taken right after boot may have wrong timestamps until NTP syncs" >&2
fi
systemctl daemon-reload
systemctl enable "${UNITS[@]/%/.service}"
systemctl restart "${UNITS[@]/%/.service}"

say "Done"
sleep 3
systemctl --no-pager --lines=0 status "${UNITS[@]/%/.service}" || true
port=$("$APP_DIR/.venv/bin/python" -c \
    'import sys; from canister_monitor.config import load_config; print(load_config(sys.argv[1]).web.port)' \
    "$APP_DIR/config.toml")
cat <<EOF

Dashboard:  http://$(hostname).local:$port
Logs:       journalctl -u canister-collector -f
Settings:   $APP_DIR/config.toml  (then: sudo systemctl restart ${UNITS[*]})
Update:     git pull && sudo deploy/install.sh
EOF
