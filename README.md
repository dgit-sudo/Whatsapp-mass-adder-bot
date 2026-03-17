# Telegram CSV Group Adder Bot

This bot now includes a persistent admin system.

- First user who starts the bot becomes superadmin.
- Superadmin can open Admin Panel.
- Superadmin can view all uploaded CSV history.
- Superadmin can view all users who have used the bot.
- Superadmin can ban and unban users.
- Every new user starts with 0 credits.
- Superadmin can set user credits from Admin Panel.

## Main Features

- Menu UI: Upload CSV, Contact Support, Admin Panel (superadmin only)
- CSV upload for numeric Telegram user IDs
- Invite safety controls (cooldown, random delay jitter, flood handling, failure stop)
- CSV archive storage on disk + metadata in SQLite
- User registry with superadmin and banned flags
- Credit wallet per user (default 0) with superadmin controls
- Paginated admin lists for users and CSV history

## Setup

1. Install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Copy env file:

```bash
cp .env.example .env
```

3. Set values in `.env`:

- `BOT_TOKEN`
- `API_ID`, `API_HASH`
- `SESSION_NAME`
- Optional: `SUPPORT_CHAT_ID`, `SUPPORT_USERNAME`
- Optional safety knobs: `DEFAULT_DELAY_SECONDS`, `DELAY_JITTER_SECONDS`, `MAX_PER_RUN`, `RUN_COOLDOWN_SECONDS`, `MAX_CONSECUTIVE_FAILURES`, `PROGRESS_EVERY`
- Storage: `DB_PATH`, `UPLOAD_ARCHIVE_DIR`

4. Authorize Telethon session once:

```bash
python init_session.py
```

5. Run bot:

```bash
python bot.py
```

## Superadmin Logic

- The very first user to send `/start` is permanently marked as `is_superadmin=1` in DB.
- Superadmin cannot be banned.

## Admin Panel Actions

Superadmin sees `Admin Panel` button in `/start` menu.

- `View All CSVs`: shows historical uploads with `upload_id`
- `View Users`: shows all known users, status, and credits
- `Ban User`: enter numeric user id
- `Unban User`: enter numeric user id
- `Set User Credits`: send `<user_id> <credits>`

Both `View All CSVs` and `View Users` are paginated with `Prev/Next` buttons.

To download archived CSV file:

```text
/csv <upload_id>
```

## Normal Flow

1. User sends `/start`
2. Set target group once:

```text
/setgroup @your_group_or_id
```

3. Click `Upload CSV`
4. Send CSV with user IDs
5. Bot checks your credits first
6. Bot archives CSV, validates IDs, runs controlled invites
7. Credits are consumed for attempted invite actions

## Notes

- Telegram anti-spam limits still apply.
- This bot adds protection but cannot guarantee no limits.
- Use only with permission and in compliance with Telegram rules.

## Production Hosting (GCP VM)

### 1) BotFather setup

In Telegram chat with `@BotFather`:

1. `/newbot`
2. Set bot name and username
3. Copy bot token
4. Optional hardening:
	- `/setprivacy` -> Disable (if you ever need group command reads)
	- `/setjoingroups` -> Disable (recommended for private-operator bot)
	- `/setcommands` with:

```text
start - Open control panel
help - Show help
setgroup - Set target group
status - Show current group
csv - Download archived CSV (superadmin)
```

### 2) VM requirements

- Ubuntu 22.04/24.04 VM
- Python 3.10+
- Outbound internet access to Telegram

### 3) Process manager

Use systemd service template in [deploy/telegram-adder.service](deploy/telegram-adder.service).
