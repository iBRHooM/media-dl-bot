"""
Media Downloader Telegram Bot.

Supported:
  - URL-based: YouTube, TikTok, X/Twitter, Facebook, Instagram, Twitch
  - Username-based: Snapchat (type: snapchat <username>)
"""

import os
import time
import logging
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from importlib.metadata import version, PackageNotFoundError

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError

from downloader import fetch_formats, download_media, needs_quality_picker
from snapchat import (
    fetch_snapchat_stories,
    download_story_media,
    download_one_snap,
    build_story_grid,
    GRID_PAGE_SIZE,
)
from utils import detect_platform, sizeof_fmt, cleanup_files, escape_markdown

# ── Version (single source of truth: pyproject.toml) ─────────────────────────

try:
    __version__ = version("media-dl-bot")
except PackageNotFoundError:
    # Package not installed (e.g. running from source without `pip install .`)
    __version__ = "0.0.0+unknown"

# ── Logging ──────────────────────────────────────────────────────────────────

# Ensure log directory exists before FileHandler is instantiated.
# (Volume mount creates it, but only at container start; this guards against
# any edge case where the path isn't writable.)
LOG_DIR = Path("/app/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

class _RedactingFormatter(logging.Formatter):
    """
    Redact the bot token from every formatted log record.

    Defense in depth for the v0.1.4 token-leak class: the httpx/httpcore
    silencing below stops those loggers' own request lines, but it cannot
    cover an exception whose text embeds a request URL (`/bot<TOKEN>/...`)
    being logged with a traceback via logger.exception / on_error. This
    formatter runs on the final formatted string — message, args, and
    traceback text included — so the token never reaches bot.log or
    stdout regardless of which code path logged it.

    The token is looked up per record (not captured at import) because
    this class is defined before the BOT_TOKEN config check runs.
    """

    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        token = os.environ.get("BOT_TOKEN")
        if token:
            formatted = formatted.replace(token, "<BOT_TOKEN_REDACTED>")
        return formatted


_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_log_handlers = [
    logging.StreamHandler(),
    logging.FileHandler(LOG_DIR / "bot.log"),
]
for _handler in _log_handlers:
    _handler.setFormatter(_RedactingFormatter(_LOG_FORMAT))

logging.basicConfig(level=logging.INFO, handlers=_log_handlers)

# Silence httpx's request logging — it includes the bot token in every URL,
# which would leak the token into bot.log. We still see Telegram errors via
# python-telegram-bot's own exception handling.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit(
        "BOT_TOKEN environment variable is required. "
        "Set it in your .env file (see .env.example)."
    )

LOCAL_API_URL = os.environ.get("LOCAL_API_URL", "")
MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", 1900))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

_allowed_raw = os.environ.get("ALLOWED_USERS", "").strip()
ALLOWED_USERS: set[int] = (
    {int(uid.strip()) for uid in _allowed_raw.split(",") if uid.strip()}
    if _allowed_raw
    else set()
)

# Timezone for displaying Snapchat post timestamps in captions.
# Defaults to Asia/Riyadh (the operator's locale). Override via TZ env var
# using any IANA name, e.g. TZ=America/New_York or TZ=UTC.
# Invalid values are caught here at startup rather than at caption time.
_tz_name = os.environ.get("TZ", "Asia/Riyadh").strip() or "Asia/Riyadh"
try:
    DISPLAY_TZ = ZoneInfo(_tz_name)
    logger.info(f"Display timezone: {_tz_name}")
except ZoneInfoNotFoundError:
    logger.warning(
        f"Unknown timezone {_tz_name!r} — falling back to UTC. "
        f"Use an IANA timezone name (e.g. Asia/Riyadh, America/New_York)."
    )
    DISPLAY_TZ = timezone.utc

PLATFORM_LABELS = {
    "youtube": "YouTube",
    "tiktok": "TikTok",
    "twitter": "X (Twitter)",
    "facebook": "Facebook",
    "instagram": "Instagram",
    "twitch": "Twitch",
    "snapchat": "Snapchat",
}

