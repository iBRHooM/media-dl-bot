"""
Snapchat public-profile story scraper (Playwright + aiohttp).

Snapchat moved away from `story.snapchat.com/<username>`. Public profiles
now live at `snapchat.com/@<username>` (and `snapchat.com/add/<username>`,
which redirects to the same page).

The page is a Next.js SPA. We extract the embedded `__NEXT_DATA__` JSON
and pull the `snapList` array — the 24-hour stories. We deliberately
ignore Spotlight (durable saved videos) and Lenses; those are different
content types under different keys.

Each item in `snapList` looks like:
    {
        "snapIndex": 0,
        "createTime": "2024-10-16T19:01:13.000Z",
        "mediaPreviewUrl": ".../EgLargeThumbnail",  -- thumbnail
        "mediaUrl":        ".../DfMedia",            -- real media file
        "mediaType":       "VIDEO" | "IMAGE"  (sometimes absent)
    }
"""

import asyncio
import io
import json
import logging
import uuid
import aiohttp
import aiofiles
from PIL import Image, ImageDraw, ImageFont
from playwright.async_api import async_playwright

from utils import get_downloads_dir

logger = logging.getLogger(__name__)

# Try @username first (current canonical), fall back to /add/username (legacy
# but still valid as a redirect target).
PROFILE_URL_PATTERNS = (
    "https://www.snapchat.com/@{username}",
    "https://www.snapchat.com/add/{username}",
)
TIMEOUT_MS = 30_000


def _classify_snap(snap: dict) -> str:
    """
    Determine if a snap is video or photo from `snapMediaType`.

    Verified enum (against snapchat.com/@<username> __NEXT_DATA__ on
    multiple profile types):
      0 = IMAGE / photo
      1 = VIDEO

    Unknown enum values fall back to "video" since most modern stories
    are video — and we log so the mapping can be extended if Snapchat
    introduces new types.
    """
    media_type = snap.get("snapMediaType")
    if media_type == 0:
        return "photo"
    if media_type == 1:
        return "video"
    logger.warning(
        f"Unknown snapMediaType={media_type!r} — defaulting to video"
    )
    return "video"


def _extract_stories_from_next_data(data: dict) -> list[dict]:
    """
    Read the active 24-hour story list from Snapchat's __NEXT_DATA__.

    Verified path (v0.1.6):
        data["props"]["pageProps"]["story"]["snapList"]

    Each item:
        {
            "snapIndex": 0,
            "snapMediaType": 1,                  # 0=IMAGE, 1=VIDEO
            "snapUrls": {
                "mediaUrl": "https://cf-st.sc-cdn.net/...",
                "mediaPreviewUrl": {"value": "..."},  # 256px thumbnail
                "overlayUrl": null,
                "attachmentUrl": null
            },
            ...
        }

    Returns items shaped for download_story_media:
        [{ "url": str, "type": "video"|"photo", "index": int }, ...]

    Notes on what we deliberately skip:
      - `pageProps.curatedHighlights[*].snapList` and
        `pageProps.spotlightHighlights[*].snapList` — these are durable
        saved content, not 24-hour stories. Out of scope.
      - `mediaPreviewUrl` — that's a 256px thumbnail; we want the full
        media URL.
    """
    page_props = data.get("props", {}).get("pageProps", {})
    story = page_props.get("story")

    if not isinstance(story, dict):
        logger.warning(
            f"Snapchat: pageProps.story is missing or wrong type "
            f"({type(story).__name__}). Schema may have changed."
        )
        return []

    snap_list = story.get("snapList")
    if not isinstance(snap_list, list):
        logger.warning(
            f"Snapchat: pageProps.story.snapList is missing or wrong type "
            f"({type(snap_list).__name__}). Schema may have changed."
        )
        return []

    logger.info(f"Snapchat: pageProps.story.snapList has {len(snap_list)} items")

    items: list[dict] = []
    total = len(snap_list)
    for snap in snap_list:
        if not isinstance(snap, dict):
            continue

        snap_urls = snap.get("snapUrls")
        if not isinstance(snap_urls, dict):
            continue

        media_url = snap_urls.get("mediaUrl")
        if not isinstance(media_url, str) or not media_url.startswith("http"):
            continue

        # `timestampInSec` is a nested {"value": "<unix_seconds_string>"}.
        # Defensive parse — Snapchat occasionally returns it as a plain
        # int or omits it entirely on older snaps.
        ts_raw = snap.get("timestampInSec")
        timestamp = 0
        if isinstance(ts_raw, dict):
            try:
                timestamp = int(ts_raw.get("value", 0))
            except (TypeError, ValueError):
                timestamp = 0
        elif isinstance(ts_raw, (int, str)):
            try:
                timestamp = int(ts_raw)
            except (TypeError, ValueError):
                timestamp = 0

        # 256px thumbnail used for the grid preview. Nested as
        # {"value": "https://..."} in the schema; missing on rare snaps
        # (we fall back to the full media URL if absent, which works but
        # downloads the entire video for one frame — acceptable as
        # a last resort).
        preview_node = snap_urls.get("mediaPreviewUrl")
        preview_url = ""
        if isinstance(preview_node, dict):
            value = preview_node.get("value")
            if isinstance(value, str) and value.startswith("http"):
                preview_url = value
        elif isinstance(preview_node, str) and preview_node.startswith("http"):
            preview_url = preview_node

        items.append({
            "url": media_url,
            "preview_url": preview_url,
            "type": _classify_snap(snap),
            "index": snap.get("snapIndex", len(items)),
            "total": total,
            "timestamp": timestamp,
        })

    return items


