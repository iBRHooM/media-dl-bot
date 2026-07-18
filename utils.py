"""
Shared utilities: platform detection, file cleanup, size formatting.
"""

import os
import re
import time
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Any file in the downloads dir older than this is debris from a crash or
# restart — the normal flow deletes each file right after sending it.
# mtime refreshes on every write, so actively-downloading files (even huge
# multi-hour VOD downloads) are never considered stale.
DOWNLOADS_STALE_AGE_SECONDS = 3600

# Supported URL patterns per platform
URL_PATTERNS = {
    "youtube": re.compile(
        r"(https?://)?(www\.)?(youtube\.com/(watch\?v=|shorts/)|youtu\.be/)\S+"
    ),
    "tiktok": re.compile(r"(https?://)?(www\.|vm\.)?tiktok\.com/\S+"),
    "twitter": re.compile(r"(https?://)?(www\.)?(twitter\.com|x\.com)/\S+/status/\d+"),
    "facebook": re.compile(r"(https?://)?(www\.|m\.)?facebook\.com/\S+"),
    "instagram": re.compile(r"(https?://)?(www\.)?instagram\.com/(p|reel|tv)/\S+"),
    "twitch": re.compile(r"(https?://)?(www\.)?twitch\.tv/\S+"),
}

# Snapchat username command pattern: "snapchat <username>"
SNAPCHAT_PATTERN = re.compile(r"^snapchat\s+([a-zA-Z0-9._-]+)$", re.IGNORECASE)


def detect_platform(text: str) -> tuple[str, str] | tuple[None, None]:
    """
    Detect platform from text.
    Returns (platform, url_or_username) or (None, None).
    """
    text = text.strip()

    # Check for snapchat username command
    match = SNAPCHAT_PATTERN.match(text)
    if match:
        return "snapchat", match.group(1)

    # Check URL patterns. Return only the matched URL substring — never
    # the full message text. Passing the whole text let a crafted message
    # (e.g. "https://evil.example/#facebook.com/x", where the platform
    # pattern matches inside the fragment) hand an arbitrary URL to
    # yt-dlp's generic extractor.
    for platform, pattern in URL_PATTERNS.items():
        match = pattern.search(text)
        if match:
            return platform, match.group(0)

    return None, None


def sizeof_fmt(num_bytes: float | None) -> str:
    """Human-readable file size. Returns '?' when size is unknown."""
    if num_bytes is None:
        return "?"
    size = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def escape_markdown(text: str) -> str:
    """
    Escape Telegram legacy-Markdown special characters so user-supplied or
    third-party strings (usernames, video titles) don't break message parsing.

    Telegram legacy Markdown reserves: * _ ` [
    """
    if not text:
        return ""
    for char in ("\\", "*", "_", "`", "["):
        text = text.replace(char, f"\\{char}")
    return text


async def cleanup_files(*paths: str) -> None:
    """Remove downloaded temp files."""
    for path in paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
                logger.debug(f"Cleaned up: {path}")
        except Exception as e:
            logger.warning(f"Failed to clean up {path}: {e}")


def get_downloads_dir() -> Path:
    """Return the downloads directory, ensuring it exists."""
    path = Path("/app/downloads")
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_max_file_size_bytes() -> int:
    """
    Upload/download size limit in bytes, from the MAX_FILE_SIZE_MB env var
    (default 1900). Shared by the upload check in main.py and the
    pre-download caps in downloader.py / snapchat.py so they can't drift.
    """
    return int(os.environ.get("MAX_FILE_SIZE_MB", 1900)) * 1024 * 1024


def cleanup_stale_downloads(
    max_age_seconds: int = DOWNLOADS_STALE_AGE_SECONDS,
) -> None:
    """
    Remove orphaned files from the downloads dir.

    Called opportunistically at the start of each download (same
    no-background-task pattern as the Snapchat session purge). Guards
    against disk fill from files left behind by crashes or restarts.
    """
    now = time.time()
    try:
        entries = list(get_downloads_dir().iterdir())
    except OSError as e:
        logger.warning(f"Stale-download sweep failed to list dir: {e}")
        return
    for p in entries:
        try:
            if p.is_file() and now - p.stat().st_mtime > max_age_seconds:
                p.unlink()
                logger.info(f"Removed stale download: {p.name}")
        except OSError as e:
            logger.warning(f"Could not remove stale file {p}: {e}")
