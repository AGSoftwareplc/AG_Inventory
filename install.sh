#!/bin/sh
# One-time installation on Linux / macOS. Needs Python 3.10+ (sudo apt install python3 python3-venv).
set -e
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
echo "Installation finished. Run ./start.sh to start AG Inventory."
