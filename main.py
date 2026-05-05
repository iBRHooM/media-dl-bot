"""
Media Downloader Telegram Bot.

Supported:
  - URL-based: YouTube, TikTok, X/Twitter, Facebook, Instagram, Twitch
  - Username-based: Snapchat (type: snapchat <username>)
"""

import os
import logging
from pathlib import Path
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
from snapchat import fetch_snapchat_stories, download_story_media
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "bot.log"),
    ],
)

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

PLATFORM_LABELS = {
    "youtube": "YouTube",
    "tiktok": "TikTok",
    "twitter": "X (Twitter)",
    "facebook": "Facebook",
    "instagram": "Instagram",
    "twitch": "Twitch",
    "snapchat": "Snapchat",
}

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
• `snapchat <username>` — downloads all public stories

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


async def handle_snapchat(
    update: Update, context: ContextTypes.DEFAULT_TYPE, username: str
) -> None:
    status = await update.message.reply_text(f"👻 Fetching stories for *@{escape_markdown(username)}*...", parse_mode=ParseMode.MARKDOWN)
    downloaded = []

    try:
        media_items = await fetch_snapchat_stories(username)
        await status.edit_text(f"⬇️ Downloading {len(media_items)} story item(s)...")
        downloaded = await download_story_media(media_items, username)

        if not downloaded:
            await status.edit_text("❌ Could not download any story items.")
            return

        await status.edit_text(f"📤 Sending {len(downloaded)} item(s)...")

        for file_path, media_type in downloaded:
            file_size = os.path.getsize(file_path)
            if file_size > MAX_FILE_SIZE_BYTES:
                await update.message.reply_text(
                    f"⚠️ Skipped one item — too large ({sizeof_fmt(file_size)})."
                )
                continue
            try:
                with open(file_path, "rb") as f:
                    if media_type == "video":
                        await update.message.reply_video(f, caption=f"👻 @{username}")
                    else:
                        await update.message.reply_photo(f, caption=f"👻 @{username}")
            except TelegramError as e:
                logger.error(f"Failed to send story item: {e}")
                await update.message.reply_text(f"❌ Failed to send one item: {e}")

        await status.delete()

    except (ValueError, RuntimeError) as e:
        await status.edit_text(f"❌ {e}")
    except Exception as e:
        logger.exception(f"Unexpected error for snapchat @{username}")
        await status.edit_text("❌ An unexpected error occurred. Please try again.")
    finally:
        await cleanup_files(*[fp for fp, _ in downloaded])


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
        except Exception as e:
            logger.exception(f"Fallback download failed for {url}")
            await status.edit_text(f"❌ Download failed: {e}")
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
    except Exception as e:
        logger.exception(f"Auto download failed for {url}")
        await status.edit_text(f"❌ Download failed: {e}")


async def handle_quality_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle quality picker button press."""
    query = update.callback_query
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
    except Exception as e:
        logger.exception(f"Quality download failed for {url}")
        await query.edit_message_text(f"❌ Download failed: {e}")
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
        await status_msg.edit_text(f"❌ Upload failed: {e}")
    finally:
        await cleanup_files(file_path)


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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info(f"Bot started (media-dl-bot v{__version__}).")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
