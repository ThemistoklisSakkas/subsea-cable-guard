#!/usr/bin/env bash
# One-time setup of the AIS collector on a Linux VM (tested target: Ubuntu 24.04,
# Oracle Cloud Always Free). Run from the repo root AFTER creating .env:
#     bash scripts/setup_vm.sh
# Safe to re-run: every step checks whether it is already done.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
[ -f .env ] || { echo "Missing .env (copy .env.example and add AISSTREAM_API_KEY)"; exit 1; }
chmod 600 .env

# 1. uv (Python package manager) - installs into ~/.local/bin
if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"

# 2. Python environment (core dependencies only: the collector needs no geo stack)
[ -d .venv ] || uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -e ".[dev]"

# 3. Swap on small machines: nightly compaction loads one day into memory,
#    and the 1 GB micro shape could otherwise run out of RAM.
mem_kb=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
if [ "$mem_kb" -lt 2000000 ] && ! swapon --show | grep -q /swapfile; then
    sudo fallocate -l 2G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
    sudo swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
fi

# 4. Tests must pass before we trust it to run unattended
.venv/bin/python -m pytest -q

# 5. systemd service: starts at boot, restarts 30 s after any crash
sed -e "s#__USER__#$USER#g" -e "s#__DIR__#$REPO#g" scripts/cableguard-collector.service \
    | sudo tee /etc/systemd/system/cableguard-collector.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now cableguard-collector
sleep 5
systemctl --no-pager status cableguard-collector | head -12
echo
echo "Done. Live log:  journalctl -u cableguard-collector -f"
echo "Summary:         .venv/bin/python -m cableguard status"
