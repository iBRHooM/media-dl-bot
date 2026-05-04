"""
Snapchat public-story scraper (Playwright + aiohttp).
"""

import logging
import aiohttp
import aiofiles
from playwright.async_api import async_playwright

from utils import get_downloads_dir

logger = logging.getLogger(__name__)

SNAPCHAT_STORY_URL = "https://story.snapchat.com/{username}"
TIMEOUT_MS = 30_000


async def fetch_snapchat_stories(username: str) -> list[dict]:
    """
    Scrape public Snapchat stories for a given username.
    Returns list of dicts with 'url', 'type' (photo/video), 'index'.
    """
    story_url = SNAPCHAT_STORY_URL.format(username=username)
    media_items = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        try:
            response = await page.goto(story_url, timeout=TIMEOUT_MS, wait_until="networkidle")

            if response and response.status == 404:
                raise ValueError(f"Snapchat user '{username}' not found or has no public stories.")

            # Wait for story media elements to appear
            await page.wait_for_selector("video, img[fetchpriority]", timeout=TIMEOUT_MS)

            # Extract video sources
            videos = await page.evaluate("""
                () => {
                    const items = [];
                    document.querySelectorAll('video source, video').forEach((el, i) => {
                        const src = el.src || el.getAttribute('src');
                        if (src && src.startsWith('http')) {
                            items.push({ url: src, type: 'video', index: i });
                        }
                    });
                    return items;
                }
            """)

            # Extract image sources (story snaps)
            images = await page.evaluate("""
                () => {
                    const items = [];
                    document.querySelectorAll('img[fetchpriority]').forEach((el, i) => {
                        const src = el.src || el.getAttribute('src');
                        if (src && src.startsWith('http') && !src.includes('avatar')) {
                            items.push({ url: src, type: 'photo', index: i });
                        }
                    });
                    return items;
                }
            """)

            media_items = videos + images

            if not media_items:
                raise ValueError(f"No stories found for '{username}'. Profile may be private or empty.")

            logger.info(f"Found {len(media_items)} story items for @{username}")

        except ValueError:
            raise
        except Exception as e:
            logger.error(f"Playwright error scraping @{username}: {e}")
            raise RuntimeError(f"Failed to fetch stories for '{username}'. Please try again.")
        finally:
            await browser.close()

    return media_items


async def download_story_media(media_items: list[dict], username: str) -> list[tuple[str, str]]:
    """
    Download story media files.
    Returns list of (file_path, media_type) tuples.
    """
    downloads_dir = get_downloads_dir()
    downloaded = []

    async with aiohttp.ClientSession() as session:
        for item in media_items:
            url = item["url"]
            media_type = item["type"]
            index = item["index"]
            ext = "mp4" if media_type == "video" else "jpg"
            # Include media_type in filename: video and photo can share the same index
            filename = downloads_dir / f"snap_{username}_{media_type}_{index}.{ext}"

            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status != 200:
                        logger.warning(f"Failed to download story item {index}: HTTP {resp.status}")
                        continue
                    async with aiofiles.open(filename, "wb") as f:
                        async for chunk in resp.content.iter_chunked(1024 * 64):
                            await f.write(chunk)
                downloaded.append((str(filename), media_type))
                logger.debug(f"Downloaded story item {index}: {filename}")
            except Exception as e:
                logger.warning(f"Failed to download story item {index}: {e}")
                continue

    return downloaded
