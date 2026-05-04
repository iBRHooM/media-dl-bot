"""
Shared utilities: platform detection, file cleanup, size formatting.
"""

import os
import re
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

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

    # Check URL patterns
    for platform, pattern in URL_PATTERNS.items():
        if pattern.search(text):
            return platform, text

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
