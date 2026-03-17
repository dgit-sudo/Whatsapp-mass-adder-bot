import asyncio
import csv
import io
import logging
import os
import random
import re
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telethon import TelegramClient
from telethon.errors import (
    ChatAdminRequiredError,
    FloodWaitError,
    PeerFloodError,
    UserAlreadyParticipantError,
    UserBotError,
    UserChannelsTooMuchError,
    UserDeactivatedError,
    UserIdInvalidError,
    UserKickedError,
    UserNotMutualContactError,
    UserPrivacyRestrictedError,
)
from telethon.tl.functions.channels import InviteToChannelRequest

load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()
if LOG_LEVEL not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
    LOG_LEVEL = "INFO"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
logger = logging.getLogger("csv-adder-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_ID_RAW = os.getenv("API_ID", "").strip()
API_HASH = os.getenv("API_HASH", "").strip()
SESSION_NAME = os.getenv("SESSION_NAME", "adder_session")
SUPPORT_CHAT_ID_RAW = os.getenv("SUPPORT_CHAT_ID", "").strip()
SUPPORT_USERNAME = "noneedtoknowwhatmynameis"
DEFAULT_DELAY_SECONDS = float(os.getenv("DEFAULT_DELAY_SECONDS", "8"))
DELAY_JITTER_SECONDS = float(os.getenv("DELAY_JITTER_SECONDS", "4"))
MAX_PER_RUN = int(os.getenv("MAX_PER_RUN", "50"))
MAX_CSV_FILE_BYTES = int(os.getenv("MAX_CSV_FILE_BYTES", "1000000"))
RUN_COOLDOWN_SECONDS = int(os.getenv("RUN_COOLDOWN_SECONDS", "900"))
MAX_CONSECUTIVE_FAILURES = int(os.getenv("MAX_CONSECUTIVE_FAILURES", "8"))
PROGRESS_EVERY = int(os.getenv("PROGRESS_EVERY", "10"))
DB_PATH = os.getenv("DB_PATH", "data/bot.db").strip()
UPLOAD_ARCHIVE_DIR = os.getenv("UPLOAD_ARCHIVE_DIR", "data/uploads").strip()

CALLBACK_UPLOAD = "menu_upload"
CALLBACK_ADMIN = "menu_admin"
CALLBACK_ADMIN_USERS = "admin_users"
CALLBACK_ADMIN_CSVS = "admin_csvs"
CALLBACK_ADMIN_BAN = "admin_ban"
CALLBACK_ADMIN_UNBAN = "admin_unban"
CALLBACK_ADMIN_CREDITS = "admin_credits"
CALLBACK_ADMIN_BACK = "admin_back"
CALLBACK_ADMIN_USERS_PAGE = "admin_users_page:"
CALLBACK_ADMIN_CSVS_PAGE = "admin_csvs_page:"

STATE_EXPECTING_UPLOAD = "expecting_upload"
STATE_EXPECTING_SETGROUP = "expecting_setgroup"
STATE_EXPECTING_BAN_INPUT = "expecting_ban_input"
STATE_EXPECTING_UNBAN_INPUT = "expecting_unban_input"
STATE_EXPECTING_CREDITS_INPUT = "expecting_credits_input"
STATE_RUNNING = "invite_run_running"
STATE_LAST_RUN_AT = "last_run_at"

ADMIN_PAGE_SIZE = 20

db_lock = threading.Lock()


def parse_support_chat_id() -> int | None:
    if not SUPPORT_CHAT_ID_RAW:
        return None
    try:
        return int(SUPPORT_CHAT_ID_RAW)
    except ValueError:
        return None


SUPPORT_CHAT_ID = parse_support_chat_id()


def now_ts() -> float:
    return time.time()


def utc_iso(ts: int | float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_storage() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(UPLOAD_ARCHIVE_DIR).mkdir(parents=True, exist_ok=True)
    with db_lock:
        conn = open_db()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    is_superadmin INTEGER NOT NULL DEFAULT 0,
                    is_banned INTEGER NOT NULL DEFAULT 0,
                    credits INTEGER NOT NULL DEFAULT 0,
                    first_seen INTEGER NOT NULL,
                    last_seen INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS uploads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    uploader_id INTEGER NOT NULL,
                    file_name TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )

            # Backward-compatible migration for pre-credits databases.
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(users)").fetchall()
            }
            if "credits" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN credits INTEGER NOT NULL DEFAULT 0")

            conn.commit()
        finally:
            conn.close()


