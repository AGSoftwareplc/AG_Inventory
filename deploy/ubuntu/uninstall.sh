#!/bin/bash
# Remove the AG Inventory service from this server. Touches nothing else.
# The folder /opt/ag-inventory (with your data) is KEPT unless you add --delete-data.
set -euo pipefail
PORT="${AGINV_PORT:-4080}"
[ "$(id -u)" -eq 0 ] || { echo "please run with sudo"; exit 1; }
systemctl disable --now ag-inventory 2>/dev/null || true
rm -f /etc/systemd/system/ag-inventory.service
systemctl daemon-reload
if command -v ufw >/dev/null && ufw status | grep -q "${PORT}/tcp"; then
    ufw delete allow "${PORT}/tcp" >/dev/null || true
fi
if [ "${1:-}" = "--delete-data" ]; then
    rm -rf /opt/ag-inventory
    userdel aginv 2>/dev/null || true
    echo "AG Inventory and its data were removed."
else
    echo "AG Inventory service removed. Your data is still in /opt/ag-inventory/data."
fi