async def fetch_snapchat_stories(username: str) -> list[dict]:
    """
    Scrape active public stories for a Snapchat username.

    Returns a list of dicts: [{ url, type, index }, ...].
    Raises ValueError if the profile is missing, private, or has no
    active stories.
    Raises RuntimeError on unexpected scraping failures.
    """
    logger.info(f"Snapchat: starting scrape for @{username}")
    media_items: list[dict] = []
    found_profile = False

    try:
        async with async_playwright() as pw:
            # No --no-sandbox: the container runs as non-root (pwuser) and
            # docker-compose loads seccomp_profile.json, which permits the
            # user-namespace syscalls Chromium's own sandbox needs. Untrusted
            # page content is rendered here, so the sandbox must stay on.
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1280, "height": 1800},
                )
                page = await context.new_page()

                for pattern in PROFILE_URL_PATTERNS:
                    profile_url = pattern.format(username=username)
                    logger.info(f"Snapchat: trying {profile_url}")
                    try:
                        response = await page.goto(
                            profile_url,
                            timeout=TIMEOUT_MS,
                            wait_until="domcontentloaded",
                        )
                    except Exception as e:
                        logger.warning(
                            f"Snapchat: page.goto failed for {profile_url}: {e}"
                        )
                        continue

                    status_code = response.status if response else None
                    logger.info(
                        f"Snapchat: {profile_url} returned HTTP {status_code}"
                    )

                    if response and response.status == 404:
                        # Profile doesn't exist on this URL pattern — try
                        # the next.
                        continue

                    found_profile = True

                    next_data_raw = await page.evaluate(
                        """
                        () => {
                            const el = document.getElementById('__NEXT_DATA__');
                            return el ? el.textContent : null;
                        }
                        """
                    )

                    if not next_data_raw:
                        logger.warning(
                            f"Snapchat: no __NEXT_DATA__ found at {profile_url}"
                        )
                        continue

                    logger.info(
                        f"Snapchat: __NEXT_DATA__ size = "
                        f"{len(next_data_raw)} chars"
                    )

                    try:
                        data = json.loads(next_data_raw)
                    except json.JSONDecodeError as e:
                        logger.warning(
                            f"Snapchat: could not parse __NEXT_DATA__ at "
                            f"{profile_url}: {e}"
                        )
                        continue

                    media_items = _extract_stories_from_next_data(data)
                    if media_items:
                        logger.info(
                            f"Snapchat: found {len(media_items)} active "
                            f"stories for @{username}"
                        )
                        # Success — no need to try the other URL pattern.
                        break
                    # Page loaded but no snapList → user has no active
                    # stories. Don't try the fallback pattern; the answer
                    # is the same.
                    logger.info(
                        f"Snapchat: no stories extracted at {profile_url}; "
                        f"not falling through to other URL patterns"
                    )
                    break

            finally:
                await browser.close()
    except Exception:
        # Re-raise as RuntimeError so the caller's `except (ValueError,
        # RuntimeError)` branch catches it cleanly. Full traceback is
        # already logged by main.py's global error handler if this leaks.
        logger.exception(f"Snapchat: scraping crashed for @{username}")
        raise RuntimeError(
            f"Snapchat scraping failed for '{username}'. Check bot logs "
            f"for details."
        )

    if not found_profile:
        raise ValueError(
            f"Snapchat profile '{username}' not found. "
            f"Check the spelling and try again."
        )

    if not media_items:
        raise ValueError(
            f"No active stories found for '{username}'. The profile may be "
            f"private, have no stories posted in the last 24 hours, or only "
            f"contain Spotlight content (which this bot does not download)."
        )

    return media_items


