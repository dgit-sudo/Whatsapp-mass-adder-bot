#!/usr/bin/env bash
set -uo pipefail

SERVICE_NAME="${SERVICE_NAME:-telegram-adder}"
DEFAULT_APP_DIR="/home/dhyanam2412/telegram-csv-adder"
APP_DIR="${APP_DIR:-$DEFAULT_APP_DIR}"

if [[ ! -d "$APP_DIR" ]]; then
  if [[ -d "$HOME/telegram-csv-adder" ]]; then
    APP_DIR="$HOME/telegram-csv-adder"
  elif [[ -d "$PWD" && -f "$PWD/bot.py" ]]; then
    APP_DIR="$PWD"
  fi
fi

as_root() {
  if command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    "$@"
  fi
}

echo "[1/7] Stopping service: ${SERVICE_NAME}"
as_root systemctl stop "${SERVICE_NAME}" || true

echo "[2/7] Entering app directory"
if [[ ! -d "$APP_DIR" ]]; then
  echo "ERROR: App directory not found: $APP_DIR"
  echo "Set APP_DIR and rerun, example: APP_DIR=/home/dhyanam2412/telegram-csv-adder bash deploy/fix_telethon_user_session.sh"
  exit 1
fi
cd "${APP_DIR}"

echo "[3/7] Activating venv"
if [[ ! -f ".venv/bin/activate" ]]; then
  echo "ERROR: venv not found at $APP_DIR/.venv"
  exit 1
fi
. .venv/bin/activate

if [[ ! -f ".env" ]]; then
  echo "ERROR: .env file not found in $APP_DIR"
  exit 1
fi

echo "[4/7] Checking current Telethon session type"
if ! SESSION_CHECK_OUTPUT=$(python - <<'PY'
import os
import asyncio
from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()

api_id = int(os.getenv("API_ID", "0"))
api_hash = os.getenv("API_HASH", "")
session_name = os.getenv("SESSION_NAME", "adder_session")

async def main():
  if not api_id or not api_hash:
    print("AUTHORIZED=0")
    print("CHECK_ERROR=Missing API_ID/API_HASH in .env")
    return

  client = TelegramClient(session_name, api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        print("AUTHORIZED=0")
        await client.disconnect()
        return
    me = await client.get_me()
    print("AUTHORIZED=1")
    print(f"IS_BOT={1 if getattr(me, 'bot', False) else 0}")
    print(f"USER_ID={getattr(me, 'id', '')}")
    print(f"USERNAME={getattr(me, 'username', '')}")
    await client.disconnect()

asyncio.run(main())
PY
); then
  echo "WARN: Session check failed. Will force re-login flow."
  SESSION_CHECK_OUTPUT="AUTHORIZED=0"
fi

echo "${SESSION_CHECK_OUTPUT}"

AUTHORIZED=$(echo "${SESSION_CHECK_OUTPUT}" | awk -F= '/^AUTHORIZED=/{print $2}' | tail -n1)
IS_BOT=$(echo "${SESSION_CHECK_OUTPUT}" | awk -F= '/^IS_BOT=/{print $2}' | tail -n1)

if [[ "${AUTHORIZED:-0}" == "1" && "${IS_BOT:-0}" == "0" ]]; then
  echo "[5/7] Session is already a user session. No re-login required."
else
  echo "[5/7] Session is bot/unauthorized. Removing old .session files..."
  rm -f ./*.session ./*.session-journal

  echo "[6/7] Starting interactive user login (phone + OTP + 2FA if enabled)"
  python init_session.py
fi

echo "[7/7] Starting service and printing recent logs"
as_root systemctl start "${SERVICE_NAME}"
as_root systemctl --no-pager --full status "${SERVICE_NAME}" || true
as_root journalctl -u "${SERVICE_NAME}" -n 80 --no-pager || true

echo "Done."
