#!/usr/bin/env bash
# Headless install for Ubuntu (Azure VM). Does not start the bot.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
echo "==> repo $ROOT"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "Need python3. On Ubuntu: sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip"
  exit 1
fi

"$PYTHON" -m venv venv
# shellcheck disable=SC1091
source venv/bin/activate
pip install -U pip
pip install -r requirements-headless.txt

if [ ! -f config.yaml ]; then
  cp example_config.yaml config.yaml
  echo "==> wrote config.yaml from example_config.yaml"
fi

mkdir -p logs data/recordings

echo
echo "Headless install OK. Next:"
echo "  1. Edit config.yaml  (recording.dir=/data/mm_bot/recordings if you mounted a data disk)"
echo "  2. See deploy/azure.md for systemd and disk mount"
echo "  3. Do not use python main.py on the VM (needs Qt)"
echo "  Start later with:  source venv/bin/activate && python run.py"
echo "  Or systemd after editing deploy/mm-bot.service"