_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.snapchat.com/",
}


async def _download_single_snap(
    session: "aiohttp.ClientSession",
    item: dict,
    username: str,
    total: int,
) -> tuple[str, str, int, int, int] | None:
    """
    Download one snap. Returns the result tuple or None on failure.

    Extracted into its own function so the per-snap picker (v0.1.8) and
    the bulk "download all" path can share the same logic.
    """
    url = item["url"]
    media_type = item["type"]
    index = item["index"]
    timestamp = item.get("timestamp", 0)
    ext = "mp4" if media_type == "video" else "jpg"

    downloads_dir = get_downloads_dir()
    # Include media_type in filename: video and photo can share an index.
    # The uuid prefix makes the path unique per download (same pattern as
    # downloader.py): without it, concurrent requests for the same
    # username collide on one path, and cleanup_files() in one request's
    # `finally` can delete the file another request is still sending.
    unique = uuid.uuid4().hex[:8]
    filename = (
        downloads_dir / f"snap_{unique}_{username}_{media_type}_{index}.{ext}"
    )

    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=60)
        ) as resp:
            if resp.status != 200:
                logger.warning(
                    f"Failed to download story item {index}: "
                    f"HTTP {resp.status}"
                )
                return None
            async with aiofiles.open(filename, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024 * 64):
                    await f.write(chunk)
        logger.debug(f"Downloaded story item {index}: {filename}")
        return (str(filename), media_type, index, total, timestamp)
    except Exception as e:
        logger.warning(f"Failed to download story item {index}: {e}")
        return None


async def download_one_snap(
    item: dict, username: str, total: int
) -> tuple[str, str, int, int, int] | None:
    """
    Download a single snap. Used by the v0.1.8 grid picker after the
    user taps a number button.

    `total` is passed in (not derived from the single item) so the
    caption can read "3 of 7" using the original story length, not
    "3 of 1".
    """
    async with aiohttp.ClientSession(headers=_HTTP_HEADERS) as session:
        return await _download_single_snap(session, item, username, total)


async def download_story_media(
    media_items: list[dict], username: str
) -> list[tuple[str, str, int, int, int]]:
    """
    Download every snap in the story (bulk path, for "Download all").

    Returns a list of (file_path, media_type, index, total, timestamp)
    tuples. `index` is the snap's `snapIndex` (Snapchat's own 0-based
    numbering for the story), `total` is the total number of snaps in
    the story (so captions can read "3 of 7" honestly even if some
    snaps failed to download), and `timestamp` is the Unix-epoch
    seconds when the snap was posted (0 if Snapchat didn't return one).
    """
    downloaded: list[tuple[str, str, int, int, int]] = []
    total = len(media_items)

    async with aiohttp.ClientSession(headers=_HTTP_HEADERS) as session:
        for item in media_items:
            result = await _download_single_snap(session, item, username, total)
            if result is not None:
                downloaded.append(result)

    return downloaded


# ─── Grid builder (v0.1.8) ────────────────────────────────────────────────────

# Grid layout constants. 4 rows × 3 cols = 12 snaps per page, which keeps
# the rendered image under ~1 MB and the inline-keyboard button count below
# Telegram's per-message limit (~100). Cells use Snapchat's vertical
# 9:16 aspect ratio scaled to 256×456 — matches the native thumbnail size,
# so no upscaling.
GRID_COLS = 3
GRID_ROWS = 4
GRID_PAGE_SIZE = GRID_COLS * GRID_ROWS  # 12
CELL_W = 256
CELL_H = 456
CELL_GAP = 8
GRID_BG = (20, 20, 20)          # near-black, matches Telegram dark theme
NUMBER_FG = (255, 255, 255)
NUMBER_OUTLINE = (0, 0, 0)


async def _fetch_thumbnail_bytes(
    session: "aiohttp.ClientSession", url: str
) -> bytes | None:
    """Fetch a single thumbnail. Returns raw bytes or None on failure."""
    if not url:
        return None
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            if resp.status != 200:
                logger.warning(f"Thumbnail HTTP {resp.status} for {url[:80]}")
                return None
            return await resp.read()
    except Exception as e:
        logger.warning(f"Thumbnail fetch failed for {url[:80]}: {e}")
        return None