# Longest message handle_message will consider. Supported URLs are well
# under this; anything longer is junk that would otherwise flow into
# yt-dlp, Playwright, and log lines. (Telegram's own cap is 4096.)
MAX_INPUT_LENGTH = 1024

# How long a Snapchat picker session is kept in memory before it's
# considered stale and the user has to re-send the username.
# v0.1.9: bumped from 120s → 300s to give multi-pick more breathing room
# (the grid stays open after a single-snap download, so users naturally
# want to come back and pick more without re-requesting the story).
SNAPCHAT_PICKER_TTL_SECONDS = 300  # 5 minutes

HELP_TEXT = """
👋 *Media Downloader Bot*

*URL-based platforms* — just paste the link:
• YouTube (videos & Shorts)
• TikTok (no watermark)
• X / Twitter
• Facebook
• Instagram (posts & reels)
• Twitch (clips & VODs)

*Username-based:*
• `snapchat <username>` — preview all stories in a grid, then pick which to download (or grab them all)

For YouTube, Facebook, Twitch, and X you'll be asked to pick a quality.
"""


# ── Auth guard ────────────────────────────────────────────────────────────────

def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True
    return user_id in ALLOWED_USERS


# ── Handlers ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Main message handler — routes to correct downloader."""
    user = update.effective_user
    if not is_allowed(user.id):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    text = update.message.text.strip()

    # Length guard before any pattern matching: no legitimate supported
    # URL or username command comes close to this. Keeps oversized junk
    # out of yt-dlp, Playwright navigation, and log lines.
    if len(text) > MAX_INPUT_LENGTH:
        await update.message.reply_text(
            "❌ Message too long to be a supported URL or command."
        )
        return

    platform, target = detect_platform(text)

    if not platform:
        await update.message.reply_text(
            "❓ Unrecognized input.\n\n"
            "Paste a supported URL or type `snapchat <username>`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    label = PLATFORM_LABELS.get(platform, platform.capitalize())
    logger.info(f"User {user.id} requested {label}: {target}")

    # ── Snapchat (username-based) ─────────────────────────────────────────────
    if platform == "snapchat":
        await handle_snapchat(update, context, target)
        return

    # ── Quality picker platforms ──────────────────────────────────────────────
    if needs_quality_picker(platform):
        await handle_quality_picker(update, context, platform, target)
        return

    # ── Auto-best (TikTok, Instagram) ─────────────────────────────────────────
    await handle_auto_download(update, context, platform, target)


def _build_snap_caption(username: str, index: int, total: int, timestamp: int) -> str:
    """
    Build the three-line caption attached to each Snapchat story item.

    Format:
        👻 @username
        N of M
        YYYY-MM-DD HH:MMam/pm

    `index` is the snap's `snapIndex` from Snapchat (0-based), shown to
    the user as `index+1` so gaps (failed downloads) are visible —
    matches what they would see in the Snapchat app.

    `timestamp` is Unix seconds when the snap was posted. Rendered in
    DISPLAY_TZ (configurable via the TZ env var). If 0 (Snapchat didn't
    return a timestamp), the date line is omitted rather than showing a
    bogus 1970-01-01 value.
    """
    lines = [f"👻 @{username}", f"{index + 1} of {total}"]
    if timestamp > 0:
        when = datetime.fromtimestamp(timestamp, tz=DISPLAY_TZ)
        # 12-hour clock with lowercase am/pm suffix:
        #   "2026-04-25 01:56pm"
        date_part = when.strftime("%Y-%m-%d %I:%M")
        ampm = when.strftime("%p").lower()
        lines.append(f"{date_part}{ampm}")
    return "\n".join(lines)


async def handle_snapchat(
    update: Update, context: ContextTypes.DEFAULT_TYPE, username: str
) -> None:
    """
    v0.1.8: instead of auto-downloading every snap, fetch the story
    listing and present a numbered grid + inline buttons. The user
    picks which snap(s) to download.

    Flow:
      1. Scrape story metadata (URLs + thumbnails) — no media downloaded yet.
      2. Build the page-0 grid PNG from Snapchat's `mediaPreviewUrl` thumbs.
      3. Send the grid as a photo with inline buttons numbered 1..N for
         this page, plus pagination (if >1 page) and "Download all".
      4. The CallbackQuery handler `handle_snapchat_callback` reacts to
         taps and either re-renders a different page or downloads.

    Session state lives in `context.bot_data["snapchat_sessions"][key]`
    keyed by user_id + message_id so concurrent users don't collide.
    Sessions expire after SNAPCHAT_PICKER_TTL_SECONDS (2 minutes).
    """
    status = None
    try:
        status = await update.message.reply_text(
            f"👻 Fetching stories for *@{escape_markdown(username)}*...",
            parse_mode=ParseMode.MARKDOWN,
        )

        media_items = await fetch_snapchat_stories(username)
        total = len(media_items)
        await status.edit_text(
            f"🖼 Building preview grid for {total} snap(s)..."
        )

        # Build page 0 of the grid.
        png, page_count, page_size = await build_story_grid(media_items, page=0)

        # Store session. Same `bot_data` pattern as the YouTube quality picker,
        # but keyed under its own namespace so the two don't clash.
        sessions = context.bot_data.setdefault("snapchat_sessions", {})
        # Purge stale sessions opportunistically (no background task).
        _purge_expired_snapchat_sessions(sessions)

        key = f"sc_{update.effective_user.id}_{update.message.message_id}"
        sessions[key] = {
            "username": username,
            "media_items": media_items,
            "created_at": time.time(),
            # Tracks which snap indices (0-based) the user has already
            # downloaded in this session. Used to mark them with ✅ in
            # the keyboard so it's obvious what's been grabbed.
            "picked": set(),
        }

        markup = _build_grid_keyboard(
            key=key, page=0, page_count=page_count, total=total
        )

        # Send the grid as a photo. The status "Building..." message is
        # deleted on success to keep the chat tidy.
        await update.message.reply_photo(
            photo=png,
            caption=(
                f"👻 *@{escape_markdown(username)}* — {total} snap(s)\n"
                f"Tap a number to download, or ⭐ for all."
            ),
            reply_markup=markup,
            parse_mode=ParseMode.MARKDOWN,
        )
        await status.delete()

    except (ValueError, RuntimeError) as e:
        if status is not None:
            await status.edit_text(f"❌ {e}")
        else:
            await update.message.reply_text(f"❌ {e}")
    except Exception:
        logger.exception(f"Unexpected error in handle_snapchat for @{username}")
        msg = "❌ An unexpected error occurred. Please try again."
        try:
            if status is not None:
                await status.edit_text(msg)
            else:
                await update.message.reply_text(msg)
        except TelegramError:
            pass


def _purge_expired_snapchat_sessions(sessions: dict) -> None:
    """Drop sessions older than the TTL. Mutates the dict in place."""
    now = time.time()
    expired = [
        k for k, v in sessions.items()
        if now - v.get("created_at", 0) > SNAPCHAT_PICKER_TTL_SECONDS
    ]
    for k in expired:
        sessions.pop(k, None)


def _build_grid_keyboard(
    key: str,
    page: int,
    page_count: int,
    total: int,
    picked: set[int] | None = None,
) -> InlineKeyboardMarkup:
    """
    Build the inline keyboard for a grid page.

    Layout:
        [1] [2] [3] [4]
        [5] [6] [7] [8]
        [9] [10] [11] [12]
        [⬅️ Prev]  [N/M]  [Next ➡️]    (only if page_count > 1)
        [⭐ Download all]
        [❌ Close]

    Number buttons are 4-per-row. Already-downloaded snaps (in `picked`)
    get a ✅ prefix so the user can see at a glance what's been grabbed
    in this session — useful since the grid stays open after each
    single-snap pick (v0.1.9). Pagination omitted for single-page stories.
    """
    if picked is None:
        picked = set()

    start = page * GRID_PAGE_SIZE
    end = min(start + GRID_PAGE_SIZE, total)

    rows: list[list[InlineKeyboardButton]] = []
    # Number buttons, 4 per row.
    current_row: list[InlineKeyboardButton] = []
    for n in range(start + 1, end + 1):  # 1-based for display
        snap_idx = n - 1
        label = f"✅ {n}" if snap_idx in picked else str(n)
        current_row.append(
            InlineKeyboardButton(
                text=label,
                callback_data=f"sc|{key}|dl|{snap_idx}",  # 0-based snapIndex
            )
        )
        if len(current_row) == 4:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)

    # Pagination row.
    if page_count > 1:
        prev_data = (
            f"sc|{key}|page|{page - 1}" if page > 0 else "sc|noop"
        )
        next_data = (
            f"sc|{key}|page|{page + 1}" if page < page_count - 1 else "sc|noop"
        )
        rows.append([
            InlineKeyboardButton(
                "⬅️ Prev" if page > 0 else "·",
                callback_data=prev_data,
            ),
            InlineKeyboardButton(
                f"{page + 1}/{page_count}",
                callback_data="sc|noop",
            ),
            InlineKeyboardButton(
                "Next ➡️" if page < page_count - 1 else "·",
                callback_data=next_data,
            ),
        ])

    # Action rows.
    rows.append([
        InlineKeyboardButton(
            "⭐ Download all", callback_data=f"sc|{key}|all|"
        )
    ])
    rows.append([
        InlineKeyboardButton("❌ Close", callback_data=f"sc|{key}|cancel|")
    ])

    return InlineKeyboardMarkup(rows)


