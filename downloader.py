"""
Media downloader (yt-dlp wrapper).
"""

import os
import uuid
import asyncio
import logging
from typing import Optional
import yt_dlp

from utils import get_downloads_dir

logger = logging.getLogger(__name__)

# Platforms that typically have multiple quality options worth presenting.
# All other supported platforms (TikTok, Instagram) auto-pick the best stream.
QUALITY_PICKER_PLATFORMS = {"youtube", "facebook", "twitch", "twitter"}


def _build_ydl_opts(output_template: str, format_id: Optional[str] = None) -> dict:
    """Build yt-dlp options."""
    opts = {
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "merge_output_format": "mp4",
    }

    if format_id:
        # User picked a specific quality. On YouTube and similar sites the
        # format id usually refers to a video-only stream, so we ask yt-dlp
        # to merge the best available audio with it. The `+ba/...` fallback
        # chain handles platforms (Twitter HLS, Twitch) where the format
        # already contains audio — yt-dlp will pick the first viable branch.
        opts["format"] = (
            f"{format_id}+bestaudio/"
            f"{format_id}+ba/"
            f"{format_id}"
        )
    else:
        # Default: best video+audio (TikTok, Instagram, fallback path).
        opts["format"] = "bestvideo*+bestaudio/best"

    return opts


async def fetch_formats(url: str) -> tuple[list[dict], str, int]:
    """
    Fetch available formats for a URL.
    Returns (quality_options, title, duration).
    """
    loop = asyncio.get_event_loop()

    def _fetch():
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            info = ydl.extract_info(url, download=False)
            return info

    try:
        info = await loop.run_in_executor(None, _fetch)
    except Exception as e:
        logger.error(f"Failed to fetch formats for {url}: {e}")
        raise

    formats = info.get("formats", [])
    title = info.get("title", "Unknown")
    duration = info.get("duration", 0)

    # Filter to video formats with height info, deduplicate by resolution
    seen_heights = set()
    quality_options = []

    for f in reversed(formats):  # reversed = best first
        height = f.get("height")
        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")

        if not height or vcodec == "none":
            continue
        if height in seen_heights:
            continue

        seen_heights.add(height)
        filesize = f.get("filesize") or f.get("filesize_approx")

        quality_options.append({
            "format_id": f["format_id"],
            "height": height,
            "ext": f.get("ext", "mp4"),
            "filesize": filesize,
            "label": f"{height}p",
            "has_audio": acodec != "none",
        })

    # Sort by height descending
    quality_options.sort(key=lambda x: x["height"], reverse=True)

    # Keep top 5 to avoid button overflow
    quality_options = quality_options[:5]

    return quality_options, title, duration


def _resolve_downloaded_path(info: dict, fallback_template: str) -> Optional[str]:
    """
    Find the actual file yt-dlp wrote to disk.

    yt-dlp populates `requested_downloads` after download with the real
    filepath(s) — that's the authoritative source. We fall back to scanning
    common video extensions only if `requested_downloads` is missing
    (older yt-dlp versions or unusual extractors).
    """
    requested = info.get("requested_downloads") or []
    for entry in requested:
        path = entry.get("filepath")
        if path and os.path.exists(path):
            return path

    # Legacy fallback: derive from the prepare_filename template and try
    # common video extensions. We use a wide list because some extractors
    # (e.g. Twitter HLS) produce mp4 directly while others go through
    # webm/mkv first.
    base = os.path.splitext(fallback_template)[0]
    for ext in ("mp4", "mkv", "webm", "mov", "m4v", "ts"):
        candidate = f"{base}.{ext}"
        if os.path.exists(candidate):
            return candidate

    return None


async def download_media(url: str, format_id: Optional[str] = None) -> tuple[str, str]:
    """
    Download media from URL.
    Returns (file_path, title).
    """
    downloads_dir = get_downloads_dir()
    # Unique per-download prefix prevents collisions when two users request the
    # same video at the same time (yt-dlp would otherwise reuse %(id)s.%(ext)s).
    unique = uuid.uuid4().hex[:8]
    output_template = str(downloads_dir / f"{unique}_%(id)s.%(ext)s")

    opts = _build_ydl_opts(output_template, format_id)
    loop = asyncio.get_event_loop()

    def _download():
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            fallback = ydl.prepare_filename(info)
            path = _resolve_downloaded_path(info, fallback)
            if not path:
                raise FileNotFoundError(
                    f"Download appeared to succeed but no output file was found "
                    f"(template: {fallback})"
                )
            return path, info.get("title", "Unknown")

    try:
        file_path, title = await loop.run_in_executor(None, _download)
        logger.info(f"Downloaded: {file_path} ({title})")
        return file_path, title
    except Exception as e:
        logger.error(f"Download failed for {url}: {e}")
        raise


def needs_quality_picker(platform: str) -> bool:
    return platform in QUALITY_PICKER_PLATFORMS