def _load_number_font(size: int) -> ImageFont.ImageFont:
    """
    Try a TrueType font for the number overlay; fall back to Pillow's
    default bitmap if not present. The Playwright Ubuntu base image
    ships DejaVu so the TTF path normally hits.
    """
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _draw_number(draw: ImageDraw.ImageDraw, n: int, x: int, y: int) -> None:
    """
    Draw a snap number at (x, y) — white text with a thick black outline
    so it reads against any thumbnail content. A semi-opaque rounded
    background pill sits behind the digits for extra contrast.
    """
    font = _load_number_font(48)
    text = str(n)
    try:
        bbox = draw.textbbox((x, y), text, font=font, stroke_width=3)
        pad = 6
        draw.rectangle(
            [bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad],
            fill=(0, 0, 0, 180),
        )
    except AttributeError:
        # Very old Pillow — skip the background, outline alone is fine.
        pass
    draw.text(
        (x, y),
        text,
        fill=NUMBER_FG,
        font=font,
        stroke_width=3,
        stroke_fill=NUMBER_OUTLINE,
    )


def _build_grid_image(
    thumbnails: list[bytes | None], start_index: int
) -> bytes:
    """
    Compose a grid PNG from a list of thumbnail byte-strings (some
    entries may be None when a thumbnail fetch failed — those become
    blank placeholders so the numbering stays consistent).

    `start_index` is the snap number for the first cell (1-based, for
    display). E.g. on page 2 of a 30-snap story, start_index = 13.

    Layout is left-to-right, top-to-bottom. The last page only renders
    as many rows as needed (no blank trailing rows).
    """
    n_cells = len(thumbnails)
    rows_needed = (n_cells + GRID_COLS - 1) // GRID_COLS
    width = GRID_COLS * CELL_W + (GRID_COLS + 1) * CELL_GAP
    height = rows_needed * CELL_H + (rows_needed + 1) * CELL_GAP

    canvas = Image.new("RGB", (width, height), GRID_BG)
    draw = ImageDraw.Draw(canvas, "RGBA")

    for i, thumb_bytes in enumerate(thumbnails):
        col = i % GRID_COLS
        row = i // GRID_COLS
        x = CELL_GAP + col * (CELL_W + CELL_GAP)
        y = CELL_GAP + row * (CELL_H + CELL_GAP)

        if thumb_bytes:
            try:
                thumb = Image.open(io.BytesIO(thumb_bytes)).convert("RGB")
                # Cover the cell — preserve aspect, crop excess.
                thumb_ratio = thumb.width / thumb.height
                cell_ratio = CELL_W / CELL_H
                if thumb_ratio > cell_ratio:
                    new_w = int(thumb.height * cell_ratio)
                    left = (thumb.width - new_w) // 2
                    thumb = thumb.crop((left, 0, left + new_w, thumb.height))
                else:
                    new_h = int(thumb.width / cell_ratio)
                    top = (thumb.height - new_h) // 2
                    thumb = thumb.crop((0, top, thumb.width, top + new_h))
                thumb = thumb.resize((CELL_W, CELL_H), Image.LANCZOS)
                canvas.paste(thumb, (x, y))
            except Exception as e:
                logger.warning(f"Thumbnail decode failed: {e}")
                # Fall through to placeholder.

        # Draw the snap number in the top-left of the cell.
        _draw_number(draw, start_index + i, x + 10, y + 6)

    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


async def build_story_grid(
    media_items: list[dict], page: int
) -> tuple[bytes, int, int]:
    """
    Build the grid image for one page of the story.

    Returns (png_bytes, page_count, page_size). `page` is 0-based.

    Thumbnail fetches run concurrently to keep response time snappy
    even on 40+ snap stories. Failed thumbnails become blank placeholders
    (with just the number drawn) so the picker still works.
    """
    total = len(media_items)
    page_count = (total + GRID_PAGE_SIZE - 1) // GRID_PAGE_SIZE
    page = max(0, min(page, page_count - 1))
    start = page * GRID_PAGE_SIZE
    end = min(start + GRID_PAGE_SIZE, total)
    page_items = media_items[start:end]

    async with aiohttp.ClientSession(headers=_HTTP_HEADERS) as session:
        thumbnails = await asyncio.gather(
            *(
                _fetch_thumbnail_bytes(session, item.get("preview_url", ""))
                for item in page_items
            )
        )

    png = _build_grid_image(list(thumbnails), start_index=start + 1)
    return png, page_count, GRID_PAGE_SIZE