async def handle_snapchat_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle taps on the Snapchat grid picker keyboard."""
    query = update.callback_query
    # Auth guard on every entry point: unauthorized users never receive
    # picker keyboards, but callback queries are still an independent
    # update type (e.g. in a group chat where an allowed user opened a
    # picker) and must be checked like any other handler.
    if not is_allowed(update.effective_user.id):
        await query.answer("Not authorized", show_alert=False)
        return
    await query.answer()

    data = query.data or ""
    # "sc|noop" — disabled pagination edges, ignore silently.
    if data == "sc|noop":
        return

    try:
        _, key, action, arg = data.split("|", 3)
    except ValueError:
        await query.answer("Invalid button", show_alert=False)
        return

    sessions = context.bot_data.get("snapchat_sessions", {})
    _purge_expired_snapchat_sessions(sessions)
    session = sessions.get(key)

    if not session:
        # Session purged (timed out, restart, or already cancelled).
        try:
            await query.edit_message_caption(
                caption="⌛ Session expired. Send the username again.",
                reply_markup=None,
            )
        except TelegramError:
            pass
        return

    username = session["username"]
    media_items = session["media_items"]
    total = len(media_items)

    if action == "cancel":
        sessions.pop(key, None)
        try:
            # Update the grid caption and remove buttons. We keep the
            # grid image so the user can scroll back and see what was
            # offered without re-requesting.
            await query.edit_message_caption(
                caption=(
                    f"👻 *@{escape_markdown(username)}* — picker closed."
                ),
                reply_markup=None,
                parse_mode=ParseMode.MARKDOWN,
            )
        except TelegramError:
            pass
        return

    if action == "page":
        try:
            new_page = int(arg)
        except ValueError:
            return
        try:
            png, page_count, _ = await build_story_grid(media_items, page=new_page)
        except Exception:
            logger.exception("Failed to rebuild grid for page change")
            await query.answer("Couldn't load that page", show_alert=True)
            return
        markup = _build_grid_keyboard(
            key=key,
            page=new_page,
            page_count=page_count,
            total=total,
            picked=session.get("picked", set()),
        )
        # `edit_message_media` replaces both the image and the keyboard.
        from telegram import InputMediaPhoto
        try:
            await query.edit_message_media(
                media=InputMediaPhoto(
                    media=png,
                    caption=(
                        f"👻 *@{escape_markdown(username)}* — {total} snap(s)\n"
                        f"Tap a number to download, or ⭐ for all."
                    ),
                    parse_mode=ParseMode.MARKDOWN,
                ),
                reply_markup=markup,
            )
        except TelegramError as e:
            logger.error(f"Failed to edit grid message: {e}")
        return

    if action == "dl":
        try:
            snap_idx = int(arg)
        except ValueError:
            return
        if not (0 <= snap_idx < total):
            await query.answer("Out of range", show_alert=True)
            return

        success = await _download_and_send_single(
            update, context, session, snap_idx
        )

        # v0.1.9: keep the picker open after a single-snap download so
        # the user can grab more snaps without re-requesting the story.
        # Mark the picked snap with ✅ in the keyboard if download
        # succeeded. The grid image itself doesn't change — only the
        # button labels — so we use edit_message_reply_markup (lighter
        # than edit_message_media, and avoids re-uploading the PNG).
        if success:
            session.setdefault("picked", set()).add(snap_idx)
            # Refresh the session timestamp too — the user is actively
            # using the picker, so the 5-minute TTL should restart.
            session["created_at"] = time.time()

            # Figure out which page the picked snap is on so we re-render
            # the keyboard for the current view (not page 0).
            current_page = snap_idx // GRID_PAGE_SIZE
            page_count = (total + GRID_PAGE_SIZE - 1) // GRID_PAGE_SIZE
            markup = _build_grid_keyboard(
                key=key,
                page=current_page,
                page_count=page_count,
                total=total,
                picked=session["picked"],
            )
            try:
                await query.edit_message_reply_markup(reply_markup=markup)
            except TelegramError as e:
                # Non-fatal: the download already worked, the user just
                # won't see the ✅ marker. Log and move on.
                logger.warning(f"Could not refresh picker keyboard: {e}")
        return

    if action == "all":
        await _download_and_send_all(update, context, session)
        # "Download all" ends the session — clear the picker UI entirely
        # by removing the keyboard and updating the caption.
        sessions.pop(key, None)
        try:
            await query.edit_message_caption(
                caption=(
                    f"👻 *@{escape_markdown(username)}* — all "
                    f"{total} snap(s) downloaded ✅"
                ),
                reply_markup=None,
                parse_mode=ParseMode.MARKDOWN,
            )
        except TelegramError as e:
            logger.warning(f"Could not close picker after 'all': {e}")
        return


async def _download_and_send_single(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: dict,
    snap_idx: int,
) -> bool:
    """
    Download one snap from the picker and send it as a follow-up message.

    Returns True on success (snap sent, or oversized-skipped), False on
    failure (download error or Telegram error). The caller uses the
    return value to decide whether to mark the snap with ✅ in the
    keyboard.
    """
    query = update.callback_query
    username = session["username"]
    media_items = session["media_items"]
    total = len(media_items)
    item = media_items[snap_idx]

    notice = await query.message.reply_text(
        f"⬇️ Downloading snap {snap_idx + 1} of {total}..."
    )

    file_path: str | None = None
    try:
        result = await download_one_snap(item, username, total)
        if not result:
            await notice.edit_text(f"❌ Failed to download snap {snap_idx + 1}.")
            return False
        file_path, media_type, index, total_out, timestamp = result

        file_size = os.path.getsize(file_path)
        if file_size > MAX_FILE_SIZE_BYTES:
            await notice.edit_text(
                f"⚠️ Snap {snap_idx + 1} too large ({sizeof_fmt(file_size)}). "
                f"Max: {MAX_FILE_SIZE_MB}MB."
            )
            # Mark as "picked" anyway — the user got an answer about
            # this snap, even if it was "too big". Saves them from
            # tapping it again expecting different behaviour.
            return True

        caption = _build_snap_caption(username, index, total_out, timestamp)
        with open(file_path, "rb") as f:
            if media_type == "video":
                await query.message.reply_video(f, caption=caption)
            else:
                await query.message.reply_photo(f, caption=caption)
        await notice.delete()
        return True

    except Exception:
        logger.exception(f"Failed to download single snap {snap_idx}")
        try:
            await notice.edit_text(
                f"❌ Failed to download snap {snap_idx + 1}."
            )
        except TelegramError:
            pass
        return False
    finally:
        if file_path:
            await cleanup_files(file_path)


async def _download_and_send_all(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: dict,
) -> None:
    """Bulk download every snap in the story (mirrors pre-v0.1.8 behaviour)."""
    query = update.callback_query
    username = session["username"]
    media_items = session["media_items"]

    notice = await query.message.reply_text(
        f"⬇️ Downloading all {len(media_items)} snap(s)..."
    )
    downloaded: list[tuple[str, str, int, int, int]] = []
    try:
        downloaded = await download_story_media(media_items, username)
        if not downloaded:
            await notice.edit_text("❌ Could not download any story items.")
            return

        await notice.edit_text(f"📤 Sending {len(downloaded)} item(s)...")

        for file_path, media_type, index, total, timestamp in downloaded:
            file_size = os.path.getsize(file_path)
            if file_size > MAX_FILE_SIZE_BYTES:
                await query.message.reply_text(
                    f"⚠️ Skipped one item — too large ({sizeof_fmt(file_size)})."
                )
                continue
            caption = _build_snap_caption(username, index, total, timestamp)
            try:
                with open(file_path, "rb") as f:
                    if media_type == "video":
                        await query.message.reply_video(f, caption=caption)
                    else:
                        await query.message.reply_photo(f, caption=caption)
            except TelegramError as e:
                logger.error(f"Failed to send story item: {e}")
                await query.message.reply_text("❌ Failed to send one item.")

        await notice.delete()
    except Exception:
        logger.exception("Failed bulk Snapchat download")
        try:
            await notice.edit_text("❌ An unexpected error occurred.")
        except TelegramError:
            pass
    finally:
        await cleanup_files(*[t[0] for t in downloaded])


async def handle_quality_picker(
    update: Update, context: ContextTypes.DEFAULT_TYPE, platform: str, url: str
) -> None:
    label = PLATFORM_LABELS[platform]
    status = await update.message.reply_text(f"🔍 Fetching available qualities from *{label}*...", parse_mode=ParseMode.MARKDOWN)

    try:
        quality_options, title, duration = await fetch_formats(url)
    except Exception:
        logger.exception(f"Failed to fetch formats for {url}")
        await status.edit_text("❌ Could not fetch video info. The URL may be invalid or unsupported.")
        return

    if not quality_options:
        # No quality info — fall back to auto-best
        await status.edit_text("⬇️ No quality info found, downloading best available...")
        try:
            file_path, title = await download_media(url)
            await _send_video(update, file_path, title, status)
        except Exception:
            # Full details are in the log; don't echo internals (paths,
            # URLs, extractor output) into the chat.
            logger.exception(f"Fallback download failed for {url}")
            await status.edit_text("❌ Download failed. Check bot logs for details.")
        return

    # Telegram callback_data has a 64-byte limit, so we can't fit the URL.
    # Store URL + title in bot_data keyed by user+message ID, and put just the
    # short key in callback_data.
    key = f"dl_{update.effective_user.id}_{update.message.message_id}"
    if "pending_downloads" not in context.bot_data:
        context.bot_data["pending_downloads"] = {}
    context.bot_data["pending_downloads"][key] = {
        "url": url,
        "title": title,
    }

    # Build inline keyboard
    buttons = []
    for opt in quality_options:
        size_str = sizeof_fmt(opt["filesize"])
        btn_label = f"{opt['label']} • {size_str}"
        callback_data = f"dl|{key}|{opt['format_id']}"
        buttons.append([InlineKeyboardButton(btn_label, callback_data=callback_data)])

    # Add "Best" option
    buttons.append([InlineKeyboardButton("⭐ Best available", callback_data=f"dl|{key}|best")])

    markup = InlineKeyboardMarkup(buttons)
    mins, secs = divmod(int(duration or 0), 60)
    duration_str = f"{mins}:{secs:02d}" if duration else "?"

    await status.edit_text(
        f"🎬 *{escape_markdown(title)}*\n⏱ Duration: `{duration_str}`\n\nSelect quality:",
        reply_markup=markup,
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_auto_download(
    update: Update, context: ContextTypes.DEFAULT_TYPE, platform: str, url: str
) -> None:
    label = PLATFORM_LABELS[platform]
    status = await update.message.reply_text(f"⬇️ Downloading from *{label}*...", parse_mode=ParseMode.MARKDOWN)

    try:
        file_path, title = await download_media(url)
        await _send_video(update, file_path, title, status)
    except Exception:
        logger.exception(f"Auto download failed for {url}")
        await status.edit_text("❌ Download failed. Check bot logs for details.")


async def handle_quality_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle quality picker button press."""
    query = update.callback_query
    # Auth guard on every entry point (see handle_snapchat_callback).
    if not is_allowed(update.effective_user.id):
        await query.answer("Not authorized", show_alert=False)
        return
    await query.answer()

    try:
        _, key, format_id = query.data.split("|", 2)
    except ValueError:
        await query.edit_message_text("❌ Invalid selection.")
        return

    pending = context.bot_data.get("pending_downloads", {}).get(key)
    if not pending:
        await query.edit_message_text("❌ Session expired. Please send the link again.")
        return

    url = pending["url"]
    title = pending["title"]
    actual_format = None if format_id == "best" else format_id

    await query.edit_message_text(f"⬇️ Downloading *{escape_markdown(title)}*...", parse_mode=ParseMode.MARKDOWN)

    try:
        file_path, title = await download_media(url, actual_format)
        await _send_video(update, file_path, title, query.message)
    except Exception:
        logger.exception(f"Quality download failed for {url}")
        await query.edit_message_text("❌ Download failed. Check bot logs for details.")
    finally:
        # Clean up pending entry
        context.bot_data.get("pending_downloads", {}).pop(key, None)


