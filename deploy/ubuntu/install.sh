#!/bin/bash
# Mizan - install or update on an Ubuntu server, side by side with other apps.
#
#   sudo bash install.sh            install, or update to the latest version
#   sudo bash install.sh --check    only look (changes nothing) and report what it would do
#
# What it does, and nothing more:
#   * its own system user  "aginv"   (no login, no sudo)
#   * its own folder       /opt/ag-inventory   (code + data/ + backups/)
#   * its own Python venv  /opt/ag-inventory/.venv
#   * its own service      ag-inventory.service, listening on port 4080
#   * one firewall rule    allow 4080/tcp   (only if ufw is already switched on)
# It never stops, restarts or edits other services, nginx/apache sites, or other firewall rules,
# and it does not switch a firewall on.
set -euo pipefail
umask 022

# If run from inside the install folder, run a copy instead, so updating the code can't change this script mid-run.
case "$(readlink -f "$0")" in
    /opt/ag-inventory/*) tmp="$(mktemp)"; cp "$0" "$tmp"; exec bash "$tmp" "$@" ;;
esac

PORT="${AGINV_PORT:-4080}"
APP_USER="aginv"
APP_DIR="/opt/ag-inventory"
SERVICE="ag-inventory"
UNIT="/etc/systemd/system/${SERVICE}.service"
REPO="${AGINV_REPO:-https://github.com/AGSoftwareplc/AG_Inventory.git}"
BRANCH="${AGINV_BRANCH:-main}"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*"; }
die()  { printf '\033[1;31mSTOP:\033[0m %s\n' "$*"; exit 1; }

[ "$(id -u)" -eq 0 ] || die "please run with sudo:  sudo bash $0 $*"
command -v systemctl >/dev/null || die "systemd was not found; this script is for Ubuntu with systemd."

# ---------------------------------------------------------------- 1. look before touching anything
say "Checking the server (read only)"
. /etc/os-release 2>/dev/null && echo "    System: ${PRETTY_NAME:-unknown}"

UPDATE=0
if systemctl list-unit-files "${SERVICE}.service" --no-legend 2>/dev/null | grep -q "^${SERVICE}.service"; then
    UPDATE=1
    echo "    Mizan is already installed here: this run will UPDATE it (data is kept)."
fi
if [ "$UPDATE" -eq 0 ] && [ -d "$APP_DIR/.git" ] && id "$APP_USER" >/dev/null 2>&1; then
    UPDATE=1   # was uninstalled earlier with its data kept: set the service up again on the same data
    echo "    An earlier Mizan folder was found: it will be reused (data is kept)."
fi

# Who is on the port?
PORT_OWNER="$(ss -ltnpH "sport = :${PORT}" 2>/dev/null || true)"
if [ -n "$PORT_OWNER" ]; then
    OWN_PID="$(systemctl show -p MainPID --value "$SERVICE" 2>/dev/null || echo 0)"
    if [ "$UPDATE" -eq 1 ] && [ "${OWN_PID:-0}" != 0 ] && echo "$PORT_OWNER" | grep -q "pid=${OWN_PID},"; then
        echo "    Port ${PORT}: used by Mizan itself (fine)."
    else
        echo "$PORT_OWNER"
        die "port ${PORT} is already used by another program (shown above). Nothing was changed. Pick another port: sudo AGINV_PORT=4081 bash $0"
    fi
else
    echo "    Port ${PORT}: free."
fi

if [ "$UPDATE" -eq 0 ] && [ -e "$APP_DIR" ]; then
    die "${APP_DIR} already exists but is not a Mizan install. Nothing was changed."
fi
if [ "$UPDATE" -eq 0 ] && id "$APP_USER" >/dev/null 2>&1; then
    die "a user called ${APP_USER} already exists. Nothing was changed."
fi

echo "    Other apps listening right now (left untouched):"
ss -ltnpH 2>/dev/null | awk '{print "      " $4 "  " $6}' | sort -u || true
for d in /etc/nginx/sites-enabled /etc/apache2/sites-enabled; do
    [ -d "$d" ] && echo "    $d: $(ls "$d" | tr '\n' ' ') (left untouched)"
done
UFW_ACTIVE=0
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
    UFW_ACTIVE=1; echo "    Firewall: ufw is ON, a rule for ${PORT}/tcp will be added."
else
    echo "    Firewall: ufw is off or not installed, nothing will be changed there."
fi

PKGS=""
command -v python3 >/dev/null || PKGS="python3"
python3 -c 'import ensurepip, venv' 2>/dev/null || PKGS="$PKGS python3-venv"
command -v git >/dev/null || PKGS="$PKGS git"
if command -v python3 >/dev/null; then
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
        || die "Python 3.10 or newer is needed (found $(python3 -V)). Nothing was changed."
fi
[ -n "$PKGS" ] && echo "    Packages to install:${PKGS}"

if [ "$CHECK_ONLY" -eq 1 ]; then
    say "Check finished. Nothing was changed. Run again without --check to install."
    exit 0
fi

# ---------------------------------------------------------------- 2. install
if [ -n "$PKGS" ]; then
    say "Installing:${PKGS} (no other packages are upgraded)"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-upgrade $PKGS
fi

if [ "$UPDATE" -eq 0 ]; then
    say "Creating system user ${APP_USER} (no login shell)"
    useradd --system --home-dir "$APP_DIR" --no-create-home --shell /usr/sbin/nologin "$APP_USER"
    say "Downloading Mizan to ${APP_DIR}"
    git clone --quiet --branch "$BRANCH" "$REPO" "$APP_DIR"
    chown -R "$APP_USER:$APP_USER" "$APP_DIR"
else
    say "Updating the code in ${APP_DIR} (data/ and backups/ are kept)"
    sudo -u "$APP_USER" git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
    sudo -u "$APP_USER" git -C "$APP_DIR" merge --ff-only --quiet FETCH_HEAD
fi

chmod 750 "$APP_DIR"   # the shop data and secret key are readable only by aginv (and root)
say "Setting up the Python environment"
[ -x "$APP_DIR/.venv/bin/python" ] || sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" -m pip install --quiet --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" -m pip install --quiet -r "$APP_DIR/requirements.txt"

say "Writing ${UNIT}"
cat > "$UNIT" <<UNITEOF
[Unit]
Description=Mizan (shop inventory web app) on port ${PORT}
After=network-online.target
Wants=network-online.target

[Service]
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_DIR}
Environment=PORT=${PORT}
Environment=AGINV_NO_BROWSER=1
Environment=PYTHONUNBUFFERED=1
UMask=0027
ExecStart=${APP_DIR}/.venv/bin/python serve.py
Restart=on-failure
RestartSec=5
# keep it in its own box: it can only write to its own folder
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=${APP_DIR}

[Install]
WantedBy=multi-user.target
UNITEOF

systemctl daemon-reload
systemctl enable --quiet "$SERVICE"
# restarts only Mizan's own service
systemctl restart "$SERVICE"

if [ "$UFW_ACTIVE" -eq 1 ]; then
    say "Opening port ${PORT}/tcp in ufw (other rules unchanged)"
    ufw allow "${PORT}/tcp" comment 'AG Inventory' >/dev/null
fi

# ---------------------------------------------------------------- 3. confirm it answers
say "Waiting for Mizan to answer on port ${PORT}"
ok=0
for _ in $(seq 1 30); do
    if python3 -c "import urllib.request as u; u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:${PORT}/login', timeout=2)" 2>/dev/null; then
        ok=1; break
    fi
    sleep 1
done
if [ "$ok" -ne 1 ]; then
    journalctl -u "$SERVICE" -n 30 --no-pager || true
    die "Mizan did not start (log above). Other apps were not touched."
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
say "Done. Mizan is running."
echo "    Open:        http://${IP:-<server-ip>}:${PORT}"
echo "    First login: admin / admin123  (change it straight away)"
echo "    Status:      sudo systemctl status ${SERVICE}"
echo "    Logs:        sudo journalctl -u ${SERVICE} -f"
echo "    Update:      sudo bash ${APP_DIR}/deploy/ubuntu/install.sh"
echo "    Data:        ${APP_DIR}/data   (backups in ${APP_DIR}/backups)"
