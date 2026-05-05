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

import json
import logging
import aiohttp
import aiofiles
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


def _classify_snap(item: dict, media_url: str) -> str:
    """
    Determine if a snap is video or photo.

    Snapchat sometimes provides a `mediaType` field; otherwise we infer
    from the URL/extension hints.
    """
    declared = (item.get("mediaType") or "").upper()
    if declared == "VIDEO":
        return "video"
    if declared in ("IMAGE", "PHOTO"):
        return "photo"

    lowered = media_url.lower()
    if any(t in lowered for t in (".mp4", "video", "dfmedia")):
        return "video"
    if any(t in lowered for t in (".jpg", ".jpeg", ".png", ".webp")):
        return "photo"
    # Snapchat default: most modern stories are video.
    return "video"


def _extract_stories_from_next_data(data: dict) -> list[dict]:
    """
    Walk __NEXT_DATA__ to find the `snapList` array — i.e. the active
    24-hour stories. The exact JSON path varies between profile types
    (creator / business / personal), so we recursively search for any
    object that has a `snapList` key.

    Returns items shaped for download_story_media:
        [{ "url": str, "type": "video"|"photo", "index": int }, ...]
    """
    snaps_raw: list[dict] = []

    def _walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("snapList"), list):
                snaps_raw.extend(node["snapList"])
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(data)

    # Dedupe by mediaUrl in case multiple snapList instances overlap.
    seen: set[str] = set()
    items: list[dict] = []
    for raw in snaps_raw:
        media_url = raw.get("mediaUrl") or raw.get("snapMediaUrl")
        if not media_url or not isinstance(media_url, str):
            continue
        if not media_url.startswith("http"):
            continue
        if media_url in seen:
            continue
        seen.add(media_url)
        items.append({
            "url": media_url,
            "type": _classify_snap(raw, media_url),
            "index": raw.get("snapIndex", len(items)),
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
    media_items: list[dict] = []
    found_profile = False

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
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
                try:
                    response = await page.goto(
                        profile_url,
                        timeout=TIMEOUT_MS,
                        wait_until="domcontentloaded",
                    )
                except Exception as e:
                    logger.warning(
                        f"Could not load {profile_url}: {e}"
                    )
                    continue

                if response and response.status == 404:
                    # Profile doesn't exist on this URL pattern — try the next.
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
                        f"No __NEXT_DATA__ found at {profile_url}"
                    )
                    continue

                try:
                    data = json.loads(next_data_raw)
                except json.JSONDecodeError as e:
                    logger.warning(
                        f"Could not parse __NEXT_DATA__ at {profile_url}: {e}"
                    )
                    continue

                media_items = _extract_stories_from_next_data(data)
                if media_items:
                    logger.info(
                        f"Found {len(media_items)} active stories for "
                        f"@{username}"
                    )
                    break  # success — no need to try the other URL pattern
                # Page loaded but no snapList → user has no active stories.
                # Don't try the fallback pattern; the answer is the same.
                break

        finally:
            await browser.close()

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


async def download_story_media(
    media_items: list[dict], username: str
) -> list[tuple[str, str]]:
    """
    Download story media files. Returns list of (file_path, media_type) tuples.
    """
    downloads_dir = get_downloads_dir()
    downloaded: list[tuple[str, str]] = []

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.snapchat.com/",
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        for item in media_items:
            url = item["url"]
            media_type = item["type"]
            index = item["index"]
            ext = "mp4" if media_type == "video" else "jpg"
            # Include media_type in filename: video and photo can share an index.
            filename = downloads_dir / f"snap_{username}_{media_type}_{index}.{ext}"

            try:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=60)
                ) as resp:
                    if resp.status != 200:
                        logger.warning(
                            f"Failed to download story item {index}: "
                            f"HTTP {resp.status}"
                        )
                        continue
                    async with aiofiles.open(filename, "wb") as f:
                        async for chunk in resp.content.iter_chunked(1024 * 64):
                            await f.write(chunk)
                downloaded.append((str(filename), media_type))
                logger.debug(f"Downloaded story item {index}: {filename}")
            except Exception as e:
                logger.warning(
                    f"Failed to download story item {index}: {e}"
                )
                continue

    return downloaded
