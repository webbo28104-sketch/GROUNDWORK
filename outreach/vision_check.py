"""
Website screenshot capture for the outreach pipeline (Track A).

Takes a Playwright screenshot and saves it to outreach/screenshots/{prospect_id}.png.
The actual quality judgment (no_website / has_website_dated / has_website_modern)
is done by Cowork in its own session after reviewing the screenshot, then written
back via:
    python outreach/apply_result.py vision <prospect_id> <verdict>

Playwright is imported lazily so this module still imports on a machine where it
isn't installed; at runtime a missing Playwright logs a warning and returns None.
"""
import os
import logging

logger = logging.getLogger("outreach.vision_check")

SCREENSHOT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "outreach", "screenshots",
)
SCREENSHOT_TIMEOUT_MS = 20_000

try:
    from playwright.sync_api import sync_playwright
    _PLAYWRIGHT_AVAILABLE = True
except Exception as _e:
    sync_playwright = None
    _PLAYWRIGHT_AVAILABLE = False
    logger.warning("Playwright not importable (%s); screenshots will be skipped", _e)


def take_screenshot(prospect_id, url):
    """
    Capture a viewport screenshot of `url` and save it to
    outreach/screenshots/{prospect_id}.png.

    Returns the saved file path on success, or None if Playwright is
    unavailable or the page fails to load (timeout / SSL / DNS errors).
    Never raises.
    """
    if not url or not str(url).strip():
        return None

    if not _PLAYWRIGHT_AVAILABLE:
        logger.warning(
            "Playwright not available — skipping screenshot for prospect %s. "
            "Run: playwright install chromium",
            prospect_id,
        )
        return None

    os.makedirs(SCREENSHOT_DIR, exist_ok=True)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                page.goto(url, timeout=SCREENSHOT_TIMEOUT_MS, wait_until="load")
                png = page.screenshot(type="png")
            finally:
                browser.close()
    except Exception as e:
        logger.info("Screenshot failed for prospect %s (%s): %s", prospect_id, url, e)
        return None

    path = os.path.join(SCREENSHOT_DIR, f"{prospect_id}.png")
    with open(path, "wb") as f:
        f.write(png)
    logger.info("Screenshot saved: %s", path)
    return path
