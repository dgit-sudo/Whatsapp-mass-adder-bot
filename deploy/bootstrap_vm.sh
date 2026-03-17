#!/usr/bin/env bash
set -euo pipefail

# Run as root on Ubuntu VM.
# Usage:
#   sudo bash deploy/bootstrap_vm.sh

APP_DIR="/opt/telegram-csv-adder"
APP_USER="botuser"

apt-get update
apt-get install -y python3 python3-venv python3-pip git

if ! id -u "$APP_USER" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
fi

mkdir -p "$APP_DIR"
chown -R "$APP_USER":"$APP_USER" "$APP_DIR"

echo "Bootstrap done. Copy your project into $APP_DIR and continue service setup."
