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
    for snap in snap_list:
        if not isinstance(snap, dict):
            continue

        snap_urls = snap.get("snapUrls")
        if not isinstance(snap_urls, dict):
            continue

        media_url = snap_urls.get("mediaUrl")
        if not isinstance(media_url, str) or not media_url.startswith("http"):
            continue

        items.append({
            "url": media_url,
            "type": _classify_snap(snap),
            "index": snap.get("snapIndex", len(items)),
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
