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
        "postprocessors": [
            {
                "key": "FFmpegVideoConvertor",
                "preferedformat": "mp4",
            }
        ],
    }

    if format_id:
        opts["format"] = format_id
    else:
        # Best video+audio, no watermark for TikTok
        opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"

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
            filename = ydl.prepare_filename(info)
            # yt-dlp may change extension after merge
            base = os.path.splitext(filename)[0]
            for ext in ["mp4", "mkv", "webm", "mov"]:
                candidate = f"{base}.{ext}"
                if os.path.exists(candidate):
                    return candidate, info.get("title", "Unknown")
            return filename, info.get("title", "Unknown")

    try:
        file_path, title = await loop.run_in_executor(None, _download)
        logger.info(f"Downloaded: {file_path} ({title})")
        return file_path, title
    except Exception as e:
        logger.error(f"Download failed for {url}: {e}")
        raise


def needs_quality_picker(platform: str) -> bool:
    return platform in QUALITY_PICKER_PLATFORMS