async def _send_video(
    update: Update, file_path: str, title: str, status_msg
) -> None:
    """Send downloaded video to user and clean up."""
    try:
        file_size = os.path.getsize(file_path)

        if file_size > MAX_FILE_SIZE_BYTES:
            await status_msg.edit_text(
                f"⚠️ File too large to send ({sizeof_fmt(file_size)}). "
                f"Max allowed: {MAX_FILE_SIZE_MB}MB."
            )
            return

        await status_msg.edit_text(f"📤 Uploading *{escape_markdown(title)}*...", parse_mode=ParseMode.MARKDOWN)

        # Reply to the original message if available
        target = update.message or update.callback_query.message
        with open(file_path, "rb") as f:
            await target.reply_video(
                f,
                caption=f"🎬 {title}",
                supports_streaming=True,
            )

        await status_msg.delete()

    except TelegramError as e:
        logger.error(f"Failed to send video: {e}")
        await status_msg.edit_text("❌ Upload failed. Check bot logs for details.")
    finally:
        await cleanup_files(file_path)


# ── Global error handler ──────────────────────────────────────────────────────

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Catch-all for exceptions that escape individual handlers.

    Without this, ptb's default behaviour is to log the exception under its
    own logger name (`telegram.ext.Application`) at ERROR level — but only
    a one-line summary, not the full traceback. We saw exactly that gap in
    v0.1.4: a Snapchat handler exception produced an error message in the
    user's Telegram client but left NO usable trace in `docker compose
    logs bot`. This handler ensures every unhandled exception is logged
    with full traceback under the `__main__` logger.
    """
    logger.exception("Unhandled exception in handler", exc_info=context.error)


# ── App bootstrap ─────────────────────────────────────────────────────────────

def main() -> None:
    builder = Application.builder().token(BOT_TOKEN)

    if LOCAL_API_URL:
        builder = builder.base_url(f"{LOCAL_API_URL}/bot")
        builder = builder.base_file_url(f"{LOCAL_API_URL}/file/bot")
        logger.info(f"Using local Bot API: {LOCAL_API_URL}")

    app = builder.build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(handle_quality_callback, pattern=r"^dl\|"))
    app.add_handler(CallbackQueryHandler(handle_snapchat_callback, pattern=r"^sc\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(on_error)

    logger.info(f"Bot started (media-dl-bot v{__version__}).")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
