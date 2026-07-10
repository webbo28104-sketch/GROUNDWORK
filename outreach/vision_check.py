"""
Website-quality check for the outreach pipeline.

Given a prospect's website URL, returns one of:
  "no_website"          — no URL to check
  "has_website_dated"   — screenshot failed to load, OR Claude judged it dated
  "has_website_modern"  — Claude judged it modern/professional

Design choices (per spec):
  - A site that won't screenshot (timeout / SSL / DNS / network) is treated as
    "dated" — a broken or unreachable site is worthless to a customer anyway,
    so it's a qualifying signal, not a skip.
  - Any Claude error or unparseable response also fails safe to "dated".
  - Playwright is imported lazily inside a try/except so this module still
    imports on a box where playwright isn't installed; at runtime a missing
    Playwright just logs a warning and returns "has_website_dated".
"""
import os
import json
import base64
import logging

import anthropic

logger = logging.getLogger("outreach.vision_check")

VISION_MODEL = "claude-haiku-4-5-20251001"
SCREENSHOT_TIMEOUT_MS = 20000

# Try to import Playwright at module load; fall back gracefully if absent.
try:
    from playwright.sync_api import sync_playwright
    _PLAYWRIGHT_AVAILABLE = True
except Exception as _e:  # pragma: no cover — import guard
    sync_playwright = None
    _PLAYWRIGHT_AVAILABLE = False
    logger.warning("Playwright not importable (%s); vision_check will fall back to 'has_website_dated'", _e)


VISION_PROMPT = """You are checking if a UK trade business website looks modern and professional.

Score this website against this checklist — output ONLY a JSON object like {"hits": [1, 3], "verdict": "dated"} where hits is the list of checklist items that apply (1-6), and verdict is "dated" (2+ hits) or "modern" (0-1 hits).

Checklist:
1. Fixed/non-responsive layout — squeezed or misaligned content visible
2. Outdated design — default template look, clashing colours, stretched/pixelated images, low-res logo
3. No clear call-to-action anywhere on the visible page
4. Stale content — old copyright year visible, broken images, placeholder text
5. No reviews or testimonials visible (despite this being a trade business)
6. Unprofessional overall impression for a trade business website

Respond only with the JSON. No other text."""


def _take_screenshot(url):
    """Return PNG bytes of the above-the-fold view of `url`, or None on any
    failure. Raises nothing — all Playwright exceptions are caught here."""
    if not _PLAYWRIGHT_AVAILABLE:
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                page.goto(url, timeout=SCREENSHOT_TIMEOUT_MS, wait_until="load")
                png = page.screenshot(type="png")
            finally:
                browser.close()
        return png
    except Exception as e:
        logger.info("Screenshot failed for %s: %s", url, e)
        return None


def _ask_claude(png_bytes):
    """Send the screenshot to Claude vision and return the parsed verdict
    string ("has_website_dated" / "has_website_modern"). Fails safe to
    "has_website_dated" on any API/parse error."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY not set; vision check falling back to dated")
        return "has_website_dated"

    try:
        client = anthropic.Anthropic(api_key=api_key)
        b64 = base64.standard_b64encode(png_bytes).decode("ascii")
        resp = client.messages.create(
            model=VISION_MODEL,
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                    {"type": "text", "text": VISION_PROMPT},
                ],
            }],
        )
        text = "".join(block.text for block in resp.content if getattr(block, "type", None) == "text").strip()
    except Exception as e:
        logger.error("Claude vision call failed: %s", e)
        return "has_website_dated"

    parsed = _extract_json(text)
    if parsed is None:
        logger.error("Could not parse vision JSON from: %r", text[:200])
        return "has_website_dated"

    hits = parsed.get("hits") or []
    try:
        n_hits = len(hits)
    except TypeError:
        n_hits = 0
    return "has_website_dated" if n_hits >= 2 else "has_website_modern"


def _extract_json(text):
    """Best-effort extract the first JSON object from a text blob."""
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            return None
    return None


def check_website_status(url):
    """Public entry point. See module docstring for return values."""
    if not url or not str(url).strip():
        return "no_website"

    png = _take_screenshot(url)
    if png is None:
        # Failed to load / Playwright missing — spec says auto-dated.
        return "has_website_dated"

    return _ask_claude(png)