def upsert_user_and_assign_superadmin(user_id: int, username: str | None, first_name: str | None) -> bool:
    ts = int(now_ts())
    with db_lock:
        conn = open_db()
        try:
            conn.execute(
                """
                INSERT INTO users (user_id, username, first_name, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username=excluded.username,
                    first_name=excluded.first_name,
                    last_seen=excluded.last_seen
                """,
                (user_id, username or "", first_name or "", ts, ts),
            )
            superadmin_row = conn.execute("SELECT user_id FROM users WHERE is_superadmin = 1 LIMIT 1").fetchone()
            became_superadmin = False
            if superadmin_row is None:
                conn.execute("UPDATE users SET is_superadmin = 1 WHERE user_id = ?", (user_id,))
                became_superadmin = True
            conn.commit()
            return became_superadmin
        finally:
            conn.close()


def get_user_row(user_id: int) -> sqlite3.Row | None:
    with db_lock:
        conn = open_db()
        try:
            return conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        finally:
            conn.close()


def is_superadmin(user_id: int) -> bool:
    row = get_user_row(user_id)
    return bool(row and row["is_superadmin"] == 1)


def is_banned(user_id: int) -> bool:
    row = get_user_row(user_id)
    return bool(row and row["is_banned"] == 1)


def list_users(limit: int = 500, offset: int = 0) -> List[sqlite3.Row]:
    with db_lock:
        conn = open_db()
        try:
            return conn.execute(
                """
                SELECT user_id, username, first_name, is_superadmin, is_banned, credits, first_seen, last_seen
                FROM users
                ORDER BY last_seen DESC
                LIMIT ?
                OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        finally:
            conn.close()


def count_users() -> int:
    with db_lock:
        conn = open_db()
        try:
            row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
            return int(row["c"]) if row else 0
        finally:
            conn.close()


def set_ban_state(target_user_id: int, banned: bool) -> tuple[bool, str]:
    with db_lock:
        conn = open_db()
        try:
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (target_user_id,)).fetchone()
            if row is None:
                return False, "User ID not found in bot database."
            if row["is_superadmin"] == 1:
                return False, "Cannot ban or unban superadmin."
            conn.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (1 if banned else 0, target_user_id))
            conn.commit()
            return True, "ok"
        finally:
            conn.close()


def get_user_credits(user_id: int) -> int:
    row = get_user_row(user_id)
    if row is None:
        return 0
    return int(row["credits"])


def set_user_credits(target_user_id: int, credits: int) -> tuple[bool, str]:
    if credits < 0:
        return False, "Credits cannot be negative."
    with db_lock:
        conn = open_db()
        try:
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (target_user_id,)).fetchone()
            if row is None:
                return False, "User ID not found in bot database."
            conn.execute("UPDATE users SET credits = ? WHERE user_id = ?", (credits, target_user_id))
            conn.commit()
            return True, "ok"
        finally:
            conn.close()


def decrement_user_credits(target_user_id: int, amount: int) -> tuple[bool, str, int]:
    if amount <= 0:
        return True, "ok", get_user_credits(target_user_id)
    with db_lock:
        conn = open_db()
        try:
            row = conn.execute("SELECT credits FROM users WHERE user_id = ?", (target_user_id,)).fetchone()
            if row is None:
                return False, "User ID not found in bot database.", 0
            current = int(row["credits"])
            if current < amount:
                return False, "Not enough credits.", current
            new_credits = current - amount
            conn.execute("UPDATE users SET credits = ? WHERE user_id = ?", (new_credits, target_user_id))
            conn.commit()
            return True, "ok", new_credits
        finally:
            conn.close()


def sanitize_filename(name: str) -> str:
    base = Path(name).name
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    return safe or "upload.csv"


def archive_uploaded_csv(uploader_id: int, original_name: str, file_bytes: bytes, row_count: int) -> int:
    ts = int(now_ts())
    safe_name = sanitize_filename(original_name)
    stored_name = f"{ts}_{uploader_id}_{safe_name}"
    stored_path = Path(UPLOAD_ARCHIVE_DIR) / stored_name
    stored_path.write_bytes(file_bytes)

    with db_lock:
        conn = open_db()
        try:
            cur = conn.execute(
                """
                INSERT INTO uploads (uploader_id, file_name, file_path, row_count, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (uploader_id, original_name, str(stored_path), row_count, ts),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()


def update_upload_row_count(upload_id: int, row_count: int) -> None:
    with db_lock:
        conn = open_db()
        try:
            conn.execute("UPDATE uploads SET row_count = ? WHERE id = ?", (row_count, upload_id))
            conn.commit()
        finally:
            conn.close()


def list_uploads(limit: int = 500, offset: int = 0) -> List[sqlite3.Row]:
    with db_lock:
        conn = open_db()
        try:
            return conn.execute(
                """
                SELECT id, uploader_id, file_name, file_path, row_count, created_at
                FROM uploads
                ORDER BY created_at DESC
                LIMIT ?
                OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        finally:
            conn.close()


def count_uploads() -> int:
    with db_lock:
        conn = open_db()
        try:
            row = conn.execute("SELECT COUNT(*) AS c FROM uploads").fetchone()
            return int(row["c"]) if row else 0
        finally:
            conn.close()


def get_upload(upload_id: int) -> sqlite3.Row | None:
    with db_lock:
        conn = open_db()
        try:
            return conn.execute("SELECT * FROM uploads WHERE id = ?", (upload_id,)).fetchone()
        finally:
            conn.close()


def resolve_support_chat_id() -> int | None:
    if SUPPORT_CHAT_ID is not None:
        return SUPPORT_CHAT_ID
    with db_lock:
        conn = open_db()
        try:
            row = conn.execute("SELECT user_id FROM users WHERE is_superadmin = 1 LIMIT 1").fetchone()
            return int(row["user_id"]) if row else None
        finally:
            conn.close()


async def ensure_known_user(update: Update) -> tuple[int | None, bool]:
    user = update.effective_user
    if user is None:
        return None, False
    became_superadmin = upsert_user_and_assign_superadmin(user.id, user.username, user.first_name)
    return user.id, became_superadmin


async def require_access(update: Update) -> bool:
    user_id, _ = await ensure_known_user(update)
    if user_id is None:
        return False
    if is_banned(user_id):
        if update.message:
            await update.message.reply_text("Your access to this bot is blocked.")
        elif update.callback_query and update.callback_query.message:
            await update.callback_query.message.reply_text("Your access to this bot is blocked.")
        return False
    return True


def normalize_group_identifier(value: str) -> str | int:
    cleaned = value.strip()
    if cleaned.startswith("https://t.me/"):
        cleaned = cleaned.replace("https://t.me/", "", 1)
    if cleaned.startswith("@"):
        cleaned = cleaned[1:]
    if cleaned.lstrip("-").isdigit():
        return int(cleaned)
    return cleaned


def normalize_user_identifier(raw_value: str) -> str | int | None:
    value = raw_value.strip()
    if not value:
        return None

    # Support tg://user?id=123 format.
    if value.startswith("tg://user?id="):
        value = value.replace("tg://user?id=", "", 1)

    # Support t.me/<username> links.
    if "t.me/" in value:
        value = value.split("t.me/", 1)[1]
        value = value.split("?", 1)[0]
        value = value.split("/", 1)[0]

    if value.startswith("@"):
        value = value[1:]

    if value.lstrip("-").isdigit():
        parsed = int(value)
        return parsed if parsed > 0 else None

    # Basic Telegram username pattern.
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{2,31}", value):
        return value

    return None


def read_csv_user_ids(file_bytes: bytes) -> tuple[List[str | int], List[str]]:
    decoded = None
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            decoded = file_bytes.decode(encoding)
            break
        except UnicodeDecodeError:
            continue

    if decoded is None:
        raise ValueError("Could not decode CSV. Please use UTF-8.")

    reader = csv.reader(io.StringIO(decoded))
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        return [], []

    header = [c.strip().lower() for c in rows[0]]
    has_header = any(h in {"user_id", "id", "telegram_id", "username", "user", "link", "identifier"} for h in header)

    user_ids: List[str | int] = []
    invalid_rows: List[str] = []
    seen: set[str] = set()

    def add_candidate(raw_value: str) -> None:
        if not raw_value.strip():
            return

        normalized = normalize_user_identifier(raw_value)
        if normalized is None:
            invalid_rows.append(raw_value.strip())
            return

        dedupe_key = f"id:{normalized}" if isinstance(normalized, int) else f"user:{normalized.lower()}"
        if dedupe_key in seen:
            return
        seen.add(dedupe_key)
        user_ids.append(normalized)

    if has_header:
        dict_reader = csv.DictReader(io.StringIO(decoded))
        for item in dict_reader:
            raw_id = (
                (item.get("user_id") or "")
                or (item.get("id") or "")
                or (item.get("telegram_id") or "")
                or (item.get("username") or "")
                or (item.get("user") or "")
                or (item.get("link") or "")
                or (item.get("identifier") or "")
            )
            add_candidate(raw_id)
    else:
        for row in rows:
            add_candidate(row[0])

    return user_ids, invalid_rows


def summarize_lines(label: str, values: Iterable[str], limit: int = 8) -> str:
    data = list(values)
    if not data:
        return f"{label}: 0"
    shown = data[:limit]
    remainder = len(data) - len(shown)
    text = f"{label}: {len(data)}\n" + "\n".join(f"- {x}" for x in shown)
    if remainder > 0:
        text += f"\n... and {remainder} more"
    return text


def build_main_menu_markup(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("Upload CSV", callback_data=CALLBACK_UPLOAD)],
        [InlineKeyboardButton("Contact Support", url=f"https://t.me/{SUPPORT_USERNAME}")],
    ]
    if is_superadmin(user_id):
        rows.append([InlineKeyboardButton("Admin Panel", callback_data=CALLBACK_ADMIN)])
    return InlineKeyboardMarkup(rows)


def build_admin_panel_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("View All CSVs", callback_data=CALLBACK_ADMIN_CSVS)],
            [InlineKeyboardButton("View Users", callback_data=CALLBACK_ADMIN_USERS)],
            [InlineKeyboardButton("Set User Credits", callback_data=CALLBACK_ADMIN_CREDITS)],
            [InlineKeyboardButton("Ban User", callback_data=CALLBACK_ADMIN_BAN)],
            [InlineKeyboardButton("Unban User", callback_data=CALLBACK_ADMIN_UNBAN)],
            [InlineKeyboardButton("Back", callback_data=CALLBACK_ADMIN_BACK)],
        ]
    )


def render_menu_text(target_group: str | None, credits: int) -> str:
    group_text = target_group or "not set"
    return (
        "Control Panel\n\n"
        f"Target group: {group_text}\n\n"
        f"Credits: {credits}\n\n"
        "Use the buttons below:\n"
        "- Upload CSV: import user IDs and start invite run\n"
        "- Contact Support: send message directly to support"
    )


def render_users_page(page: int) -> tuple[str, InlineKeyboardMarkup]:
    total = count_users()
    pages = max(1, (total + ADMIN_PAGE_SIZE - 1) // ADMIN_PAGE_SIZE)
    safe_page = max(0, min(page, pages - 1))
    offset = safe_page * ADMIN_PAGE_SIZE
    users = list_users(limit=ADMIN_PAGE_SIZE, offset=offset)

    lines: List[str] = []
    for row in users:
        uname = f"@{row['username']}" if row["username"] else "-"
        flags = []
        if row["is_superadmin"] == 1:
            flags.append("superadmin")
        if row["is_banned"] == 1:
            flags.append("banned")
        flag_text = ",".join(flags) if flags else "active"
        lines.append(
            f"{row['user_id']} | {uname} | {flag_text} | credits={row['credits']}"
        )

    body = "\n".join(lines) if lines else "No users yet."
    text = f"Known users (page {safe_page + 1}/{pages}, total={total})\n\n{body}"
    nav = [
        InlineKeyboardButton("Prev", callback_data=f"{CALLBACK_ADMIN_USERS_PAGE}{safe_page - 1}"),
        InlineKeyboardButton("Next", callback_data=f"{CALLBACK_ADMIN_USERS_PAGE}{safe_page + 1}"),
    ]
    markup = InlineKeyboardMarkup([
        nav,
        [InlineKeyboardButton("Back", callback_data=CALLBACK_ADMIN)],
    ])
    return text, markup


def render_uploads_page(page: int) -> tuple[str, InlineKeyboardMarkup]:
    total = count_uploads()
    pages = max(1, (total + ADMIN_PAGE_SIZE - 1) // ADMIN_PAGE_SIZE)
    safe_page = max(0, min(page, pages - 1))
    offset = safe_page * ADMIN_PAGE_SIZE
    uploads = list_uploads(limit=ADMIN_PAGE_SIZE, offset=offset)

    lines = [
        (
            f"id={row['id']} uploader={row['uploader_id']} rows={row['row_count']} "
            f"file={sanitize_filename(row['file_name'])}"
        )
        for row in uploads
    ]
    body = "\n".join(lines) if lines else "No archived CSV uploads yet."
    text = (
        f"Archived CSV uploads (page {safe_page + 1}/{pages}, total={total})\n\n"
        f"{body}\n\nUse /csv <id> to download a specific archived file."
    )
    nav = [
        InlineKeyboardButton("Prev", callback_data=f"{CALLBACK_ADMIN_CSVS_PAGE}{safe_page - 1}"),
        InlineKeyboardButton("Next", callback_data=f"{CALLBACK_ADMIN_CSVS_PAGE}{safe_page + 1}"),
    ]
    markup = InlineKeyboardMarkup([
        nav,
        [InlineKeyboardButton("Back", callback_data=CALLBACK_ADMIN)],
    ])
    return text, markup


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_access(update):
        return

    user_id, became_superadmin = await ensure_known_user(update)
    if user_id is None:
        return

    context.user_data[STATE_EXPECTING_UPLOAD] = False
    context.user_data[STATE_EXPECTING_SETGROUP] = False
    context.user_data[STATE_EXPECTING_BAN_INPUT] = False
    context.user_data[STATE_EXPECTING_UNBAN_INPUT] = False
    context.user_data[STATE_EXPECTING_CREDITS_INPUT] = False

    if became_superadmin:
        await update.message.reply_text("You are the first user and now the superadmin.")

    group_value = context.user_data.get("target_group")
    await update.message.reply_text(
        render_menu_text(group_value, get_user_credits(user_id)),
        reply_markup=build_main_menu_markup(user_id),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_access(update):
        return
    await update.message.reply_text(
        "Commands:\n"
        "/setgroup <group_username_or_id> - set target group\n"
        "/status - show current target group\n"
        "/csv <upload_id> - superadmin can download archived CSV\n"
        "/start - open control panel\n"
        "/help - show this message\n\n"
        "CSV format for upload:\n"
        "- Header or single-column CSV\n"
        "- Supports: numeric user_id, @username, username, t.me link, tg://user?id=..."
    )


async def setgroup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_access(update):
        return
    if not context.args:
        context.user_data[STATE_EXPECTING_SETGROUP] = True
        await update.message.reply_text(
            "Send target group now (username/link/id).\n"
            "Examples: @ONEHACKGOD or ONEHACKGOD or https://t.me/ONEHACKGOD"
        )
        return
    group_value = " ".join(context.args).strip()

    client: TelegramClient | None = context.application.bot_data.get("telethon_client")
    if client is None:
        await update.message.reply_text("Group check failed: Telethon client is not initialized.")
        return

    try:
        resolved = await client.get_entity(normalize_group_identifier(group_value))
    except Exception as exc:
        await update.message.reply_text(
            "Group is NOT set. I could not verify this target. "
            f"Error: {exc}"
        )
        return

    context.user_data["target_group"] = group_value
    context.user_data[STATE_EXPECTING_SETGROUP] = False
    title = getattr(resolved, "title", None) or getattr(resolved, "username", None) or str(group_value)
    await update.message.reply_text(f"Group is set successfully: {title}")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_access(update):
        return
    user_id = update.effective_user.id
    group_value = context.user_data.get("target_group")
    if not group_value:
        await update.message.reply_text("No target group set. Use /setgroup first.")
        return
    await update.message.reply_text(
        render_menu_text(group_value, get_user_credits(user_id)),
        reply_markup=build_main_menu_markup(user_id),
    )


async def csv_download_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_access(update):
        return
    user_id = update.effective_user.id
    if not is_superadmin(user_id):
        await update.message.reply_text("Only superadmin can download archived CSV files.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /csv <upload_id>")
        return

    upload_id = int(context.args[0])
    upload = get_upload(upload_id)
    if upload is None:
        await update.message.reply_text("Upload ID not found.")
        return

    path = Path(upload["file_path"])
    if not path.exists():
        await update.message.reply_text("File is missing from archive storage.")
        return

    with path.open("rb") as handle:
        await update.message.reply_document(
            document=handle,
            filename=f"upload_{upload_id}_{sanitize_filename(upload['file_name'])}",
            caption=(
                f"Upload ID: {upload_id}\n"
                f"Uploader: {upload['uploader_id']}\n"
                f"Rows: {upload['row_count']}\n"
                f"At: {utc_iso(upload['created_at'])}"
            ),
        )


async def menu_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_access(update):
        return

    query = update.callback_query
    if query is None:
        return

    await query.answer()
    user_id = query.from_user.id

    if query.data == CALLBACK_UPLOAD:
        context.user_data[STATE_EXPECTING_UPLOAD] = True
        context.user_data[STATE_EXPECTING_SETGROUP] = False
        context.user_data[STATE_EXPECTING_BAN_INPUT] = False
        context.user_data[STATE_EXPECTING_UNBAN_INPUT] = False
        context.user_data[STATE_EXPECTING_CREDITS_INPUT] = False
        await query.message.reply_text(
            "Upload mode enabled.\n\n"
            "Now send a CSV containing Telegram user identifiers.\n"
            "Accepted headers: user_id, id, telegram_id, username, user, link, identifier\n"
            "If group is not set, run /setgroup first."
        )
        return

    if query.data == CALLBACK_ADMIN:
        if not is_superadmin(user_id):
            await query.message.reply_text("Only superadmin can open Admin Panel.")
            return
        context.user_data[STATE_EXPECTING_SETGROUP] = False
        context.user_data[STATE_EXPECTING_BAN_INPUT] = False
        context.user_data[STATE_EXPECTING_UNBAN_INPUT] = False
        context.user_data[STATE_EXPECTING_CREDITS_INPUT] = False
        await query.message.reply_text("Admin Panel", reply_markup=build_admin_panel_markup())
        return

    if query.data == CALLBACK_ADMIN_BACK:
        await query.message.reply_text(
            render_menu_text(context.user_data.get("target_group"), get_user_credits(user_id)),
            reply_markup=build_main_menu_markup(user_id),
        )
        return

    if not is_superadmin(user_id):
        await query.message.reply_text("Only superadmin can use admin actions.")
        return

    if query.data == CALLBACK_ADMIN_USERS:
        text, markup = render_users_page(0)
        await query.message.reply_text(text, reply_markup=markup)
        return

    if query.data == CALLBACK_ADMIN_CSVS:
        text, markup = render_uploads_page(0)
        await query.message.reply_text(text, reply_markup=markup)
        return

    if query.data.startswith(CALLBACK_ADMIN_USERS_PAGE):
        raw_page = query.data.replace(CALLBACK_ADMIN_USERS_PAGE, "", 1)
        page = int(raw_page) if raw_page.lstrip("-").isdigit() else 0
        text, markup = render_users_page(page)
        await query.message.reply_text(text, reply_markup=markup)
        return

    if query.data.startswith(CALLBACK_ADMIN_CSVS_PAGE):
        raw_page = query.data.replace(CALLBACK_ADMIN_CSVS_PAGE, "", 1)
        page = int(raw_page) if raw_page.lstrip("-").isdigit() else 0
        text, markup = render_uploads_page(page)
        await query.message.reply_text(text, reply_markup=markup)
        return

    if query.data == CALLBACK_ADMIN_CREDITS:
        context.user_data[STATE_EXPECTING_CREDITS_INPUT] = True
        context.user_data[STATE_EXPECTING_BAN_INPUT] = False
        context.user_data[STATE_EXPECTING_UNBAN_INPUT] = False
        await query.message.reply_text(
            "Send credits update in this format:\n<user_id> <credits>\nExample: 123456789 25"
        )
        return

    if query.data == CALLBACK_ADMIN_BAN:
        context.user_data[STATE_EXPECTING_BAN_INPUT] = True
        context.user_data[STATE_EXPECTING_UNBAN_INPUT] = False
        await query.message.reply_text("Send the numeric user ID to ban.")
        return

    if query.data == CALLBACK_ADMIN_UNBAN:
        context.user_data[STATE_EXPECTING_UNBAN_INPUT] = True
        context.user_data[STATE_EXPECTING_BAN_INPUT] = False
        await query.message.reply_text("Send the numeric user ID to unban.")
        return


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_access(update):
        return
    if update.effective_chat is None or update.effective_chat.type != "private":
        return
    if not update.message or not update.message.text:
        return

    sender_id = update.effective_user.id
    text = update.message.text.strip()

    if context.user_data.get(STATE_EXPECTING_SETGROUP):
        group_value = text
        if not group_value:
            await update.message.reply_text("Group value cannot be empty. Send username/link/id.")
            return

        client: TelegramClient | None = context.application.bot_data.get("telethon_client")
        if client is None:
            await update.message.reply_text("Group check failed: Telethon client is not initialized.")
            return

        try:
            resolved = await client.get_entity(normalize_group_identifier(group_value))
        except Exception as exc:
            await update.message.reply_text(
                "Group is NOT set. I could not verify this target. "
                f"Error: {exc}"
            )
            return

        context.user_data["target_group"] = group_value
        context.user_data[STATE_EXPECTING_SETGROUP] = False
        title = getattr(resolved, "title", None) or getattr(resolved, "username", None) or str(group_value)
        await update.message.reply_text(f"Group is set successfully: {title}")
        return

    if is_superadmin(sender_id) and context.user_data.get(STATE_EXPECTING_BAN_INPUT):
        if not text.isdigit():
            await update.message.reply_text("Invalid user ID. Send numeric ID only.")
            return
        ok, msg = set_ban_state(int(text), banned=True)
        context.user_data[STATE_EXPECTING_BAN_INPUT] = False
        await update.message.reply_text("User banned." if ok else msg)
        return

    if is_superadmin(sender_id) and context.user_data.get(STATE_EXPECTING_UNBAN_INPUT):
        if not text.isdigit():
            await update.message.reply_text("Invalid user ID. Send numeric ID only.")
            return
        ok, msg = set_ban_state(int(text), banned=False)
        context.user_data[STATE_EXPECTING_UNBAN_INPUT] = False
        await update.message.reply_text("User unbanned." if ok else msg)
        return

    if is_superadmin(sender_id) and context.user_data.get(STATE_EXPECTING_CREDITS_INPUT):
        parts = text.split()
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            await update.message.reply_text("Invalid format. Use: <user_id> <credits>")
            return
        target_user_id = int(parts[0])
        credits = int(parts[1])
        ok, msg = set_user_credits(target_user_id, credits)
        context.user_data[STATE_EXPECTING_CREDITS_INPUT] = False
        await update.message.reply_text(
            f"Credits updated. user_id={target_user_id} credits={credits}" if ok else msg
        )
        return

    return


async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_access(update):
        return

    if update.effective_chat is None or update.effective_chat.type != "private":
        await update.message.reply_text("Send CSV in a private chat with the bot.")
        return

    if not context.user_data.get(STATE_EXPECTING_UPLOAD):
        await update.message.reply_text("Press Upload CSV from /start menu first, then send the CSV.")
        return

    if "telethon_client" not in context.application.bot_data:
        await update.message.reply_text("Telethon client is not initialized.")
        return

    target_group = context.user_data.get("target_group")
    if not target_group:
        await update.message.reply_text("Set a group first using /setgroup.")
        return

    if not update.message or not update.message.document:
        await update.message.reply_text("Please send a CSV file.")
        return

    sender_id = update.effective_user.id
    available_credits = get_user_credits(sender_id)
    if available_credits <= 0:
        await update.message.reply_text(
            "You have 0 credits. Ask superadmin to assign credits before uploading CSV."
        )
        return

    filename = update.message.document.file_name or "upload.csv"

    if not filename.lower().endswith(".csv"):
        await update.message.reply_text("Only .csv files are accepted.")
        return

    if update.message.document.file_size and update.message.document.file_size > MAX_CSV_FILE_BYTES:
        await update.message.reply_text(f"CSV is too large. Max allowed is {MAX_CSV_FILE_BYTES} bytes.")
        return

    if context.user_data.get(STATE_RUNNING):
        await update.message.reply_text("An invite run is already in progress for your account.")
        return

    last_run_at = float(context.user_data.get(STATE_LAST_RUN_AT, 0.0))
    remaining = int(RUN_COOLDOWN_SECONDS - (now_ts() - last_run_at))
    if remaining > 0:
        await update.message.reply_text(f"Cooldown is active. Wait {remaining}s before the next CSV run.")
        return

    await update.message.reply_text("Downloading and parsing CSV...")
    tg_file = await context.bot.get_file(update.message.document.file_id)
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=True) as tmp:
        await tg_file.download_to_drive(custom_path=tmp.name)
        tmp.seek(0)
        file_bytes = tmp.read()

    archive_id = archive_uploaded_csv(sender_id, filename, file_bytes, row_count=0)

    try:
        user_ids, invalid_rows = read_csv_user_ids(file_bytes)
    except ValueError as exc:
        await update.message.reply_text(f"CSV error: {exc}\nArchive ID: {archive_id}")
        return

    if invalid_rows:
        await update.message.reply_text(
            "CSV contains invalid rows. Allowed formats: numeric user_id, @username, username, t.me link, tg://user?id=...\n\n"
            + summarize_lines("Invalid examples", invalid_rows)
            + f"\n\nArchive ID: {archive_id}"
        )
        return

    if not user_ids:
        await update.message.reply_text(f"No valid Telegram user identifiers found in CSV.\nArchive ID: {archive_id}")
        return

    update_upload_row_count(archive_id, len(user_ids))

    if len(user_ids) > MAX_PER_RUN:
        await update.message.reply_text(
            f"CSV has {len(user_ids)} entries. Limit is MAX_PER_RUN={MAX_PER_RUN}.\nArchive ID: {archive_id}"
        )
        return

    if len(user_ids) > available_credits:
        await update.message.reply_text(
            f"Not enough credits. Needed={len(user_ids)}, available={available_credits}.\n"
            "Ask superadmin to add more credits or use a smaller CSV."
        )
        return

    client: TelegramClient = context.application.bot_data["telethon_client"]

    try:
        group_entity = await client.get_entity(normalize_group_identifier(target_group))
    except Exception as exc:
        await update.message.reply_text(f"Could not resolve target group: {exc}\nArchive ID: {archive_id}")
        return

    context.user_data[STATE_RUNNING] = True
    await update.message.reply_text(
        f"Starting invite run for {len(user_ids)} users. Base delay={DEFAULT_DELAY_SECONDS}s\nArchive ID: {archive_id}"
    )

    added: List[str] = []
    skipped: List[str] = []
    failed: List[str] = []
    consecutive_failures = 0
    attempted_count = 0

    try:
        for idx, user_id in enumerate(user_ids, start=1):
            key = str(user_id)
            attempted_count += 1
            try:
                user_entity = await client.get_entity(user_id)
                await client(InviteToChannelRequest(channel=group_entity, users=[user_entity]))
                added.append(key)
                consecutive_failures = 0
            except UserAlreadyParticipantError:
                skipped.append(f"{key} (already in group)")
                consecutive_failures = 0
            except (UserPrivacyRestrictedError, UserNotMutualContactError):
                skipped.append(f"{key} (privacy/contact restriction)")
                consecutive_failures += 1
            except (UserChannelsTooMuchError, UserKickedError, UserIdInvalidError, UserBotError, UserDeactivatedError) as exc:
                skipped.append(f"{key} ({exc.__class__.__name__})")
                consecutive_failures += 1
            except ChatAdminRequiredError:
                failed.append(f"{key} (ChatAdminRequiredError)")
                await update.message.reply_text(
                    "Invite run stopped. Your user session is missing add-member admin rights in this group."
                )
                break
            except PeerFloodError:
                failed.append(f"{key} (PeerFloodError)")
                await update.message.reply_text(
                    "Invite run stopped due to anti-spam limit (PeerFlood). Wait and try later with lower volume."
                )
                break
            except FloodWaitError as exc:
                wait_seconds = int(exc.seconds) + 3
                await update.message.reply_text(f"Flood wait triggered ({wait_seconds}s). Pausing before retrying {key}.")
                await asyncio.sleep(wait_seconds)
                try:
                    user_entity = await client.get_entity(user_id)
                    await client(InviteToChannelRequest(channel=group_entity, users=[user_entity]))
                    added.append(key)
                    consecutive_failures = 0
                except Exception as retry_exc:
                    failed.append(f"{key} ({retry_exc})")
                    consecutive_failures += 1
            except Exception as exc:
                failed.append(f"{key} ({exc})")
                consecutive_failures += 1

            if PROGRESS_EVERY > 0 and idx % PROGRESS_EVERY == 0:
                await update.message.reply_text(
                    f"Progress: {idx}/{len(user_ids)} processed. Added={len(added)} Skipped={len(skipped)} Failed={len(failed)}"
                )

            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                await update.message.reply_text(
                    "Invite run stopped early due to repeated failures. This helps reduce spam-risk signals."
                )
                break

            if idx != len(user_ids):
                delay = DEFAULT_DELAY_SECONDS + random.uniform(0, max(DELAY_JITTER_SECONDS, 0.0))
                await asyncio.sleep(delay)
    finally:
        context.user_data[STATE_RUNNING] = False
        context.user_data[STATE_LAST_RUN_AT] = now_ts()
        context.user_data[STATE_EXPECTING_UPLOAD] = False

    ok_debit, debit_msg, remaining_credits = decrement_user_credits(sender_id, attempted_count)
    if not ok_debit:
        logger.warning("Could not decrement credits for %s: %s", sender_id, debit_msg)
        remaining_credits = get_user_credits(sender_id)

    report = (
        "Invite run finished.\n\n"
        + summarize_lines("Added", added)
        + "\n\n"
        + summarize_lines("Skipped", skipped)
        + "\n\n"
        + summarize_lines("Failed", failed)
        + f"\n\nAttempted (credit used): {attempted_count}"
        + f"\nCredits remaining: {remaining_credits}"
        + f"\n\nArchive ID: {archive_id}"
    )
    await update.message.reply_text(report, reply_markup=build_main_menu_markup(sender_id))


async def on_startup(app: Application) -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing")
    if not API_ID_RAW or not API_HASH:
        raise RuntimeError("API_ID/API_HASH is missing")

    init_storage()

    api_id = int(API_ID_RAW)
    client = TelegramClient(SESSION_NAME, api_id, API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("Telethon user session is not authorized. Run init_session.py first.")

    app.bot_data["telethon_client"] = client
    logger.info("Telethon client connected and authorized")


async def on_shutdown(app: Application) -> None:
    client: TelegramClient | None = app.bot_data.get("telethon_client")
    if client:
        await client.disconnect()
        logger.info("Telethon client disconnected")


def build_application() -> Application:
    application = ApplicationBuilder().token(BOT_TOKEN).post_init(on_startup).post_shutdown(on_shutdown).build()

    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("setgroup", setgroup_cmd))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CommandHandler("csv", csv_download_cmd))
    application.add_handler(CallbackQueryHandler(menu_callback_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    application.add_handler(MessageHandler(filters.Document.ALL, document_handler))

    return application


def main() -> None:
    app = build_application()
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
