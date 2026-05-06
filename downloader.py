"""
Media downloader (yt-dlp wrapper).
"""

import os
import uuid
import asyncio
import logging
from pathlib import Path
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
        # `noplaylist` doesn't always cover Twitter quote-tweets where both
        # the outer tweet and the quoted tweet have video — yt-dlp may still
        # treat them as a 2-entry playlist. `playlist_items='1'` forces
        # only the first entry (the outer tweet's video) to be downloaded.
        "playlist_items": "1",
        "merge_output_format": "mp4",
        # Twitter's `http-*` format variants ship as progressive mp4 but with
        # `ext=NA` because yt-dlp can't probe the codec without downloading.
        # The FFmpegVideoRemuxer postprocessor renames/remuxes any non-mp4
        # output (including `.NA`) to `.mp4` after the download completes.
        # `merge_output_format` alone doesn't help because no merge happens
        # for single-format http downloads.
        "postprocessors": [
            {
                "key": "FFmpegVideoRemuxer",
                "preferedformat": "mp4",
            }
        ],
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
        # Default selector. Branch order matters — yt-dlp picks the first
        # branch that resolves to a valid stream:
        #
        #   1. HLS video + HLS audio. Twitter's `http-*` progressive variants
        #      report `vcodec=unknown,acodec=unknown,ext=NA`, which causes
        #      `bestvideo*+bestaudio` to either skip them (no codec info) or
        #      mux them into a `.NA` container that yt-dlp can't finalize.
        #      The HLS streams DO have proper codec metadata and merge into
        #      mp4 cleanly via `merge_output_format`.
        #
        #   2. Generic best video + best audio merge — covers YouTube, FB,
        #      Twitch, and most non-Twitter sites.
        #
        #   3. Single-stream best with mp4 preference — TikTok, Instagram,
        #      and any platform that ships progressive mp4 with proper codec
        #      info (so it doesn't fall into the .NA trap).
        #
        #   4. Last resort: yt-dlp's plain `best` selector.
        opts["format"] = (
            "bv*[protocol*=m3u8]+ba[protocol*=m3u8]/"
            "bv*+ba/"
            "b[ext=mp4]/"
            "b"
        )

    return opts


async def fetch_formats(url: str) -> tuple[list[dict], str, int]:
    """
    Fetch available formats for a URL.
    Returns (quality_options, title, duration).
    """
    loop = asyncio.get_event_loop()

    def _fetch():
        # `playlist_items='1'` is critical for Twitter quote-tweets: yt-dlp
        # treats the outer tweet + quoted tweet as a 2-entry playlist even
        # with `noplaylist=True`. Without this, `extract_info` returns a
        # playlist dict (no top-level `formats` field), our format scan
        # finds nothing, and we fall through to the auto-best path —
        # exactly the bug that caused v0.1.4 to still fail on quote-tweets.
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "playlist_items": "1",
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            # If we still got a playlist back (some extractors ignore both
            # noplaylist and playlist_items), unwrap to the first entry.
            if info.get("_type") == "playlist":
                entries = info.get("entries") or []
                if entries:
                    info = entries[0]
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


def _resolve_downloaded_path(
    info: dict,
    fallback_template: str,
    unique_prefix: str,
    downloads_dir: Path,
) -> Optional[str]:
    """
    Find the actual file yt-dlp wrote to disk.

    Resolution order (most authoritative first):
      1. `info["requested_downloads"][i]["filepath"]` — yt-dlp populates
         this AFTER postprocessors run, so it reflects post-remux paths.
      2. Scan the downloads directory for our unique-prefix files. This is
         immune to extension surprises (e.g. Twitter `.NA` outputs, or
         remuxer leaving both `.NA` and `.mp4` behind). When multiple
         matches exist, mp4 wins because that's what we asked for via
         `merge_output_format` and `FFmpegVideoRemuxer`.
      3. Legacy template-based scan — last resort for older yt-dlp
         versions where `requested_downloads` may be absent.
    """
    requested = info.get("requested_downloads") or []
    for entry in requested:
        path = entry.get("filepath")
        if path and os.path.exists(path):
            return path

    # Scan by unique prefix — extension-agnostic. Sort to make selection
    # deterministic when multiple files exist.
    matches = sorted(downloads_dir.glob(f"{unique_prefix}_*"))
    if matches:
        mp4_matches = [m for m in matches if m.suffix.lower() == ".mp4"]
        chosen = mp4_matches[0] if mp4_matches else matches[0]
        return str(chosen)

    # Last-ditch: derive from the prepare_filename template.
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
            path = _resolve_downloaded_path(
                info,
                fallback_template=fallback,
                unique_prefix=unique,
                downloads_dir=downloads_dir,
            )
            if not path:
                raise FileNotFoundError(
                    f"Download appeared to succeed but no output file was found "
                    f"(template: {fallback}, prefix: {unique})"
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
