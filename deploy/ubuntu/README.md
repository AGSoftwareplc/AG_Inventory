# AG Inventory on an Ubuntu server

Runs AG Inventory as its own service on port **4080**, next to other apps, without touching them.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/AGSoftwareplc/AG_Inventory/main/deploy/ubuntu/install.sh -o ag-install.sh
sudo bash ag-install.sh --check   # looks only, changes nothing
sudo bash ag-install.sh           # installs and starts it
```

Then open `http://<server-ip>:4080` and log in as `admin` / `admin123` (change it straight away).

## What it creates (and nothing else)

| Item | Where |
|---|---|
| System user (no login) | `aginv` |
| Code, venv, data, backups | `/opt/ag-inventory` (data in `data/`, backups in `backups/`) |
| Service | `ag-inventory.service` (starts at boot, restarts if it crashes) |
| Firewall | `allow 4080/tcp`, only if ufw is already on |

It stops with "Nothing was changed" if port 4080 is taken, or if `/opt/ag-inventory` or a user `aginv`
already exist for something else. It never restarts other services, edits nginx/apache, upgrades other
packages, or switches a firewall on. To use another port: `sudo AGINV_PORT=4081 bash ag-install.sh`.

## Everyday commands

```bash
sudo systemctl status ag-inventory      # is it running?
sudo journalctl -u ag-inventory -f      # live log
sudo bash /opt/ag-inventory/deploy/ubuntu/install.sh    # update to the latest version (data kept)
sudo bash /opt/ag-inventory/deploy/ubuntu/uninstall.sh  # remove the service (data kept)
```

Back up by copying `/opt/ag-inventory/data/` (the app also saves copies in `backups/` before upgrades).
