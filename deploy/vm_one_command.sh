#!/usr/bin/env bash
set -euo pipefail

# Copy-paste friendly deploy command values.
# Replace SUPPORT_CHAT_ID if needed.
BOT_TOKEN="7662689866:AAFt9yjFaplP5AHVqvm_uLrxwF0Dyk7p9z4"
API_ID="33250681"
API_HASH="977bfe2d728b39dd507b165b24bba02e"
SUPPORT_CHAT_ID="123456789"

APP_USER="dhyanam2412"
APP_GROUP="$(id -gn "$APP_USER")"
APP_HOME="$(getent passwd "$APP_USER" | cut -d: -f6)"
APP_DIR="$APP_HOME/telegram-csv-adder"

cd "$APP_HOME"
if [ -d "$APP_DIR/.git" ]; then
  cd "$APP_DIR" && git pull origin main
else
  git clone https://github.com/dgit-sudo/Whatsapp-mass-adder-bot.git "$APP_DIR"
  cd "$APP_DIR"
fi

python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

[ -f .env ] || cp .env.example .env
touch .env

set_kv() {
  k="$1"
  v="$2"
  if grep -q "^${k}=" .env; then
    sed -i "s|^${k}=.*|${k}=${v}|g" .env
  else
    printf "%s=%s\n" "$k" "$v" >> .env
  fi
}

set_kv BOT_TOKEN "$BOT_TOKEN"
set_kv API_ID "$API_ID"
set_kv API_HASH "$API_HASH"
set_kv SUPPORT_CHAT_ID "$SUPPORT_CHAT_ID"
set_kv LOG_LEVEL INFO

if [ ! -f /etc/systemd/system/telegram-adder.service ]; then
  sudo tee /etc/systemd/system/telegram-adder.service > /dev/null <<EOF
[Unit]
Description=Telegram CSV Adder Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=dhyanam2412
Group=$APP_GROUP
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/bot.py
Restart=always
RestartSec=5
TimeoutStopSec=20
KillSignal=SIGINT
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
fi

sudo systemctl daemon-reload
sudo systemctl enable telegram-adder
sudo systemctl restart telegram-adder
sudo systemctl --no-pager --full status telegram-adder

echo
echo "If first deploy only, run once:"
echo "cd $APP_DIR && . .venv/bin/activate && python init_session.py && sudo systemctl restart telegram-adder"
